"""业务流程回退测试。"""
from decimal import Decimal

import pytest

from src.api.schemas import (
    InsurancePlan,
    PRESET_COMPANIES,
    PRESET_INSURANCE_ITEMS,
    PaymentResult,
    PaymentStatus,
    Quotation,
    Session,
    UnderwritingResult,
    UnderwritingStatus,
    VehicleInfo,
    WorkflowPhase,
)
from src.core.engine import engine
from src.core.state import session_store


def _build_session(session_id: str) -> Session:
    plan = InsurancePlan(
        company=PRESET_COMPANIES["pingan"],
        items=[
            PRESET_INSURANCE_ITEMS["compulsory"],
            PRESET_INSURANCE_ITEMS["third_party_1m"],
        ],
        discount_rate=PRESET_COMPANIES["pingan"].discount_rate,
    )
    plan.calculate_total()
    return Session(
        session_id=session_id,
        agent_id="agent_001",
        user_id="customer_001",
        phase=WorkflowPhase.PAYMENT,
        insurance_plan=plan,
        vehicle_info=VehicleInfo(
            license_plate="京A12345",
            vin="LSVAA49J0N2123456",
            brand_model="大众牌SVW71521DF",
        ),
        quotation=Quotation(
            quotation_id="quote-1",
            version="v1",
            plan=plan,
            status="quoted",
        ),
        underwriting_result=UnderwritingResult(
            status=UnderwritingStatus.APPROVED,
            provider_reference="UW-1",
            quote_version="v1",
            approved_amount=plan.total_premium,
        ),
    )


@pytest.mark.asyncio
async def test_rollback_to_recommendation_invalidates_downstream_data():
    session = _build_session("rollback-session")
    await session_store.save_session(session)

    result = await engine.rollback_session(
        session.session_id,
        WorkflowPhase.RECOMMENDING.value,
    )

    updated = await session_store.get_session(session.session_id)
    assert result["success"]
    assert updated is not None
    assert updated.phase == WorkflowPhase.RECOMMENDING
    assert updated.insurance_plan is not None
    assert updated.quotation is None
    assert updated.underwriting_result is None
    assert updated.payment_result is None
    assert updated.policy is None


@pytest.mark.asyncio
async def test_rollback_blocked_after_real_payment_success():
    session = _build_session("rollback-paid")
    session.payment_result = PaymentResult(
        status=PaymentStatus.SUCCESS,
        transaction_id="TX-1",
        paid_amount=Decimal("2337.50"),
        expected_amount=Decimal("2337.50"),
    )
    await session_store.save_session(session)

    result = await engine.rollback_session(
        session.session_id,
        WorkflowPhase.RECOMMENDING.value,
    )

    assert not result["success"]
    assert "支付已确认成功" in result["message"]
