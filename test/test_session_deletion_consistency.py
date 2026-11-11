"""会话删除时内存、Redis、MySQL 一致性测试。"""
from unittest.mock import AsyncMock, patch

import pytest

from src.api.schemas import Session
from src.core.state import SessionStore
from src.storage.repository import SessionRepository
from src.storage.redis_store import redis_store


@pytest.mark.asyncio
async def test_deleted_session_cannot_resurrect_from_redis_or_late_save():
    store = SessionStore()
    session = Session(
        session_id="delete-consistency",
        agent_id="agent_001",
        user_id="customer_001",
    )
    store._sessions[session.session_id] = session
    store._user_sessions[session.user_id] = [session.session_id]
    store._agent_sessions[session.agent_id] = [session.session_id]
    store._redis_available = True

    with (
        patch.object(redis_store, "delete_session", new=AsyncMock()) as redis_delete,
        patch.object(
            redis_store,
            "get_user_sessions",
            new=AsyncMock(return_value=[session]),
        ),
        patch.object(redis_store, "save_session", new=AsyncMock()) as redis_save,
        patch.object(
            redis_store,
            "get_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        await store.remove_session(session.session_id)
        await store.save_session(session)

        assert await store.get_session(session.session_id) is None
        assert await store.get_user_sessions(session.user_id) == []
        assert session.session_id in store._deleted_session_ids
        assert session.session_id not in store._pending_persists
        redis_delete.assert_awaited_once()
        redis_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_mysql_session_lists_filter_closed_records():
    statements = []

    class ScalarResult:
        def all(self):
            return []

    class QueryResult:
        def scalars(self):
            return ScalarResult()

    class Db:
        async def execute(self, statement):
            statements.append(str(statement))
            return QueryResult()

    await SessionRepository.get_user_sessions(Db(), "customer_001")
    await SessionRepository.get_agent_sessions(Db(), "agent_001")

    assert len(statements) == 2
    assert all("sessions.status" in statement for statement in statements)
