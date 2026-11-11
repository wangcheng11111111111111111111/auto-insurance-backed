"""知识库检索增强和规则校验测试。"""
from unittest.mock import AsyncMock, patch

import pytest

from src.api.schemas import (
    IDCardInfo,
    InsurancePlan,
    PRESET_COMPANIES,
    PRESET_INSURANCE_ITEMS,
    Session,
    VehicleInfo,
    WorkflowPhase,
)
from src.knowledge.guardrails import knowledge_guardrails
from src.knowledge.parser import knowledge_parser
from src.knowledge.retrieval import knowledge_retrieval
from src.services.conversation_service import ConversationService
from src.services.llm_service import llm_service


@pytest.fixture
def loaded_knowledge():
    with (
        patch.object(knowledge_parser, "_loaded", True),
        patch.object(
            knowledge_parser,
            "_terminology",
            {"第三者责任险": ["三者"], "交强险": ["交强"]},
        ),
        patch.object(knowledge_parser, "_company_map", {}),
        patch.object(
            knowledge_parser,
            "_scenarios",
            [
                {
                    "source_id": 11,
                    "slang": "货车，营运的",
                    "description": "营运货车投保",
                }
            ],
        ),
        patch.object(
            knowledge_parser,
            "_scenario_map",
            {"货车，营运的": "营运货车投保"},
        ),
        patch.object(
            knowledge_parser,
            "_collection_rules",
            [
                {
                    "source_id": 52,
                    "sort_order": 4,
                    "text": "使用性质必须核实：涉及货车营运，必须核对道路运输证",
                },
                {
                    "source_id": 69,
                    "sort_order": 21,
                    "text": "三者保额底线建议：建议三者保额不低于200万",
                },
            ],
        ),
        patch.object(
            knowledge_parser,
            "_required_fields",
            [
                {
                    "source_id": 3,
                    "group": "车辆信息",
                    "field": "使用性质",
                    "required": "是",
                    "description": "货车必须核实营运性质",
                    "common_say": "营运还是非营运",
                }
            ],
        ),
        patch.object(
            knowledge_parser,
            "_scenario_followups",
            [
                {
                    "source_id": 171,
                    "agent_say": "货车，营运的",
                    "obtained": "营运货车投保",
                    "missing": "行驶证、道路运输证",
                    "followup": "请提供行驶证和道路运输证",
                }
            ],
        ),
        patch.object(
            knowledge_parser,
            "_script_templates",
            [
                {
                    "source_id": 8,
                    "code": "TRUCK-01",
                    "intent": "营运货车投保",
                    "category": "车辆信息",
                    "agent_questions": "车辆是否营运",
                    "standard_answer": "需要先核实道路运输证",
                    "customer_version": "货车可以投保，但需要先核实营运资质。",
                    "prohibited": "不得按家用车费率报价",
                }
            ],
        ),
    ):
        yield


def test_retrieval_returns_scenarios_rules_fields_followups_and_templates(
    loaded_knowledge,
):
    result = knowledge_retrieval.retrieve(
        "货车营运的怎么投保",
        WorkflowPhase.COLLECTING_INFO.value,
    )

    assert result.scenarios
    assert result.rules
    assert result.fields
    assert result.followups
    assert result.templates
    assert "规则 #52" in result.prompt_context()
    assert {source.type for source in result.sources} >= {
        "scenario",
        "rule",
        "field",
        "followup",
        "template",
    }


@pytest.mark.asyncio
async def test_conversation_injects_retrieved_context_and_returns_sources(
    loaded_knowledge,
):
    service = ConversationService()
    session = Session(session_id="rag-test", agent_id="agent_001")
    llm_call = AsyncMock(
        return_value=(
            '{"action":"answer","reply":"货车可以投保，但需要先核实营运资质。",'
            '"plate":"","vin":""}'
        )
    )

    with patch.object(llm_service, "chat", new=llm_call):
        result = await service.respond(session, "货车营运的怎么投保", [])

    system_prompt = llm_call.await_args.args[0]
    assert "知识库检索结果" in system_prompt
    assert "规则 #52" in system_prompt
    assert result["knowledge_sources"]


def test_guardrails_warn_on_low_third_party_and_block_operating_vehicle(
    loaded_knowledge,
):
    plan = InsurancePlan(
        company=PRESET_COMPANIES["pingan"],
        items=[
            PRESET_INSURANCE_ITEMS["compulsory"],
            PRESET_INSURANCE_ITEMS["third_party_1m"],
        ],
        discount_rate=PRESET_COMPANIES["pingan"].discount_rate,
    )
    plan.calculate_total()
    session = Session(
        session_id="guard-test",
        agent_id="agent_001",
        insurance_plan=plan,
        vehicle_info=VehicleInfo(
            license_plate="京A12345",
            vin="LSVAA49J0N2123456",
            brand_model="货车",
            owner_name="张三",
            use_type="非营运",
        ),
        applicant_id_card=IDCardInfo(
            name="张三",
            id_number="110101199003071234",
        ),
    )

    validation = knowledge_guardrails.validate_quote(session)
    assert validation.allowed
    assert any("200 万" in warning for warning in validation.warnings)

    session.vehicle_info.use_type = "营运货运"
    validation = knowledge_guardrails.validate_quote(session)
    assert not validation.allowed
    assert any("营运" in error for error in validation.errors)
