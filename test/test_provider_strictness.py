"""三方 API 未配置和回调严格校验测试。"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.api.schemas import (
    InsurancePlan,
    PRESET_COMPANIES,
    PRESET_INSURANCE_ITEMS,
    Session,
    UnderwritingResult,
    UnderwritingStatus,
    VehicleInfo,
)
from src.services.payment_service import PaymentService
from src.services.provider_gateway import (
    CallbackValidationError,
    ProviderAPIUnavailable,
    provider_gateway,
)
from src.services.quote_service import QuoteService
from src.services.underwriting_service import UnderwritingService


def _session():
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
        session_id="provider-test",
        agent_id="agent_001",
        user_id="customer_001",
    )
    session.insurance_plan = plan
    session.vehicle_info = VehicleInfo(
        license_plate="京A12345",
        vin="LSVAA49J0N2123456",
        brand_model="大众牌SVW71521DF",
    )
    return session


@pytest.mark.asyncio
async def test_quote_fails_when_provider_api_is_not_configured():
    with patch.object(provider_gateway, "quote_configured", return_value=False):
        with pytest.raises(ProviderAPIUnavailable, match="报价功能API未完成"):
            await QuoteService().create_quote(
                _session(),
                effective_date=date.today(),
                expire_date=date.today() + timedelta(days=365),
            )


@pytest.mark.asyncio
async def test_underwriting_and_payment_never_simulate_results():
    quotation = {
        "quotation_id": "quote-1",
        "version": "v1",
        "provider_reference": "QUOTE-REF",
        "valid_until": (
            datetime.now(timezone.utc) + timedelta(minutes=30)
        ).isoformat(),
        "total_premium": "2337.50",
    }
    with patch.object(
        provider_gateway, "underwriting_configured", return_value=False
    ):
        with pytest.raises(
            ProviderAPIUnavailable, match="核保功能API未完成"
        ):
            await UnderwritingService().submit_for_underwriting(quotation)

    with patch.object(provider_gateway, "payment_configured", return_value=False):
        with pytest.raises(ProviderAPIUnavailable, match="支付功能API未完成"):
            await PaymentService().create_payment(quotation)


def test_underwriting_callback_rejects_amount_or_version_mismatch():
    service = UnderwritingService()
    service._pending_records["quote-1"] = UnderwritingResult(
        status=UnderwritingStatus.PENDING,
        provider_reference="UW-REF",
        quote_version="v1",
        approved_amount=Decimal("2337.50"),
    )
    signature = service.sign_callback(
        "quote-1",
        "UW-REF",
        "approved",
        "核保通过",
        "2337.50",
        "v1",
    )

    with pytest.raises(CallbackValidationError, match="报价版本"):
        service.record_callback(
            "quote-1",
            "UW-REF",
            "approved",
            "核保通过",
            "2337.50",
            "v2",
            service.sign_callback(
                "quote-1",
                "UW-REF",
                "approved",
                "核保通过",
                "2337.50",
                "v2",
            ),
        )

    with pytest.raises(CallbackValidationError, match="金额"):
        service.record_callback(
            "quote-1",
            "UW-REF",
            "approved",
            "核保通过",
            "1.00",
            "v1",
            service.sign_callback(
                "quote-1",
                "UW-REF",
                "approved",
                "核保通过",
                "1.00",
                "v1",
            ),
        )


def test_payment_callback_rejects_amount_mismatch():
    service = PaymentService()
    payment = service._transactions
    from src.api.schemas import PaymentResult, PaymentStatus

    payment["TX-1"] = PaymentResult(
        status=PaymentStatus.PROCESSING,
        transaction_id="TX-1",
        provider_reference="PAY-REF",
        quote_version="v1",
        expected_amount=Decimal("2337.50"),
    )
    signature = service.sign_callback(
        "TX-1",
        "PAY-REF",
        "success",
        "1.00",
        "v1",
    )
    with pytest.raises(CallbackValidationError, match="金额"):
        service.record_callback(
            "TX-1",
            "PAY-REF",
            "success",
            "1.00",
            "v1",
            signature,
        )


def test_payment_verification_never_auto_succeeds_without_callback():
    from src.api.schemas import PaymentResult, PaymentStatus

    service = PaymentService()
    service._transactions["TX-PENDING"] = PaymentResult(
        status=PaymentStatus.PROCESSING,
        transaction_id="TX-PENDING",
        provider_reference="PAY-REF",
        quote_version="v1",
        expected_amount=Decimal("2337.50"),
        paid_amount=Decimal("2337.50"),
    )

    result = service.verify_payment(
        "TX-PENDING",
        expected_amount="2337.50",
        expected_quote_version="v1",
        expected_provider_reference="PAY-REF",
    )

    assert result.status == PaymentStatus.PROCESSING
