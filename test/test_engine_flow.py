"""
车险出单智能体 - 核心引擎流程测试
覆盖完整的 7 阶段出单流程
"""
import json
import pytest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from src.api.schemas import (
    WorkflowPhase, CustomerType, InsuranceType, UserRole,
    PaymentStatus, UnderwritingStatus, VehicleInfo, IDCardInfo,
)
from src.core.engine import engine
from src.core.state import session_store
from src.services import ocr_service as ocr_module
from src.services.payment_service import payment_service
from src.services.provider_gateway import ProviderResponse, provider_gateway
from src.services.underwriting_service import underwriting_service


MOCK_VEHICLE = VehicleInfo(
    license_plate="京A88888",
    vin="LSVAM41Z7E2123456",
    engine_number="EA888-123456",
    brand_model="大众速腾1.4T自动舒适型",
    registration_date=date(2022, 6, 15),
    owner_name="张三",
    use_type="非营运",
    seat_count=5,
    vehicle_type="小型轿车",
)

MOCK_ID_CARD = IDCardInfo(
    name="张三",
    id_number="110101199003071234",
    gender="男",
    ethnicity="汉族",
    birth_date=date(1990, 3, 7),
    address="北京市朝阳区XX路XX号",
    issuing_authority="北京市公安局朝阳分局",
    valid_from=date(2020, 1, 1),
    valid_to=date(2040, 1, 1),
    front_image_url="/uploads/id_front.jpg",
    back_image_url="/uploads/id_back.jpg",
)


@pytest.fixture
def mock_ocr():
    """Mock OCR 服务，返回真实的车辆和身份证数据"""
    with patch.object(ocr_module.ocr_service, "recognize_vehicle_license",
                      new_callable=AsyncMock) as mock_veh, \
         patch.object(ocr_module.ocr_service, "recognize_id_card",
                      new_callable=AsyncMock) as mock_id:
        mock_veh.return_value = MOCK_VEHICLE
        mock_id.return_value = MOCK_ID_CARD
        yield


class TestWorkflowEngine:
    """完整工作流测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.agent_id = "agent_001"
        self.user_id = "user_001"

        async def quote_response(payload):
            plan = payload["plan"]
            expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            data = {
                "provider_reference": f"QUOTE-{payload['session_id']}",
                "version": "v1",
                "valid_until": expires,
                "rate_source": "测试保司官方报价API",
                "total_premium": plan["total_premium"],
                "discount_rate": plan["discount_rate"],
                "items": plan["items"],
            }
            return ProviderResponse(data, json.dumps(data), 200)

        async def underwriting_response(payload):
            data = {
                "provider_reference": f"UW-{payload['quotation_id']}",
                "quote_version": payload["quote_version"],
                "amount": payload["amount"],
            }
            return ProviderResponse(data, json.dumps(data), 200)

        async def payment_response(payload):
            data = {
                "transaction_id": f"TX-{payload['quotation_id']}",
                "payment_url": "https://pay.insurer.test/checkout",
                "provider_reference": f"PAY-{payload['quotation_id']}",
                "quote_version": payload["quote_version"],
                "amount": payload["amount"],
            }
            return ProviderResponse(data, json.dumps(data), 200)

        with (
            patch.object(provider_gateway, "quote_configured", return_value=True),
            patch.object(
                provider_gateway,
                "underwriting_configured",
                return_value=True,
            ),
            patch.object(provider_gateway, "payment_configured", return_value=True),
            patch.object(
                provider_gateway,
                "request_quote",
                new=AsyncMock(side_effect=quote_response),
            ),
            patch.object(
                provider_gateway,
                "request_underwriting",
                new=AsyncMock(side_effect=underwriting_response),
            ),
            patch.object(
                provider_gateway,
                "request_payment",
                new=AsyncMock(side_effect=payment_response),
            ),
        ):
            yield

    async def _init_and_select_car(self):
        session = await session_store.create_session(self.agent_id, self.user_id)
        result = await engine.select_insurance_type(session.session_id, InsuranceType.CAR)
        assert result["success"]
        return session.session_id

    async def _complete_underwriting_callback(self, session_id: str):
        session = await session_store.get_session(session_id)
        assert session and session.quotation and session.underwriting_result
        quotation = session.quotation
        pending = session.underwriting_result
        amount = str(quotation.plan.total_premium)
        message = "核保通过"
        signature = underwriting_service.sign_callback(
            quotation.quotation_id,
            pending.provider_reference,
            "approved",
            message,
            amount,
            quotation.version,
        )
        result = underwriting_service.record_callback(
            quotation.quotation_id,
            pending.provider_reference,
            "approved",
            message,
            amount,
            quotation.version,
            signature,
        )
        assert await engine.apply_underwriting_callback(
            quotation.quotation_id, result
        )

    async def _complete_payment_callback(self, session_id: str):
        session = await session_store.get_session(session_id)
        assert session and session.payment_result and session.quotation
        payment = session.payment_result
        amount = str(payment.expected_amount)
        signature = payment_service.sign_callback(
            payment.transaction_id,
            payment.provider_reference,
            "success",
            amount,
            session.quotation.version,
        )
        return payment_service.record_callback(
            payment.transaction_id,
            payment.provider_reference,
            "success",
            amount,
            session.quotation.version,
            signature,
        )

    @pytest.mark.asyncio
    async def test_init_session(self):
        session = await session_store.create_session(self.agent_id, self.user_id)
        assert session.session_id
        assert session.agent_id == self.agent_id
        assert session.phase == WorkflowPhase.INSURANCE_TYPE_SELECTION
        assert session.insurance_type == InsuranceType.CAR

    @pytest.mark.asyncio
    async def test_select_insurance_type_car(self):
        session = await session_store.create_session(self.agent_id)
        result = await engine.select_insurance_type(session.session_id, InsuranceType.CAR)
        assert result["success"]
        assert result["phase"] == WorkflowPhase.COLLECTING_INFO.value

    @pytest.mark.asyncio
    async def test_select_insurance_type_unsupported(self):
        session = await session_store.create_session(self.agent_id)
        result = await engine.select_insurance_type(session.session_id, InsuranceType.PROPERTY)
        assert not result["success"]

    @pytest.mark.asyncio
    async def test_new_customer_collect_info(self, mock_ocr):
        session_id = await self._init_and_select_car()
        result = await engine.collect_info_new_customer(session_id, b"front", b"back")
        assert result["success"]
        assert result["vehicle"]["license_plate"] == "京A88888"

        session = await session_store.get_session(session_id)
        assert session.customer_type == CustomerType.NEW
        assert session.phase == WorkflowPhase.RECOMMENDING

    @pytest.mark.asyncio
    async def test_returning_customer_collect_info(self, mock_ocr):
        session_id = await self._init_and_select_car()
        # 先走新用户流程，把车辆信息写入 plate_index
        await engine.collect_info_new_customer(session_id, b"front", b"back")

        # 再作为老用户凭车牌号查询
        result = await engine.collect_info_returning_customer(
            session_id, plate="京A88888"
        )
        assert result["success"]
        assert result["customer_type"] == "returning"
        assert result["vehicle"]["license_plate"] == "京A88888"

        session = await session_store.get_session(session_id)
        assert session.customer_type == CustomerType.RETURNING

    @pytest.mark.asyncio
    async def test_invalid_plate_rejected(self):
        session_id = await self._init_and_select_car()
        result = await engine.collect_info_returning_customer(
            session_id, plate="INVALID"
        )
        assert not result["success"]

    @pytest.mark.asyncio
    async def test_generate_recommendation(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")

        result = await engine.generate_recommendation(session_id)
        assert result["success"]
        plan = result["plan"]
        assert plan["company"]["name"] == "平安保险"
        assert len(plan["items"]) >= 2

    @pytest.mark.asyncio
    async def test_use_last_year_plan(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.collect_info_returning_customer(session_id, plate="京A88888")

        result = await engine.use_last_year_plan(session_id)
        assert result["success"]
        assert result["phase"] == WorkflowPhase.IDENTITY_VERIFICATION.value

    @pytest.mark.asyncio
    async def test_modify_plan_natural_language(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)

        result = await engine.modify_plan(session_id, "三者改到100万")
        assert result["success"]
        plan = result["plan"]
        third_party = next(
            item for item in plan["items"] if item["code"] == "THIRD_PARTY"
        )
        assert third_party["coverage_amount"] == "1000000"
        assert third_party["premium"] == "1800"

    @pytest.mark.asyncio
    async def test_modify_plan_cheaper_option(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        recommendation = await engine.generate_recommendation(session_id)
        original_total = Decimal(recommendation["plan"]["total_premium"])

        result = await engine.modify_plan(session_id, "更优惠的方案")

        assert result["success"]
        assert Decimal(result["plan"]["total_premium"]) < original_total
        assert result["plan"]["items"]

    @pytest.mark.asyncio
    async def test_modify_plan_compound_removal_never_empties_plan(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)

        result = await engine.modify_plan(
            session_id,
            "降低三者险保额（如从200万降到100万） • "
            "去掉车上人员责任险 • "
            "去掉车损险（仅保留交强+三者）",
        )

        assert result["success"]
        assert {
            item["code"] for item in result["plan"]["items"]
        } == {"COMPULSORY", "THIRD_PARTY"}
        assert Decimal(result["plan"]["total_premium"]) > 0

    @pytest.mark.asyncio
    async def test_compulsory_removal_returns_business_explanation(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        recommendation = await engine.generate_recommendation(session_id)

        result = await engine.modify_plan(session_id, "去掉强制性的")

        assert result["success"]
        assert "交强险" in result["message"]
        assert "不能从方案中移除" in result["message"]
        assert any(
            item["code"] == "COMPULSORY" for item in result["plan"]["items"]
        )
        assert result["plan"]["total_premium"] == recommendation["plan"]["total_premium"]

    @pytest.mark.asyncio
    async def test_remove_all_non_mandatory_coverage(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)

        result = await engine.modify_plan(
            session_id,
            "去掉法律不强制要求的",
        )

        assert result["success"]
        assert [item["code"] for item in result["plan"]["items"]] == [
            "COMPULSORY"
        ]
        assert "当前仅保留交强险" in result["message"]
        assert result["plan"]["total_premium"] == "807.50"

    @pytest.mark.asyncio
    async def test_confirm_plan(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)

        result = await engine.modify_plan(session_id, "确认")
        assert result["success"]
        assert result["phase"] == WorkflowPhase.IDENTITY_VERIFICATION.value

    @pytest.mark.asyncio
    async def test_upload_id_card(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)
        await engine.modify_plan(session_id, "确认")

        result = await engine.upload_applicant_id_card(session_id, b"front", b"back")
        assert result["success"]
        assert result["phase"] == WorkflowPhase.QUOTING.value

    @pytest.mark.asyncio
    async def test_generate_quotation(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)
        await engine.modify_plan(session_id, "确认")
        await engine.upload_applicant_id_card(session_id, b"front", b"back")

        result = await engine.generate_quotation(session_id)
        assert result["success"]
        quotation = result["quotation"]
        assert quotation["vehicle"]["license_plate"] == "京A88888"
        assert quotation["applicant"] is not None
        assert quotation["effective_date"] is not None
        assert quotation["expire_date"] is not None
        assert quotation["version"] == "v1"
        assert quotation["rate_source"] == "测试保司官方报价API"
        assert quotation["valid_until"] is not None
        assert quotation["provider_reference"]
        assert quotation["request_digest"]
        assert quotation["is_estimate"] is False

    @pytest.mark.asyncio
    async def test_submit_underwriting(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)
        await engine.modify_plan(session_id, "确认")
        await engine.upload_applicant_id_card(session_id, b"front", b"back")
        await engine.generate_quotation(session_id)

        result = await engine.submit_underwriting(session_id)
        assert result["underwriting"] is not None
        assert result["underwriting"]["status"] == "pending"
        assert result["phase"] == WorkflowPhase.UNDERWRITING.value

    @pytest.mark.asyncio
    async def test_initiate_payment(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)
        await engine.modify_plan(session_id, "确认")
        await engine.upload_applicant_id_card(session_id, b"front", b"back")
        await engine.generate_quotation(session_id)
        await engine.submit_underwriting(session_id)
        await self._complete_underwriting_callback(session_id)

        result = await engine.initiate_payment(session_id)
        assert result["success"]
        assert result["payment"]["status"] == PaymentStatus.PROCESSING.value

    @pytest.mark.asyncio
    async def test_verify_payment_requires_provider_callback(self, mock_ocr):
        session_id = await self._init_and_select_car()
        await engine.collect_info_new_customer(session_id, b"f", b"b")
        await engine.generate_recommendation(session_id)
        await engine.modify_plan(session_id, "确认")
        await engine.upload_applicant_id_card(session_id, b"front", b"back")
        await engine.generate_quotation(session_id)
        await engine.submit_underwriting(session_id)
        await self._complete_underwriting_callback(session_id)
        await engine.initiate_payment(session_id)
        await self._complete_payment_callback(session_id)

        result = await engine.verify_payment(session_id)
        assert result["success"]
        assert result["payment"]["status"] == PaymentStatus.SUCCESS.value
        assert result.get("policy") is None
        assert "保单出单接口尚未完成" in result["message"]

    @pytest.mark.asyncio
    async def test_full_workflow_new_customer(self, mock_ocr):
        """端到端新用户出单流程"""
        session_id = await self._init_and_select_car()

        # Phase 1: 上传行驶证 OCR
        result = await engine.collect_info_new_customer(session_id, b"front", b"back")
        assert result["success"]

        # Phase 2: 智能推荐
        result = await engine.generate_recommendation(session_id)
        assert result["success"]

        # 确认方案
        result = await engine.modify_plan(session_id, "确认")
        assert result["success"]

        # Phase 3: 上传身份证
        result = await engine.upload_applicant_id_card(session_id, b"front", b"back")
        assert result["success"]

        # Phase 4: 报价
        result = await engine.generate_quotation(session_id)
        assert result["success"]

        # Phase 5: 核保
        result = await engine.submit_underwriting(session_id)
        assert result["underwriting"] is not None
        await self._complete_underwriting_callback(session_id)

        # Phase 6: 支付
        result = await engine.initiate_payment(session_id)
        assert result["success"]
        await self._complete_payment_callback(session_id)

        # Phase 7: 核验支付
        result = await engine.verify_payment(session_id)
        assert result["success"]
        assert result.get("policy") is None
