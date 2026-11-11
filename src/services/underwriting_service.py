"""保险公司真实核保服务。

本服务不再生成随机核保结果。提交核保必须成功调用保险公司 API，
最终结果只能通过完成签名、金额和报价版本校验的回调写入。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from config.settings import settings
from src.api.schemas import UnderwritingResult, UnderwritingStatus
from src.services.provider_gateway import (
    CallbackValidationError,
    ProviderAPIError,
    ProviderAPIUnavailable,
    payload_digest,
    provider_gateway,
)

logger = logging.getLogger(__name__)


class UnderwritingService:
    """核保服务：提交到保司并等待正式回调。"""

    def __init__(self):
        self._pending_records: dict[str, UnderwritingResult] = {}

    def _signing_key(self) -> str:
        return (
            settings.api.UNDERWRITING_API_KEY
            or settings.security.SECRET_KEY
            or "dev-secret"
        )

    @staticmethod
    def _sign(
        quotation_id: str,
        provider_reference: str,
        status: str,
        message: str,
        amount: str,
        quote_version: str,
        key: str,
    ) -> str:
        payload = (
            f"{quotation_id}:{provider_reference}:{status}:{message}:"
            f"{amount}:{quote_version}"
        )
        return hmac.new(
            key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def sign_callback(
        self,
        quotation_id: str,
        provider_reference: str,
        status: str,
        message: str,
        amount: str,
        quote_version: str,
    ) -> str:
        return self._sign(
            quotation_id,
            provider_reference,
            status,
            message,
            amount,
            quote_version,
            self._signing_key(),
        )

    def verify_callback_signature(
        self,
        quotation_id: str,
        provider_reference: str,
        status: str,
        message: str,
        amount: str,
        quote_version: str,
        signature: Optional[str],
    ) -> bool:
        if not signature:
            return False
        expected = self.sign_callback(
            quotation_id,
            provider_reference,
            status,
            message,
            amount,
            quote_version,
        )
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _parse_amount(value: Any) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CallbackValidationError("核保回调金额格式不正确") from exc
        return amount.quantize(Decimal("0.01"))

    async def submit_for_underwriting(
        self, quotation: dict[str, Any]
    ) -> UnderwritingResult:
        """提交核保；API 不可用时直接失败，不产生模拟结果。"""
        if not provider_gateway.underwriting_configured():
            raise ProviderAPIUnavailable(
                "核保功能API未完成：未配置有效的 UNDERWRITING_API_URL"
            )

        quotation_id = str(quotation.get("quotation_id") or "")
        quote_version = str(quotation.get("version") or "")
        total_premium = str(quotation.get("total_premium") or "")
        provider_reference = str(quotation.get("provider_reference") or "")
        valid_until = str(quotation.get("valid_until") or "")
        if not quotation_id or not quote_version or not total_premium:
            raise ProviderAPIError("报价单缺少报价ID、版本或金额，无法提交核保")
        if not provider_reference:
            raise ProviderAPIError("报价单缺少保司报价流水号，无法提交核保")
        if not valid_until:
            raise ProviderAPIError("报价单缺少有效期，无法提交核保")
        try:
            valid_until_dt = datetime.fromisoformat(
                valid_until.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ProviderAPIError("报价单有效期格式不正确") from exc
        if valid_until_dt.tzinfo is None:
            valid_until_dt = valid_until_dt.replace(tzinfo=timezone.utc)
        if valid_until_dt <= datetime.now(timezone.utc):
            raise ProviderAPIError("报价单已过期，请重新获取保司报价")

        payload = {
            "quotation_id": quotation_id,
            "quote_version": quote_version,
            "quote_provider_reference": provider_reference,
            "valid_until": valid_until,
            "amount": total_premium,
            "plan": quotation.get("plan"),
            "vehicle": quotation.get("vehicle"),
            "applicant": quotation.get("applicant"),
            "insured": quotation.get("insured"),
        }
        response = await provider_gateway.request_underwriting(payload)
        data = response.data
        underwriting_reference = str(
            data.get("provider_reference") or data.get("request_id") or ""
        ).strip()
        if not underwriting_reference:
            raise ProviderAPIError("核保API缺少 provider_reference")

        returned_version = str(data.get("quote_version") or quote_version)
        returned_amount = self._parse_amount(
            data.get("amount", total_premium)
        )
        if returned_version != quote_version:
            raise ProviderAPIError("核保API返回的报价版本与提交版本不一致")
        if returned_amount != Decimal(total_premium).quantize(Decimal("0.01")):
            raise ProviderAPIError("核保API返回的金额与报价金额不一致")

        pending = UnderwritingResult(
            status=UnderwritingStatus.PENDING,
            message="核保处理中，等待保险公司正式回调。",
            provider_reference=underwriting_reference,
            request_digest=payload_digest(payload),
            raw_response=response.raw_response,
            quote_version=quote_version,
            approved_amount=returned_amount,
            checked_at=datetime.now(timezone.utc),
        )
        self._pending_records[quotation_id] = pending
        return pending

    def record_callback(
        self,
        quotation_id: str,
        provider_reference: str,
        status: str,
        message: str,
        amount: str,
        quote_version: str,
        signature: Optional[str],
        expected_provider_reference: str = "",
        expected_amount: str = "",
        expected_quote_version: str = "",
    ) -> UnderwritingResult:
        """严格校验签名、金额、报价版本后写入最终核保结果。"""
        if not self.verify_callback_signature(
            quotation_id,
            provider_reference,
            status,
            message,
            amount,
            quote_version,
            signature,
        ):
            raise CallbackValidationError("核保回调签名无效")

        pending = self._pending_records.get(quotation_id)
        expected_reference = (
            pending.provider_reference
            if pending
            else expected_provider_reference
        )
        expected_amount_value = (
            str(pending.approved_amount)
            if pending and pending.approved_amount > 0
            else expected_amount
        )
        expected_version = (
            pending.quote_version if pending else expected_quote_version
        )
        if not expected_reference or provider_reference != expected_reference:
            raise CallbackValidationError("核保回调流水号与提交记录不一致")
        if not expected_version or quote_version != expected_version:
            raise CallbackValidationError("核保回调报价版本与提交记录不一致")
        if not expected_amount_value:
            raise CallbackValidationError("缺少核保回调金额校验基准")
        callback_amount = self._parse_amount(amount)
        if callback_amount != self._parse_amount(expected_amount_value):
            raise CallbackValidationError("核保回调金额与报价金额不一致")

        if status == "approved":
            final_status = UnderwritingStatus.APPROVED
        elif status == "needs_verification":
            final_status = UnderwritingStatus.NEEDS_VERIFICATION
        elif status == "rejected":
            final_status = UnderwritingStatus.REJECTED
        else:
            raise CallbackValidationError("核保回调状态不合法")

        result = UnderwritingResult(
            status=final_status,
            message=message or "核保已完成",
            checked_at=datetime.now(timezone.utc),
            provider_reference=provider_reference,
            request_digest=pending.request_digest if pending else "",
            raw_response=json.dumps(
                {
                    "callback_status": status,
                    "message": message,
                    "amount": amount,
                    "quote_version": quote_version,
                },
                ensure_ascii=False,
            ),
            quote_version=quote_version,
            approved_amount=callback_amount,
        )
        self._pending_records[quotation_id] = result
        return result

    def get_cached_result(
        self, quotation_id: str
    ) -> Optional[UnderwritingResult]:
        return self._pending_records.get(quotation_id)

    async def submit_verification_material(
        self,
        quotation: dict[str, Any],
        photo: bytes | None = None,
        video: bytes | None = None,
    ) -> UnderwritingResult:
        if not provider_gateway.underwriting_configured():
            raise ProviderAPIUnavailable(
                "核保补充材料功能API未完成：未配置有效的 UNDERWRITING_API_URL"
            )
        pending = self._pending_records.get(
            str(quotation.get("quotation_id") or "")
        )
        if not pending or not pending.provider_reference:
            raise ProviderAPIError("找不到待补充材料的核保申请")
        payload = {
            "quotation_id": quotation.get("quotation_id"),
            "provider_reference": pending.provider_reference,
            "quote_version": quotation.get("version"),
            "amount": quotation.get("total_premium"),
            "photo_base64": base64.b64encode(photo).decode("ascii")
            if photo
            else "",
            "video_base64": base64.b64encode(video).decode("ascii")
            if video
            else "",
        }
        response = await provider_gateway.request_underwriting(payload)
        raw_reference = str(
            response.data.get("provider_reference")
            or response.data.get("request_id")
            or ""
        ).strip()
        if not raw_reference:
            raise ProviderAPIError("补充材料API缺少 provider_reference")
        return UnderwritingResult(
            status=UnderwritingStatus.PENDING,
            message="补充材料已提交，等待保险公司正式回调。",
            provider_reference=raw_reference,
            request_digest=payload_digest(payload),
            raw_response=response.raw_response,
            quote_version=str(quotation.get("version") or ""),
            approved_amount=Decimal(str(quotation.get("total_premium") or "0")),
            checked_at=datetime.now(timezone.utc),
        )


underwriting_service = UnderwritingService()
