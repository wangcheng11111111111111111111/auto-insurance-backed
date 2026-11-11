"""
车险出单智能体 - 第三方回调接口（Webhook）
接收支付平台 / 保险公司的异步结果通知，均需携带 HMAC-SHA256 签名，
签名校验失败返回 400，防止伪造回调导致「未支付即出单」或「未核保即出单」。
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.services.payment_service import payment_service
from src.services.underwriting_service import underwriting_service
from src.services.provider_gateway import CallbackValidationError, ProviderAPIError
from src.core.engine import engine
from src.core.state import session_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["webhooks"])


class PaymentCallbackRequest(BaseModel):
    transaction_id: str
    provider_reference: str = ""
    status: str = "success"
    amount: str = ""
    quote_version: str = ""
    signature: str = ""


class UnderwritingCallbackRequest(BaseModel):
    quotation_id: str
    provider_reference: str = ""
    status: str = "approved"
    message: str = ""
    amount: str = ""
    quote_version: str = ""
    signature: str = ""


@router.post("/payment/callback")
async def payment_callback(req: PaymentCallbackRequest):
    """支付平台回调：签名校验通过后标记交易为已支付。"""
    session = await session_store.find_by_transaction_id(req.transaction_id)
    if not session or not session.quotation or not session.payment_result:
        raise HTTPException(status_code=404, detail="支付订单不存在")
    try:
        payment = payment_service.record_callback(
            req.transaction_id,
            req.provider_reference,
            req.status,
            req.amount,
            req.quote_version,
            req.signature,
            expected_provider_reference=session.payment_result.provider_reference,
            expected_amount=str(session.payment_result.expected_amount),
            expected_quote_version=session.quotation.version,
        )
    except (CallbackValidationError, ProviderAPIError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": True,
        "message": "ok",
        "payment": payment.to_dict(),
    }


@router.post("/underwriting/callback")
async def underwriting_callback(req: UnderwritingCallbackRequest):
    """保险公司回调：签名校验通过后更新核保状态，并推进对应会话。"""
    session = await session_store.find_by_quotation_id(req.quotation_id)
    if not session or not session.quotation:
        raise HTTPException(status_code=404, detail="报价单不存在")
    expected_amount = (
        str(session.quotation.plan.total_premium)
        if session.quotation.plan
        else ""
    )
    pending_reference = (
        session.underwriting_result.provider_reference
        if session.underwriting_result
        else ""
    )
    try:
        result = underwriting_service.record_callback(
            req.quotation_id,
            req.provider_reference,
            req.status,
            req.message,
            req.amount,
            req.quote_version,
            req.signature,
            expected_provider_reference=pending_reference,
            expected_amount=expected_amount,
            expected_quote_version=session.quotation.version,
        )
    except CallbackValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    applied = await engine.apply_underwriting_callback(
        req.quotation_id, result
    )
    if not applied:
        raise HTTPException(status_code=409, detail="核保回调业务字段不一致")
    return {"success": True, "message": "ok"}
