"""历史会话状态兼容性测试。"""
from src.api.schemas import Session


def test_old_session_state_without_conversation_messages_is_normalized():
    session = Session(session_id="legacy-session", agent_id="agent_001")
    state = session.to_state_dict()
    state["conversation_messages"] = None

    restored = Session.from_state_dict(state)

    assert restored.conversation_messages == []
    restored.append_conversation_message("system", "兼容性测试")
    assert len(restored.conversation_messages) == 1


def test_chat_memory_survives_session_state_roundtrip():
    session = Session(session_id="memory-session", agent_id="agent_001")
    session.append_conversation_message(
        "customer",
        "第三者责任险是必须的吗？",
        sender_id="customer_001",
        sender_name="客户",
    )
    session.append_conversation_message(
        "assistant",
        "交强险是强制险，三者险是商业险。",
        sender_name="智能体",
    )

    restored = Session.from_state_dict(session.to_state_dict())

    assert [message["role"] for message in restored.conversation_messages] == [
        "customer",
        "assistant",
    ]
    assert restored.conversation_messages[1]["content"] == (
        "交强险是强制险，三者险是商业险。"
    )
