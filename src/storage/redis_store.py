"""
Redis 会话存储 - 替代内存 SessionStore
支持持久化、多实例共享、服务重启不丢失
"""
import json
import logging
from typing import Dict, List, Optional

import redis.asyncio as aioredis
from redis.asyncio import Redis

from config.settings import settings
from src.api.schemas import (
    Session, InsuranceType, UserRole, WorkflowPhase,
)

logger = logging.getLogger(__name__)

# Redis Key 前缀
PREFIX_SESSION = "session:"        # session:{session_id} → JSON
PREFIX_PLATE = "plate:"           # plate:{plate} → session_id
PREFIX_VIN = "vin:"               # vin:{vin} → session_id
PREFIX_USER = "user_sessions:"    # user_sessions:{user_id} → set[session_id]
PREFIX_AGENT = "agent_sessions:"  # agent_sessions:{agent_id} → set[session_id]
PREFIX_DELETED = "deleted_session:"  # deleted_session:{session_id} → tombstone
DELETED_TTL_SECONDS = 30 * 24 * 60 * 60


class RedisSessionStore:
    """基于 Redis 的会话存储"""

    def __init__(self):
        self._redis: Optional[Redis] = None

    async def connect(self):
        """连接 Redis"""
        self._redis = aioredis.Redis(
            host=settings.db.REDIS_HOST,
            port=settings.db.REDIS_PORT,
            password=settings.db.REDIS_PASSWORD or None,
            db=settings.db.REDIS_DB,
            decode_responses=True,
        )
        await self._redis.ping()
        logger.info("Redis 会话存储已连接: %s:%s", settings.db.REDIS_HOST, settings.db.REDIS_PORT)

    async def disconnect(self):
        if self._redis:
            await self._redis.close()
            logger.info("Redis 连接已关闭")

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            raise RuntimeError("Redis 未连接，请先调用 connect()")
        return self._redis

    def _session_to_dict(self, session: Session) -> dict:
        """Session → Redis 存储用的字典（完整状态，无损往返）"""
        return session.to_state_dict()

    def _dict_to_session(self, data: dict) -> Session:
        """Redis 字典 → Session（无损还原完整字段）"""
        return Session.from_state_dict(data)

    # ==================== 会话 CRUD ====================

    async def create_session(
        self,
        agent_id: str,
        user_id: str = "",
        role: UserRole = UserRole.CUSTOMER,
        insurance_type: InsuranceType = InsuranceType.CAR,
    ) -> Session:
        session = Session(
            agent_id=agent_id,
            user_id=user_id,
            role=role,
            insurance_type=insurance_type,
        )
        session.generate_id()

        session_data = self._session_to_dict(session)
        key = f"{PREFIX_SESSION}{session.session_id}"

        pipe = self.redis.pipeline()
        pipe.set(key, json.dumps(session_data, ensure_ascii=False))
        pipe.expire(key, 3600 * 24)  # 24小时过期
        if user_id:
            pipe.sadd(f"{PREFIX_USER}{user_id}", session.session_id)
        pipe.sadd(f"{PREFIX_AGENT}{agent_id}", session.session_id)
        await pipe.execute()

        logger.info("创建会话(Redis): %s", session.session_id[:8])
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        if await self.redis.exists(f"{PREFIX_DELETED}{session_id}"):
            return None
        key = f"{PREFIX_SESSION}{session_id}"
        data = await self.redis.get(key)
        if not data:
            return None
        return self._dict_to_session(json.loads(data))

    async def save_session(self, session: Session):
        """保存/更新会话（同时维护 user/agent/plate/vin 索引，幂等）。"""
        if await self.redis.exists(f"{PREFIX_DELETED}{session.session_id}"):
            logger.warning("忽略已删除会话的 Redis 写入: %s", session.session_id[:8])
            return
        key = f"{PREFIX_SESSION}{session.session_id}"
        session_data = self._session_to_dict(session)

        # 换绑车牌/车架号时清理旧索引，避免旧键仍指向该会话
        old_plate = ""
        old_vin = ""
        old_data = await self.redis.get(key)
        if old_data:
            old_session = self._dict_to_session(json.loads(old_data))
            old_plate = old_session.license_plate or ""
            old_vin = (old_session.vehicle_info.vin if old_session.vehicle_info else "") or ""

        new_plate = session.license_plate or ""
        new_vin = (session.vehicle_info.vin if session.vehicle_info else "") or ""

        pipe = self.redis.pipeline()
        pipe.set(key, json.dumps(session_data, ensure_ascii=False))
        pipe.expire(key, 3600 * 24)
        if session.user_id:
            pipe.sadd(f"{PREFIX_USER}{session.user_id}", session.session_id)
        pipe.sadd(f"{PREFIX_AGENT}{session.agent_id}", session.session_id)
        if old_plate and old_plate != new_plate:
            pipe.delete(f"{PREFIX_PLATE}{old_plate}")
        if new_plate:
            pipe.set(f"{PREFIX_PLATE}{new_plate}", session.session_id)
            pipe.expire(f"{PREFIX_PLATE}{new_plate}", 3600 * 24)
        if old_vin and old_vin != new_vin:
            pipe.delete(f"{PREFIX_VIN}{old_vin}")
        if new_vin:
            pipe.set(f"{PREFIX_VIN}{new_vin}", session.session_id)
            pipe.expire(f"{PREFIX_VIN}{new_vin}", 3600 * 24)
        await pipe.execute()

    async def load_all(self) -> list[Session]:
        """扫描并还原 Redis 中所有会话（用于启动预加载）。批量 mget，避免 N+1。"""
        keys = [key async for key in self.redis.scan_iter(f"{PREFIX_SESSION}*")]
        if not keys:
            return []
        deleted_ids = {
            key.removeprefix(PREFIX_DELETED)
            async for key in self.redis.scan_iter(f"{PREFIX_DELETED}*")
        }
        datas = await self.redis.mget(keys)
        sessions = []
        for key, data in zip(keys, datas):
            session_id = key.removeprefix(PREFIX_SESSION)
            if data and session_id not in deleted_ids:
                sessions.append(Session.from_state_dict(json.loads(data)))
        return sessions

    async def delete_session(
        self,
        session_id: str,
        user_id: str = "",
        agent_id: str = "",
    ):
        session = await self.get_session(session_id)

        pipe = self.redis.pipeline()
        pipe.set(
            f"{PREFIX_DELETED}{session_id}",
            "1",
            ex=DELETED_TTL_SECONDS,
        )
        pipe.delete(f"{PREFIX_SESSION}{session_id}")
        if session and session.license_plate:
            pipe.delete(f"{PREFIX_PLATE}{session.license_plate}")
        vin = (
            (session.vehicle_info.vin if session.vehicle_info else "") or ""
            if session
            else ""
        )
        if vin:
            pipe.delete(f"{PREFIX_VIN}{vin}")
        resolved_user_id = session.user_id if session else user_id
        resolved_agent_id = session.agent_id if session else agent_id
        if resolved_user_id:
            pipe.srem(f"{PREFIX_USER}{resolved_user_id}", session_id)
        if resolved_agent_id:
            pipe.srem(f"{PREFIX_AGENT}{resolved_agent_id}", session_id)
        await pipe.execute()
        logger.info("删除会话(Redis): %s", session_id[:8])

    # ==================== 车牌索引 ====================

    async def get_or_create_by_plate(
        self, plate: str, agent_id: str, user_id: str = "", role: UserRole = UserRole.CUSTOMER
    ) -> Session:
        plate_key = f"{PREFIX_PLATE}{plate}"
        existing_sid = await self.redis.get(plate_key)

        if existing_sid:
            session = await self.get_session(existing_sid)
            if session and session.phase != WorkflowPhase.COMPLETED:
                logger.info("复用已有会话(Redis): plate=%s", plate)
                return session

        session = await self.create_session(agent_id, user_id, role)
        session.set_license_plate(plate)
        await self.redis.set(plate_key, session.session_id)
        await self.redis.expire(plate_key, 3600 * 24)
        await self.save_session(session)
        logger.info("车牌绑定新会话(Redis): %s -> %s", plate, session.session_id[:8])
        return session

    async def find_by_plate(self, plate: str) -> Optional[Session]:
        sid = await self.redis.get(f"{PREFIX_PLATE}{plate}")
        return await self.get_session(sid) if sid else None

    async def find_by_vin(self, vin: str) -> Optional[Session]:
        sid = await self.redis.get(f"{PREFIX_VIN}{vin}")
        return await self.get_session(sid) if sid else None

    async def find_by_plates(self, plates) -> Dict[str, Session]:
        """根据车牌列表一次性批量还原会话（消除 N+1 往返）。"""
        plates = [p for p in plates if p]
        if not plates:
            return {}
        plate_keys = [f"{PREFIX_PLATE}{p}" for p in plates]
        sids = await self.redis.mget(plate_keys)

        sessions_by_id: Dict[str, Session] = {}
        for s in await self._batch_get_sessions([s for s in sids if s]):
            sessions_by_id[s.session_id] = s

        result: Dict[str, Session] = {}
        for plate, sid in zip(plates, sids):
            if sid and sid in sessions_by_id:
                result[plate] = sessions_by_id[sid]
        return result

    async def link_plate(self, session_id: str, plate: str):
        session = await self.get_session(session_id)
        if not session:
            return
        session.set_license_plate(plate)
        await self.redis.set(f"{PREFIX_PLATE}{plate}", session_id)
        await self.redis.expire(f"{PREFIX_PLATE}{plate}", 3600 * 24)
        await self.save_session(session)

    # ==================== 批量查询 ====================

    async def _batch_get_sessions(self, sids) -> list[Session]:
        """按 session_id 列表一次性 mget 批量还原，避免 N+1 次往返。"""
        sids = list(sids)
        if not sids:
            return []
        keys = [f"{PREFIX_SESSION}{sid}" for sid in sids]
        datas = await self.redis.mget(keys)
        deleted = await self.redis.mget(
            [f"{PREFIX_DELETED}{sid}" for sid in sids]
        )
        sessions = []
        for data, deleted_marker in zip(datas, deleted):
            if data and not deleted_marker:
                sessions.append(self._dict_to_session(json.loads(data)))
        return sessions

    async def get_user_sessions(self, user_id: str) -> list[Session]:
        sids = await self.redis.smembers(f"{PREFIX_USER}{user_id}")
        return await self._batch_get_sessions(sids)

    async def get_agent_sessions(self, agent_id: str) -> list[Session]:
        sids = await self.redis.smembers(f"{PREFIX_AGENT}{agent_id}")
        return await self._batch_get_sessions(sids)

    async def active_count(self) -> int:
        count = 0
        async for key in self.redis.scan_iter(f"{PREFIX_SESSION}*"):
            count += 1
        return count


redis_store = RedisSessionStore()
