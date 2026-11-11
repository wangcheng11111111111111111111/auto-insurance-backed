"""用户聊天记忆恢复接口测试。"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.api.routes_chat import (
    BatchDeleteSessionsRequest,
    ConversationMessageRequest,
    batch_delete_customer_sessions,
    delete_customer_session,
    list_recent_customer_sessions,
    send_conversation_message,
)
from src.api.schemas import Session, now_utc
from config.settings import settings
from src.core.state import session_store
from src.services.conversation_service import conversation_service


@pytest.mark.asyncio
async def test_recent_sessions_only_returns_resumable_memory():
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth=SimpleNamespace(role="customer", subject="customer_001")
        )
    )
    empty = Session(session_id="empty", agent_id="agent_001", user_id="customer_001")
    older = Session(session_id="older", agent_id="agent_001", user_id="customer_001")
    older.updated_at = now_utc() - timedelta(hours=1)
    older.append_conversation_message("customer", "旧会话")
    latest = Session(session_id="latest", agent_id="agent_001", user_id="customer_001")
    latest.updated_at = now_utc()
    latest.append_conversation_message("assistant", "最新回答")

    with patch.object(
        session_store,
        "get_user_sessions",
        new=AsyncMock(return_value=[empty, older, latest]),
    ):
        result = await list_recent_customer_sessions(request, limit=10)

    assert [item["session_id"] for item in result["sessions"]] == [
        "latest",
        "older",
    ]
    assert result["sessions"][0]["message_count"] == 1
    assert result["sessions"][0]["last_message"] == "最新回答"


@pytest.mark.asyncio
async def test_chat_message_persists_customer_and_assistant_memory():
    session = Session(
        session_id="memory-chat",
        agent_id="agent_001",
        user_id="customer_001",
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth=SimpleNamespace(
                role="customer",
                subject="customer_001",
                agent_id="agent_001",
            )
        )
    )
    req = ConversationMessageRequest(
        session_id=session.session_id,
        message="三者险必须买吗？",
        history=[],
    )

    with (
        patch.object(
            session_store,
            "get_session",
            new=AsyncMock(return_value=session),
        ),
        patch.object(session_store, "save_session", new=AsyncMock()),
        patch.object(
            conversation_service,
            "respond",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "action": "answer",
                    "message": "三者险属于商业险，不是法定强制险。",
                    "trace": [],
                    "phase": "insurance_type_selection",
                }
            ),
        ),
        patch.object(settings.security, "REQUIRE_CLIENT_ENCRYPTION", False),
    ):
        result = await send_conversation_message(req, request)

    assert result["action"] == "answer"
    assert [item["role"] for item in session.conversation_messages] == [
        "customer",
        "assistant",
    ]
    assert session.conversation_messages[1]["content"] == (
        "三者险属于商业险，不是法定强制险。"
    )


@pytest.mark.asyncio
async def test_customer_can_delete_own_session():
    session = Session(session_id="delete-me", user_id="customer_001")
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth=SimpleNamespace(role="customer", subject="customer_001")
        )
    )

    with (
        patch.object(
            session_store,
            "get_session",
            new=AsyncMock(return_value=session),
        ),
        patch.object(session_store, "remove_session", new=AsyncMock()) as remove,
    ):
        result = await delete_customer_session(session.session_id, request)

    assert result["success"]
    remove.assert_awaited_once_with("delete-me")


@pytest.mark.asyncio
async def test_batch_delete_only_removes_owned_sessions():
    owned = Session(session_id="owned", user_id="customer_001")
    foreign = Session(session_id="foreign", user_id="another_customer")
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth=SimpleNamespace(role="customer", subject="customer_001")
        )
    )

    async def get_session(session_id):
        return {"owned": owned, "foreign": foreign}.get(session_id)

    with (
        patch.object(session_store, "get_session", new=AsyncMock(side_effect=get_session)),
        patch.object(session_store, "remove_session", new=AsyncMock()) as remove,
    ):
        result = await batch_delete_customer_sessions(
            BatchDeleteSessionsRequest(
                session_ids=["owned", "foreign", "missing"]
            ),
            request,
        )

    assert result["deleted"] == ["owned"]
    assert result["skipped"] == ["foreign", "missing"]
    remove.assert_awaited_once_with("owned")
