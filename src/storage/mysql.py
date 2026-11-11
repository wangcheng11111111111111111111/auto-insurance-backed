"""
MySQL 异步连接池 + SQLAlchemy ORM 模型 + 基础 Repository
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import quote_plus

import asyncmy
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import (
    Column, BigInteger, String, Integer, Float, Date,
    DateTime, Text, JSON, ForeignKey, DECIMAL,
    select, inspect, text,
)
from sqlalchemy.sql import func

from config.settings import settings

logger = logging.getLogger(__name__)


# ==================== 连接池 ====================

DATABASE_URL = (
    f"mysql+asyncmy://{settings.db.MYSQL_USER}:{quote_plus(settings.db.MYSQL_PASSWORD)}"
    f"@{settings.db.MYSQL_HOST}:{settings.db.MYSQL_PORT}/{settings.db.MYSQL_DATABASE}"
    f"?charset=utf8mb4"
)

engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.app.DEBUG,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False,
)


@asynccontextmanager
async def get_db() -> AsyncIterator[AsyncSession]:
    """获取数据库会话（上下文管理器）"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类"""
    pass


# ==================== 基础 Repository ====================

class BaseRepository:
    """通用 Repository，提供常用 CRUD 方法"""

    model: type[Base]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, id: int) -> Optional[Base]:
        return await self.session.get(self.model, id)

    async def get_all(self, limit: int = 100, offset: int = 0) -> list[Base]:
        result = await self.session.execute(
            select(self.model).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def create(self, **kwargs) -> Base:
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, id: int, **kwargs) -> Optional[Base]:
        instance = await self.get_by_id(id)
        if instance:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            await self.session.flush()
        return instance

    async def delete(self, id: int) -> bool:
        instance = await self.get_by_id(id)
        if instance:
            await self.session.delete(instance)
            await self.session.flush()
            return True
        return False

    async def exists(self, **kwargs) -> bool:
        stmt = select(self.model).filter_by(**kwargs).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def find_one(self, **kwargs) -> Optional[Base]:
        stmt = select(self.model).filter_by(**kwargs).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_all(self, **kwargs) -> list[Base]:
        stmt = select(self.model).filter_by(**kwargs)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self, **kwargs) -> int:
        stmt = select(func.count()).select_from(self.model).filter_by(**kwargs)
        result = await self.session.execute(stmt)
        return result.scalar() or 0


# ==================== 自动建库 + 建表 ====================

async def _ensure_database():
    """确保数据库存在，不存在则创建"""
    try:
        conn = await asyncio.wait_for(
            asyncmy.connect(
                host=settings.db.MYSQL_HOST,
                port=settings.db.MYSQL_PORT,
                user=settings.db.MYSQL_USER,
                password=settings.db.MYSQL_PASSWORD,
            ),
            timeout=5,
        )
        async with conn.cursor() as cur:
            db_name = settings.db.MYSQL_DATABASE
            # 校验数据库名，避免 f-string 拼接导致 SQL 注入
            if not re.fullmatch(r"[A-Za-z0-9_]+", db_name or ""):
                raise ValueError(f"非法数据库名: {db_name!r}")
            await cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            logger.info("数据库已就绪: %s", db_name)
        await conn.close()
    except Exception:
        raise


async def init_db():
    """初始化数据库 - 建库 + 建表（幂等操作，不会覆盖已有数据）"""
    try:
        await _ensure_database()
    except Exception as e:
        logger.warning("跳过自动建库（可能无权限或连接超时）: %s", e)

    async with engine.begin() as conn:
        # create_all 等价于 CREATE TABLE IF NOT EXISTS，幂等安全
        logger.info("检查并创建表结构...")
        await conn.run_sync(Base.metadata.create_all)
        # 针对已存在表的增量列迁移（create_all 不会修改已有表结构）
        await _migrate_sessions(conn)
        await _migrate_auth_tables(conn)
    logger.info("数据库初始化完成（已存在的表不会重复创建、已有数据不受影响）")


async def _migrate_sessions(conn):
    """增量迁移：为已存在的 sessions 表补充缺失列（幂等）。"""

    def _apply(sync_conn):
        insp = inspect(sync_conn)
        if "sessions" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("sessions")}
        if "state_json" not in cols:
            sync_conn.execute(text(
                "ALTER TABLE sessions ADD COLUMN state_json TEXT NULL "
                "COMMENT '会话完整状态 JSON 快照（Redis/内存恢复源）'"
            ))
            logger.info("已为 sessions 表新增 state_json 列")
        if "status" not in cols:
            sync_conn.execute(text(
                "ALTER TABLE sessions ADD COLUMN status TINYINT DEFAULT 1 "
                "COMMENT '状态: 1-活跃 0-已关闭'"
            ))
            logger.info("已为 sessions 表新增 status 列")
        if "vin" not in cols:
            sync_conn.execute(text(
                "ALTER TABLE sessions ADD COLUMN vin VARCHAR(64) DEFAULT '' "
                "COMMENT '关联车架号（明文索引）'"
            ))
            logger.info("已为 sessions 表新增 vin 列")
        indexes = {idx["name"] for idx in insp.get_indexes("sessions")}
        if "idx_session_vin" not in indexes:
            sync_conn.execute(text(
                "ALTER TABLE sessions ADD INDEX idx_session_vin (vin)"
            ))
            logger.info("已为 sessions 表新增 idx_session_vin 索引")

    await conn.run_sync(_apply)


async def _migrate_auth_tables(conn):
    """为已有数据库补充用户/业务员登录字段。"""

    def _apply(sync_conn):
        insp = inspect(sync_conn)
        tables = set(insp.get_table_names())
        for table in ("users", "agents"):
            if table not in tables:
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if "password_hash" not in cols:
                sync_conn.execute(text(
                    f"ALTER TABLE `{table}` ADD COLUMN password_hash "
                    "VARCHAR(255) DEFAULT '' COMMENT '密码哈希'"
                ))
                logger.info("已为 %s 表新增 password_hash 列", table)

    await conn.run_sync(_apply)


async def close_db():
    """关闭数据库连接"""
    await engine.dispose()
    logger.info("数据库连接已关闭")
