"""真实保险公司支付服务。

支付订单和成功状态均来自保险公司/支付平台 API 与正式回调，
不存在开发环境自动支付成功分支。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from config.settings import settings
from src.api.schemas import PaymentResult, PaymentStatus
from src.services.provider_gateway import (
    CallbackValidationError,
    ProviderAPIError,
    ProviderAPIUnavailable,
    provider_gateway,
)

logger = logging.getLogger(__name__)


class PaymentService:
    """支付服务：创建真实支付订单并等待签名回调。"""

    def __init__(self):
        self._transactions: dict[str, PaymentResult] = {}

    def _signing_key(self) -> str:
        return (
            settings.api.PAYMENT_API_KEY
            or settings.security.SECRET_KEY
            or "dev-secret"
        )

    @staticmethod
    def _sign(
        transaction_id: str,
        provider_reference: str,
        status: str,
        amount: str,
        quote_version: str,
        key: str,
    ) -> str:
        payload = (
            f"{transaction_id}:{provider_reference}:{status}:"
            f"{amount}:{quote_version}"
        )
        return hmac.new(
            key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def sign_callback(
        self,
        transaction_id: str,
        provider_reference: str,
        status: str,
        amount: str,
        quote_version: str,
    ) -> str:
        return self._sign(
            transaction_id,
            provider_reference,
            status,
            amount,
            quote_version,
            self._signing_key(),
        )

    def verify_callback_signature(
        self,
        transaction_id: str,
        provider_reference: str,
        status: str,
        amount: str,
        quote_version: str,
        signature: Optional[str],
    ) -> bool:
        if not signature:
            return False
        expected = self.sign_callback(
            transaction_id,
            provider_reference,
            status,
            amount,
            quote_version,
        )
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _parse_amount(value: Any) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ProviderAPIError("支付金额格式不正确") from exc
        return amount.quantize(Decimal("0.01"))

    async def create_payment(
        self,
        quotation: dict[str, Any],
    ) -> PaymentResult:
        if not provider_gateway.payment_configured():
            raise ProviderAPIUnavailable(
                "支付功能API未完成：未配置有效的 PAYMENT_API_URL"
            )

        quotation_id = str(quotation.get("quotation_id") or "")
        quote_version = str(quotation.get("version") or "")
        amount = self._parse_amount(quotation.get("total_premium"))
        quote_reference = str(quotation.get("provider_reference") or "")
        if not quotation_id or not quote_version or not quote_reference:
            raise ProviderAPIError("报价单缺少支付所需信息")

        payload = {
            "quotation_id": quotation_id,
            "quote_version": quote_version,
            "quote_provider_reference": quote_reference,
            "amount": str(amount),
            "plan": quotation.get("plan"),
            "applicant": quotation.get("applicant"),
        }
        response = await provider_gateway.request_payment(payload)
        data = response.data
        transaction_id = str(data.get("transaction_id") or "").strip()
        payment_url = str(data.get("payment_url") or "").strip()
        provider_reference = str(
            data.get("provider_reference") or ""
        ).strip()
        if not transaction_id or not payment_url:
            raise ProviderAPIError("支付API缺少 transaction_id 或 payment_url")
        if not provider_reference:
            raise ProviderAPIError("支付API缺少 provider_reference")
        if str(data.get("quote_version") or "") != quote_version:
            raise ProviderAPIError("支付API返回的报价版本不一致")
        returned_amount = self._parse_amount(data.get("amount", amount))
        if returned_amount != amount:
            raise ProviderAPIError("支付API返回的金额与报价金额不一致")

        payment = PaymentResult(
            status=PaymentStatus.PROCESSING,
            transaction_id=transaction_id,
            payment_url=payment_url,
            paid_amount=returned_amount,
            quotation_id=quotation_id,
            quote_version=quote_version,
            expected_amount=amount,
            provider_reference=provider_reference,
            raw_response=response.raw_response,
        )
        self._transactions[transaction_id] = payment
        return payment

    def record_callback(
        self,
        transaction_id: str,
        provider_reference: str,
        status: str,
        amount: str,
        quote_version: str,
        signature: Optional[str],
        expected_provider_reference: str = "",
        expected_amount: str = "",
        expected_quote_version: str = "",
    ) -> PaymentResult:
        if not self.verify_callback_signature(
            transaction_id,
            provider_reference,
            status,
            amount,
            quote_version,
            signature,
        ):
            raise CallbackValidationError("支付回调签名无效")

        payment = self._transactions.get(transaction_id)
        expected_reference = (
            payment.provider_reference
            if payment
            else expected_provider_reference
        )
        expected_amount_value = (
            str(payment.expected_amount)
            if payment
            else expected_amount
        )
        expected_version = (
            payment.quote_version if payment else expected_quote_version
        )
        if not expected_reference or provider_reference != expected_reference:
            raise CallbackValidationError("支付回调流水号与创建订单不一致")
        if not expected_version or quote_version != expected_version:
            raise CallbackValidationError("支付回调报价版本与创建订单不一致")
        if not expected_amount_value:
            raise CallbackValidationError("缺少支付金额校验基准")
        callback_amount = self._parse_amount(amount)
        if callback_amount != self._parse_amount(expected_amount_value):
            raise CallbackValidationError("支付回调金额与订单金额不一致")

        if payment is None:
            payment = PaymentResult(
                transaction_id=transaction_id,
                quotation_id="",
                quote_version=quote_version,
                expected_amount=callback_amount,
                paid_amount=callback_amount,
                provider_reference=provider_reference,
            )
            self._transactions[transaction_id] = payment

        normalized_status = (status or "").strip().lower()
        if normalized_status == "success":
            payment.status = PaymentStatus.SUCCESS
            payment.paid_amount = callback_amount
            payment.paid_at = datetime.now(timezone.utc)
        elif normalized_status == "failed":
            payment.status = PaymentStatus.FAILED
            payment.paid_at = datetime.now(timezone.utc)
        elif normalized_status == "pending":
            payment.status = PaymentStatus.PENDING
        else:
            raise CallbackValidationError("支付回调状态不合法")
        payment.raw_response = json.dumps(
            {
                "callback_status": status,
                "amount": amount,
                "quote_version": quote_version,
            },
            ensure_ascii=False,
        )
        return payment

    def verify_payment(
        self,
        transaction_id: str,
        expected_amount: str = "",
        expected_quote_version: str = "",
        expected_provider_reference: str = "",
        quotation_id: str = "",
    ) -> PaymentResult:
        """只读取已确认的支付状态，绝不自动把待支付改为成功。"""
        payment = self._transactions.get(transaction_id)
        if payment is None:
            return PaymentResult(
                status=PaymentStatus.PENDING,
                transaction_id=transaction_id,
                quotation_id=quotation_id,
                quote_version=expected_quote_version,
                expected_amount=Decimal(expected_amount or "0"),
                provider_reference=expected_provider_reference,
            )
        if payment.status == PaymentStatus.SUCCESS:
            if expected_amount and payment.paid_amount != Decimal(
                expected_amount
            ).quantize(Decimal("0.01")):
                return PaymentResult(
                    status=PaymentStatus.FAILED,
                    transaction_id=transaction_id,
                    quotation_id=quotation_id,
                    quote_version=expected_quote_version,
                    expected_amount=Decimal(expected_amount),
                    provider_reference=expected_provider_reference,
                )
            if (
                expected_quote_version
                and payment.quote_version != expected_quote_version
            ):
                return PaymentResult(
                    status=PaymentStatus.FAILED,
                    transaction_id=transaction_id,
                    quotation_id=quotation_id,
                    quote_version=expected_quote_version,
                    expected_amount=Decimal(expected_amount or "0"),
                    provider_reference=expected_provider_reference,
                )
        return payment

    def get_payment_status(self, transaction_id: str) -> PaymentStatus:
        payment = self._transactions.get(transaction_id)
        return payment.status if payment else PaymentStatus.PENDING


payment_service = PaymentService()
