"""自然语言对话与流程操作分流测试。"""
from unittest.mock import AsyncMock, patch

import pytest

from src.api.schemas import (
    InsurancePlan,
    PRESET_COMPANIES,
    PRESET_INSURANCE_ITEMS,
    Session,
    WorkflowPhase,
)
from src.services.conversation_service import ConversationService
from src.services.llm_service import llm_service


@pytest.mark.asyncio
async def test_car_insurance_intro_is_answer_not_flow_action():
    service = ConversationService()
    session = Session(session_id="conversation-test", agent_id="agent_001")

    with patch.object(
        llm_service,
        "chat",
        new=AsyncMock(return_value=(
            '{"action":"answer","reply":"车险包括交强险和商业险。'
            '如需办理，请告诉我开始办理车险。","plate":"","vin":""}'
        )),
    ):
        result = await service.respond(session, "车险介绍", [])

    assert result["action"] == "answer"
    assert result["phase"] == WorkflowPhase.INSURANCE_TYPE_SELECTION.value
    assert "交强险" in result["message"]


@pytest.mark.asyncio
async def test_explicit_car_selection_advances_flow():
    service = ConversationService()
    session = Session(session_id="conversation-test", agent_id="agent_001")

    async def select_car(*_args, **_kwargs):
        session.advance_phase(WorkflowPhase.COLLECTING_INFO)
        return {
            "success": True,
            "message": "已选择车险，请提供车辆信息。",
            "phase": WorkflowPhase.COLLECTING_INFO.value,
        }

    with (
        patch.object(
            llm_service,
            "chat",
            new=AsyncMock(return_value='{"action":"select_car","reply":"","plate":"","vin":""}'),
        ),
        patch(
            "src.services.conversation_service.engine.select_insurance_type",
            new=AsyncMock(side_effect=select_car),
        ),
        patch.object(
            service,
            "_get_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        result = await service.respond(session, "我要办理车险", [])

    assert result["action"] == "select_car"
    assert result["phase"] == WorkflowPhase.COLLECTING_INFO.value


@pytest.mark.asyncio
async def test_cheaper_plan_uses_deterministic_modify_route():
    service = ConversationService()
    session = Session(session_id="conversation-test", agent_id="agent_001")
    session.advance_phase(WorkflowPhase.RECOMMENDING)
    llm_call = AsyncMock()

    with (
        patch.object(llm_service, "chat", new=llm_call),
        patch(
            "src.services.conversation_service.engine.modify_plan",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "message": "方案已更新",
                    "phase": WorkflowPhase.RECOMMENDING.value,
                }
            ),
        ) as modify_plan,
        patch.object(
            service,
            "_get_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        result = await service.respond(session, "更优惠的方案", [])

    assert result["action"] == "modify_plan"
    assert result["message"] == "方案已更新"
    modify_plan.assert_awaited_once()
    llm_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_implicit_remove_uses_last_discussed_plan_item():
    service = ConversationService()
    session = Session(session_id="conversation-test", agent_id="agent_001")
    session.advance_phase(WorkflowPhase.RECOMMENDING)
    session.insurance_plan = InsurancePlan(
        company=PRESET_COMPANIES["pingan"],
        items=[
            PRESET_INSURANCE_ITEMS["compulsory"],
            PRESET_INSURANCE_ITEMS["third_party_2m"],
            PRESET_INSURANCE_ITEMS["vehicle_loss"],
        ],
    )
    session.insurance_plan.calculate_total()

    with (
        patch(
            "src.services.conversation_service.engine.modify_plan",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "message": "方案已更新",
                    "phase": WorkflowPhase.RECOMMENDING.value,
                }
            ),
        ) as modify_plan,
        patch.object(
            service,
            "_get_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        result = await service.respond(
            session,
            "去掉",
            [
                {
                    "role": "assistant",
                    "content": (
                        "交强险是法律强制必须投保的。三者险属于商业险，"
                        "主要补充交强险对第三者赔偿额度不足的问题。"
                        "您可以调整三者险保额，也可以去掉该险种。"
                    ),
                }
            ],
        )

    assert result["action"] == "modify_plan"
    modify_plan.assert_awaited_once_with(
        "conversation-test",
        "去掉第三者责任险",
    )
