"""
车险出单智能体 - 会话存储
Redis 优先 + MySQL 异步持久化 + 内存 LRU 兜底

核心改进：
1. LRU 内存会话淘汰：超 MAX_MEMORY_SESSIONS 时，按 last_accessed 移除最久未访问
2. 会话 TTL 自动清理：启动后台清理协程，按 SESSION_TTL_SECONDS 超时扫描
3. 落库队列背压合并：入队前用 dict 去重相同 session_id，队列元素上限翻倍
4. get_agent_sessions / get_user_sessions 补 Redis + MySQL 批量回退
5. 所有内存中 Session 统一更新 last_accessed 字段，用于 LRU/TTL
"""
import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

from config.settings import settings
from src.api.schemas import Session, InsuranceType, UserRole, WorkflowPhase, now_utc
from src.storage.mysql import get_db, init_db, close_db
from src.storage.repository import (
    SessionRepository,
    QuotationRepository,
    PaymentRepository,
)
from src.storage.redis_store import redis_store

logger = logging.getLogger(__name__)


class SessionStore:
    """会话存储 - Redis 优先 + MySQL 异步持久化 + 内存 LRU 兜底"""

    def __init__(self):
        # 内存热缓存 / 兜底存储
        self._sessions: Dict[str, Session] = {}
        self._plate_index: Dict[str, str] = {}
        self._vin_index: Dict[str, str] = {}
        self._quotation_index: Dict[str, str] = {}
        self._user_sessions: Dict[str, List[str]] = {}
        self._agent_sessions: Dict[str, List[str]] = {}

        # 可用性标记
        self._redis_available = False
        self._mysql_available = False

        # MySQL 后台落库队列（单消费者，保证写入顺序）：带去重合并 dict
        self._pending_persists: Dict[str, dict] = {}
        self._mysql_queue: asyncio.Queue = asyncio.Queue(maxsize=20000)
        self._queued_ids: set = set()
        self._deleted_session_ids: set[str] = set()
        self._mysql_writer: Optional[asyncio.Task] = None
        self._ttl_scanner: Optional[asyncio.Task] = None
        self._shutting_down = False

        # 保护内存数据结构读写的轻量锁（防 LRU/扫描 与 业务请求并发竞态）
        self._lock = asyncio.Lock()

    def _now_ts(self) -> float:
        return time.time()

    def _touch(self, session: Session):
        """标记会话最近访问时间。"""
        session.updated_at = now_utc()

    @staticmethod
    def _vin_of(session: Session) -> str:
        """提取会话的车架号（用于明文索引），无车辆信息时返回空串。"""
        return (session.vehicle_info.vin if session.vehicle_info else "") or ""

    # ==================== 生命周期 ====================

    async def start(self) -> dict:
        """启动存储：连接 Redis + 初始化 MySQL + 启动落库队列 + TTL 扫描 + 预加载会话。"""
        status = {}

        try:
            await redis_store.connect()
            self._redis_available = True
            status["redis"] = "connected"
        except Exception as e:
            self._redis_available = False
            status["redis"] = f"unavailable: {e}"
            logger.warning("Redis 不可用，会话回退内存存储: %s", e)

        try:
            await init_db()
            self._mysql_available = True
            status["mysql"] = "connected"
        except Exception as e:
            self._mysql_available = False
            status["mysql"] = f"unavailable: {e}"
            logger.warning("MySQL 不可用，会话仅存内存/Redis: %s", e)

        self._shutting_down = False
        self._mysql_writer = asyncio.create_task(self._mysql_writer_loop())
        self._ttl_scanner = asyncio.create_task(self._ttl_scanner_loop())

        try:
            await self._preload()
        except Exception as e:
            logger.warning("会话预加载失败: %s", e)

        try:
            await self._backfill_plain_index()
        except Exception as e:
            logger.warning("会话明文索引回填失败: %s", e)

        return status

    async def shutdown(self):
        """优雅关闭：排空 MySQL 落库队列，断开 Redis 与 MySQL 连接。"""
        self._shutting_down = True

        for t in (self._mysql_writer, self._ttl_scanner):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        # 排空队列 + 待合并 dict 中剩余待落库会话
        leftover: List[dict] = []
        for sid in list(self._pending_persists.keys()):
            leftover.append(self._pending_persists[sid])
        for payload in leftover:
            try:
                await self._persist_mysql(payload)
            except Exception:
                pass
        self._pending_persists.clear()
        self._queued_ids.clear()

        if self._redis_available:
            try:
                await redis_store.disconnect()
            except Exception:
                pass

        try:
            await close_db()
        except Exception:
            pass
        logger.info("会话存储已关闭")

    async def _ttl_scanner_loop(self):
        """每 60 秒扫描一次内存会话，移除超时未访问的会话。"""
        ttl = float(settings.app.SESSION_TTL_SECONDS)
        while not self._shutting_down:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

            if self._shutting_down:
                break

            cutoff = self._now_ts() - ttl
            expired_sids: List[str] = []

            async with self._lock:
                for sid, session in self._sessions.items():
                    if session.updated_at.timestamp() < cutoff:
                        expired_sids.append(sid)

            for sid in expired_sids:
                logger.info("会话超时自动淘汰: session=%s", sid[:8])
                await self.remove_session(sid)

    async def _evict_lru_if_needed(self):
        """内存会话数超阈值时，淘汰最久未访问的 10% 个会话。"""
        max_sessions = settings.app.MAX_MEMORY_SESSIONS
        if len(self._sessions) <= max_sessions:
            return

        to_evict = sorted(
            self._sessions.values(),
            key=lambda s: s.updated_at.timestamp(),
        )[:max(len(self._sessions) // 10, 1)]

        for session in to_evict:
            # 从内存移除，但保留在 Redis/MySQL 中，下次访问从后端回退
            sid = session.session_id
            self._sessions.pop(sid, None)
            if session.license_plate:
                self._plate_index.pop(session.license_plate, None)
            vin = self._vin_of(session)
            if vin:
                self._vin_index.pop(vin, None)
            if session.quotation and session.quotation.quotation_id:
                self._quotation_index.pop(session.quotation.quotation_id, None)
            if session.user_id:
                sids = self._user_sessions.get(session.user_id, [])
                if sid in sids:
                    sids.remove(sid)
            sids = self._agent_sessions.get(session.agent_id, [])
            if sid in sids:
                sids.remove(sid)
            logger.info("内存会话 LRU 淘汰: session=%s (total=%d, limit=%d)", sid[:8], len(self._sessions), max_sessions)

    async def _preload(self):
        """启动时将已持久化会话载入内存（优先 Redis，其次 MySQL）。"""
        loaded = 0
        redis_sessions: List[Session] = []
        if self._redis_available:
            try:
                redis_sessions = await redis_store.load_all()
            except Exception as e:
                logger.warning("Redis 会话预加载失败: %s", e)

        if redis_sessions and self._mysql_available:
            try:
                async with get_db() as db:
                    rows = await SessionRepository.list_by_status(
                        db, status=1, limit=10000
                    )
                active_ids = {row.session_id for row in rows}
                redis_sessions = [
                    session
                    for session in redis_sessions
                    if session.session_id in active_ids
                ]
            except Exception as e:
                logger.warning("Redis 会话 MySQL 状态校验失败: %s", e)

        for session in redis_sessions:
            self._cache(session)
            loaded += 1

        if loaded == 0 and self._mysql_available:
            try:
                async with get_db() as db:
                    rows = await SessionRepository.list_by_status(db, status=1, limit=10000)
                for row in rows:
                    if row.state_json:
                        session = Session.from_state_dict(json.loads(row.state_json))
                        self._cache(session)
                        loaded += 1
            except Exception as e:
                logger.warning("MySQL 会话预加载失败: %s", e)

        if loaded:
            logger.info("已预加载 %d 个历史会话到内存", loaded)

    # ==================== 内存索引维护 ====================

    def _cache(self, session: Session):
        """将会话写入内存并维护 plate/vin/user/agent 索引（无淘汰检查，由写入侧调用前保障）。"""
        # 清理该会话旧的 plate/vin 索引，避免换绑车牌后旧索引仍命中
        existing = self._sessions.get(session.session_id)
        if existing:
            old_plate = existing.license_plate
            old_vin = self._vin_of(existing)
            old_qid = existing.quotation.quotation_id if existing.quotation else ""
            if old_plate and old_plate != session.license_plate:
                self._plate_index.pop(old_plate, None)
            if old_vin and old_vin != self._vin_of(session):
                self._vin_index.pop(old_vin, None)
            new_qid = session.quotation.quotation_id if session.quotation else ""
            if old_qid and old_qid != new_qid:
                self._quotation_index.pop(old_qid, None)

        self._sessions[session.session_id] = session

        if session.license_plate:
            self._plate_index[session.license_plate] = session.session_id

        vin = self._vin_of(session)
        if vin:
            self._vin_index[vin] = session.session_id

        if session.quotation and session.quotation.quotation_id:
            self._quotation_index[session.quotation.quotation_id] = session.session_id

        if session.user_id:
            sids = self._user_sessions.setdefault(session.user_id, [])
            if session.session_id not in sids:
                sids.append(session.session_id)

        sids = self._agent_sessions.setdefault(session.agent_id, [])
        if session.session_id not in sids:
            sids.append(session.session_id)

    # ==================== 会话 CRUD ====================

    async def create_session(
        self,
        agent_id: str,
        user_id: str = "",
        role: UserRole = UserRole.CUSTOMER,
        insurance_type: InsuranceType = InsuranceType.CAR,
    ) -> Session:
        """创建新会话（内存 + Redis + 异步 MySQL）。"""
        session = Session(
            agent_id=agent_id,
            user_id=user_id,
            role=role,
            insurance_type=insurance_type,
        )
        session.generate_id()
        await self.save_session(session)

        logger.info(
            "创建会话: session=%s, agent=%s, user=%s, role=%s",
            session.session_id[:8], agent_id, user_id, role.value,
        )
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话：内存 → Redis → MySQL 三级回退，命中后 touch 最近访问时间。"""
        if session_id in self._deleted_session_ids:
            return None
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                self._touch(session)
                return session

        if self._redis_available:
            try:
                session = await redis_store.get_session(session_id)
                if session:
                    async with self._lock:
                        self._cache(session)
                        await self._evict_lru_if_needed()
                    return session
            except Exception as e:
                logger.warning("Redis 读取失败: %s", e)

        if self._mysql_available:
            try:
                session = await self._load_from_mysql(session_id)
                if session:
                    async with self._lock:
                        self._cache(session)
                        await self._evict_lru_if_needed()
                    return session
            except Exception as e:
                logger.warning("MySQL 读取失败: %s", e)

        return None

    async def save_session(self, session: Session):
        """保存/更新会话：同步写 Redis，异步排队落 MySQL（带去重背压）。"""
        if session.session_id in self._deleted_session_ids:
            logger.warning("忽略已删除会话的迟到保存: %s", session.session_id[:8])
            return
        self._touch(session)

        async with self._lock:
            self._cache(session)
            await self._evict_lru_if_needed()

        if self._redis_available:
            try:
                await redis_store.save_session(session)
            except Exception as e:
                logger.warning("Redis 保存失败（已回退内存）: %s", e)

        self._enqueue_mysql(session)

    async def get_or_create_by_plate(
        self, plate: str, agent_id: str, user_id: str = "", role: UserRole = UserRole.CUSTOMER
    ) -> Session:
        """根据车牌号查找或创建会话（活跃会话复用，已完成会话新建）。"""
        existing = await self.find_by_plate(plate)
        if existing and existing.phase != WorkflowPhase.COMPLETED:
            logger.info("复用已有会话: plate=%s, session=%s", plate, existing.session_id[:8])
            return existing

        session = await self.create_session(agent_id, user_id, role)
        session.set_license_plate(plate)
        await self.save_session(session)
        logger.info("车牌绑定新会话: plate=%s -> session=%s", plate, session.session_id[:8])
        return session

    async def find_by_plate(self, plate: str) -> Optional[Session]:
        """根据车牌号查找会话：内存 → Redis → MySQL。"""
        async with self._lock:
            sid = self._plate_index.get(plate)
            if sid and sid in self._sessions:
                session = self._sessions[sid]
                self._touch(session)
                return session

        if self._redis_available:
            try:
                session = await redis_store.find_by_plate(plate)
                if session:
                    async with self._lock:
                        self._cache(session)
                        await self._evict_lru_if_needed()
                    return session
            except Exception as e:
                logger.warning("Redis 车牌查询失败: %s", e)

        if self._mysql_available:
            try:
                async with get_db() as db:
                    row = await SessionRepository.get_by_plate(db, plate)
                if row and row.state_json:
                    session = Session.from_state_dict(json.loads(row.state_json))
                    async with self._lock:
                        self._cache(session)
                        await self._evict_lru_if_needed()
                    return session
            except Exception as e:
                logger.warning("MySQL 车牌查询失败: %s", e)

        return None

    async def find_by_plates(self, plates: List[str]) -> Dict[str, Session]:
        """根据车牌列表批量查找会话（消除 N+1）：内存 → Redis 批量 → MySQL 批量。"""
        merged: Dict[str, Session] = {}
        missing: List[str] = []

        async with self._lock:
            for plate in plates:
                sid = self._plate_index.get(plate)
                if sid and sid in self._sessions:
                    session = self._sessions[sid]
                    self._touch(session)
                    merged[plate] = session
                else:
                    missing.append(plate)

        if missing and self._redis_available:
            try:
                found = await redis_store.find_by_plates(missing)
                for plate, s in found.items():
                    merged.setdefault(plate, s)
                missing = [p for p in missing if p not in found]
            except Exception as e:
                logger.warning("Redis 批量车牌查询失败: %s", e)

        if missing and self._mysql_available:
            try:
                async with get_db() as db:
                    rows = await SessionRepository.get_by_plates(db, missing)
                found_plates = set()
                for row in rows:
                    if row.state_json and row.license_plate:
                        s = Session.from_state_dict(json.loads(row.state_json))
                        merged.setdefault(row.license_plate, s)
                        found_plates.add(row.license_plate)
                missing = [p for p in missing if p not in found_plates]
            except Exception as e:
                logger.warning("MySQL 批量车牌查询失败: %s", e)

        if merged:
            async with self._lock:
                for s in merged.values():
                    self._cache(s)
                await self._evict_lru_if_needed()
        return merged

    async def link_plate(self, session_id: str, plate: str):
        """将会话与车牌号关联。"""
        session = await self.get_session(session_id)
        if not session:
            return
        session.set_license_plate(plate)
        await self.save_session(session)

    async def get_vehicle_by_plate(self, plate: str):
        """根据车牌号查找已 OCR 识别的车辆信息。"""
        session = await self.find_by_plate(plate)
        if session and session.vehicle_info:
            return session.vehicle_info
        return None

    async def find_by_vin(self, vin: str) -> Optional[Session]:
        """根据车架号查找会话：内存 → Redis → MySQL。"""
        vin = (vin or "").strip().upper()
        if not vin:
            return None

        async with self._lock:
            sid = self._vin_index.get(vin)
            if sid and sid in self._sessions:
                session = self._sessions[sid]
                self._touch(session)
                return session

        if self._redis_available:
            try:
                session = await redis_store.find_by_vin(vin)
                if session:
                    async with self._lock:
                        self._cache(session)
                        await self._evict_lru_if_needed()
                    return session
            except Exception as e:
                logger.warning("Redis 车架号查询失败: %s", e)

        if self._mysql_available:
            try:
                async with get_db() as db:
                    row = await SessionRepository.get_by_vin(db, vin)
                if row and row.state_json:
                    session = Session.from_state_dict(json.loads(row.state_json))
                    async with self._lock:
                        self._cache(session)
                        await self._evict_lru_if_needed()
                    return session
            except Exception as e:
                logger.warning("MySQL 车架号查询失败: %s", e)

        return None

    async def get_vehicle_by_vin(self, vin: str):
        """根据车架号查找已 OCR 识别的车辆信息。"""
        session = await self.find_by_vin(vin)
        if session and session.vehicle_info:
            return session.vehicle_info
        return None

    async def find_by_quotation_id(self, quotation_id: str) -> Optional[Session]:
        """根据报价单号查找会话：内存 → MySQL（quotations 表）。"""
        async with self._lock:
            sid = self._quotation_index.get(quotation_id)
            if sid and sid in self._sessions:
                session = self._sessions[sid]
                self._touch(session)
                return session

        if self._mysql_available:
            try:
                async with get_db() as db:
                    quotation = await QuotationRepository.get_by_quotation_id(db, quotation_id)
                if quotation and quotation.session_id:
                    return await self.get_session(quotation.session_id)
            except Exception as e:
                logger.warning("MySQL 报价单查询失败: %s", e)

        return None

    async def find_by_transaction_id(self, transaction_id: str) -> Optional[Session]:
        """根据支付交易号查找会话：内存 → MySQL 支付记录。"""
        async with self._lock:
            for session in self._sessions.values():
                if (
                    session.payment_result
                    and session.payment_result.transaction_id == transaction_id
                ):
                    self._touch(session)
                    return session

        if self._mysql_available:
            try:
                async with get_db() as db:
                    payment = await PaymentRepository.get_by_transaction(
                        db, transaction_id
                    )
                if payment and payment.session_id:
                    return await self.get_session(payment.session_id)
            except Exception as e:
                logger.warning("MySQL 支付记录查询失败: %s", e)
        return None

    # ==================== 批量查询 ====================

    async def get_user_sessions(self, user_id: str) -> List[Session]:
        """获取用户的所有会话：内存优先，Redis/MySQL 批量回退补全（消除 N+1）。"""
        merged: Dict[str, Session] = {}
        mysql_active_ids: Optional[set[str]] = None

        async with self._lock:
            for sid in self._user_sessions.get(user_id, []):
                s = self._sessions.get(sid)
                if s:
                    merged[sid] = s

        # Redis 批量回退（一次 mget）
        if self._redis_available:
            try:
                for s in await redis_store.get_user_sessions(user_id):
                    merged.setdefault(s.session_id, s)
            except Exception as e:
                logger.warning("Redis 用户会话查询失败: %s", e)

        # MySQL 批量回退（单条 SQL）
        if self._mysql_available:
            try:
                async with get_db() as db:
                    rows = await SessionRepository.get_user_sessions(db, user_id)
                mysql_active_ids = {row.session_id for row in rows}
                for row in rows:
                    if row.state_json:
                        s = Session.from_state_dict(json.loads(row.state_json))
                        merged.setdefault(s.session_id, s)
            except Exception as e:
                logger.warning("MySQL 用户会话查询失败: %s", e)

        if mysql_active_ids is not None:
            merged = {
                sid: session
                for sid, session in merged.items()
                if sid in mysql_active_ids
            }

        merged = {
            sid: session
            for sid, session in merged.items()
            if sid not in self._deleted_session_ids
        }
        if not merged:
            return []

        async with self._lock:
            for s in merged.values():
                self._cache(s)
            await self._evict_lru_if_needed()

        return list(merged.values())

    async def get_agent_sessions(self, agent_id: str) -> List[Session]:
        """获取业务员的所有会话：内存优先，Redis/MySQL 批量回退补全（消除 N+1）。"""
        merged: Dict[str, Session] = {}
        mysql_active_ids: Optional[set[str]] = None

        async with self._lock:
            for sid in self._agent_sessions.get(agent_id, []):
                s = self._sessions.get(sid)
                if s:
                    merged[sid] = s

        # Redis 批量回退（一次 mget）
        if self._redis_available:
            try:
                for s in await redis_store.get_agent_sessions(agent_id):
                    merged.setdefault(s.session_id, s)
            except Exception as e:
                logger.warning("Redis 业务员会话查询失败: %s", e)

        # MySQL 批量回退（单条 SQL）
        if self._mysql_available:
            try:
                async with get_db() as db:
                    rows = await SessionRepository.get_agent_sessions(db, agent_id)
                mysql_active_ids = {row.session_id for row in rows}
                for row in rows:
                    if row.state_json:
                        s = Session.from_state_dict(json.loads(row.state_json))
                        merged.setdefault(s.session_id, s)
            except Exception as e:
                logger.warning("MySQL 业务员会话查询失败: %s", e)

        if mysql_active_ids is not None:
            merged = {
                sid: session
                for sid, session in merged.items()
                if sid in mysql_active_ids
            }

        merged = {
            sid: session
            for sid, session in merged.items()
            if sid not in self._deleted_session_ids
        }
        if not merged:
            return []

        async with self._lock:
            for s in merged.values():
                self._cache(s)
            await self._evict_lru_if_needed()

        return list(merged.values())

    async def remove_session(self, session_id: str):
        """移除会话。"""
        self._deleted_session_ids.add(session_id)
        self._pending_persists.pop(session_id, None)
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                if session.license_plate:
                    self._plate_index.pop(session.license_plate, None)
                vin = self._vin_of(session)
                if vin:
                    self._vin_index.pop(vin, None)
                if session.quotation and session.quotation.quotation_id:
                    self._quotation_index.pop(session.quotation.quotation_id, None)
                if session.user_id:
                    sids = self._user_sessions.get(session.user_id, [])
                    if session_id in sids:
                        sids.remove(session_id)
                sids = self._agent_sessions.get(session.agent_id, [])
                if session_id in sids:
                    sids.remove(session_id)

        if self._redis_available:
            try:
                await redis_store.delete_session(
                    session_id,
                    user_id=session.user_id if session else "",
                    agent_id=session.agent_id if session else "",
                )
            except Exception as e:
                logger.warning("Redis 删除会话失败: %s", e)

        if self._mysql_available:
            try:
                async with get_db() as db:
                    await SessionRepository.close_session(db, session_id)
            except Exception as e:
                logger.warning("MySQL 关闭会话失败: %s", e)

        logger.info("移除会话: %s", session_id[:8])

    # ==================== MySQL 异步落库 - 背压合并 ====================

    def _enqueue_mysql(self, session: Session):
        """将完整状态快照放入后台落库队列（去重合并相同 session_id，非阻塞）。"""
        if (
            not self._mysql_available
            or self._shutting_down
            or session.session_id in self._deleted_session_ids
        ):
            return

        snapshot = session.to_state_dict()
        sid = snapshot.get("session_id")
        if not sid:
            return

        # 明文索引字段单独保留：state_json 中 plate/vin 已加密，MySQL 索引列需要明文
        self._pending_persists[sid] = {
            "snapshot": snapshot,
            "license_plate": session.license_plate or "",
            "vin": self._vin_of(session),
        }
        self._try_push(sid)

    def _try_push(self, sid: str):
        """将待落库会话 ID 推入队列；队列满时快照仍保留在 _pending_persists 等待补入。"""
        if sid in self._queued_ids:
            return
        try:
            self._mysql_queue.put_nowait(sid)
            self._queued_ids.add(sid)
        except asyncio.QueueFull:
            logger.warning("MySQL 落库队列已满，快照保留待补入: session=%s", sid[:8])

    def _drain_pending(self):
        """队列腾出空间后，将滞留（未入队）的待落库会话补入队列。"""
        for sid in list(self._pending_persists.keys()):
            if sid in self._queued_ids:
                continue
            if self._mysql_queue.full():
                break
            try:
                self._mysql_queue.put_nowait(sid)
                self._queued_ids.add(sid)
            except asyncio.QueueFull:
                break

    async def _mysql_writer_loop(self):
        """单消费者后台落库循环，保证同一会话写入顺序。"""
        while True:
            try:
                sid = await self._mysql_queue.get()
            except (asyncio.CancelledError, Exception):
                break

            payload = self._pending_persists.get(sid)
            if sid in self._deleted_session_ids:
                self._pending_persists.pop(sid, None)
            elif payload is not None:
                try:
                    await self._persist_mysql(payload)
                except Exception as e:
                    logger.warning("MySQL 落库失败（将在后续更新重试）: %s", e)
                finally:
                    self._pending_persists.pop(sid, None)

            self._queued_ids.discard(sid)
            self._mysql_queue.task_done()

            # 队列消费后腾出空间，尝试补入滞留会话
            self._drain_pending()

    async def _persist_mysql(self, payload: dict):
        """将状态快照幂等写入 MySQL sessions 表（明文索引字段从 payload 单独取）。"""
        snapshot = payload.get("snapshot") or {}
        session_id = snapshot.get("session_id")
        if not session_id:
            return
        if session_id in self._deleted_session_ids:
            return
        status = 0 if snapshot.get("phase") == WorkflowPhase.COMPLETED.value else 1
        async with get_db() as db:
            await SessionRepository.upsert(
                db,
                session_id,
                agent_id=snapshot.get("agent_id", ""),
                user_id=snapshot.get("user_id", ""),
                role=snapshot.get("role", UserRole.CUSTOMER.value),
                license_plate=payload.get("license_plate", ""),
                vin=payload.get("vin", ""),
                insurance_type=snapshot.get("insurance_type", InsuranceType.CAR.value),
                phase=snapshot.get("phase", WorkflowPhase.INSURANCE_TYPE_SELECTION.value),
                customer_type=snapshot.get("customer_type", "new"),
                state_json=json.dumps(snapshot, ensure_ascii=False),
                status=status,
            )

    async def _load_from_mysql(self, session_id: str) -> Optional[Session]:
        """从 MySQL 还原会话（基于 state_json 快照）。"""
        async with get_db() as db:
            row = await SessionRepository.get_by_id(db, session_id)
        if row and row.status == 1 and row.state_json:
            return Session.from_state_dict(json.loads(row.state_json))
        return None

    async def _backfill_plain_index(self):
        """一次性回填历史会话的明文 vin/车牌 索引列（幂等，可重复执行）。

        修复两类历史脏数据：
        1. 新增 vin 列后，存量会话的 vin 为空，需从 state_json 解密还原；
        2. 旧版落库把密文写入了 license_plate 明文索引列，需修复为明文。
        """
        if not self._mysql_available:
            return

        total = 0
        batch = 1000
        while True:
            try:
                async with get_db() as db:
                    rows = await SessionRepository.list_plain_index_dirty(db, limit=batch)
                if not rows:
                    break

                fixed = 0
                async with get_db() as db:
                    for row in rows:
                        if not row.state_json:
                            continue
                        try:
                            session = Session.from_state_dict(json.loads(row.state_json))
                        except Exception as e:
                            logger.warning(
                                "回填会话索引失败（跳过）: session=%s, err=%s",
                                row.session_id[:8], e,
                            )
                            continue
                        plate = session.license_plate or ""
                        vin = self._vin_of(session)
                        if row.license_plate == plate and row.vin == vin:
                            continue
                        await SessionRepository.update_plain_index(
                            db, row.session_id, plate, vin,
                        )
                        fixed += 1
                total += fixed
                if len(rows) < batch:
                    break
            except Exception as e:
                logger.warning("回填会话明文索引中断: %s", e)
                break

        if total:
            logger.info("已回填 %d 个历史会话的明文 vin/车牌 索引", total)

    # ==================== 统计 ====================

    @property
    def active_count(self) -> int:
        return len(self._sessions)


session_store = SessionStore()
