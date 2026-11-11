"""
车险出单智能体 - 用户端 API 路由
处理普通用户的聊天式出单交互
"""
import base64
import binascii
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel

from src.api.deps import read_upload
from src.api.schemas import InsuranceType, UserRole, WorkflowPhase
from src.core.engine import engine
from src.core.state import session_store
from src.knowledge.parser import knowledge_parser
from src.services.conversation_service import conversation_service
from src.services.client_crypto import ClientCryptoError, client_crypto_service
from src.services.provider_gateway import provider_gateway
from config.settings import settings

logger = logging.getLogger(__name__)

chat_router = APIRouter(prefix="/api/chat", tags=["用户端"])


async def _require_customer_session(request: Request, session_id: str):
    identity = getattr(request.state, "auth", None)
    if not identity or identity.role != "customer":
        raise HTTPException(status_code=403, detail="该接口仅限客户访问")
    session = await session_store.get_session(session_id)
    if not session or session.user_id != identity.subject:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


# ==================== 请求模型 ====================

class InitSessionRequest(BaseModel):
    agent_id: str
    user_id: str = ""


class SelectInsuranceTypeRequest(BaseModel):
    session_id: str
    insurance_type: str


class ReturningCustomerRequest(BaseModel):
    session_id: str
    plate: str = ""
    vin: str = ""


class ModifyPlanRequest(BaseModel):
    session_id: str
    user_input: str


class SessionActionRequest(BaseModel):
    session_id: str


class RollbackRequest(BaseModel):
    session_id: str
    target_phase: str


class ConversationMessageRequest(BaseModel):
    session_id: str
    message: str = ""
    history: list[dict[str, str]] | None = None
    encrypted_payload: dict[str, str] | None = None


class SecureFileRequest(BaseModel):
    session_id: str
    encrypted_payload: dict[str, str]


class SecureTextRequest(BaseModel):
    session_id: str
    encrypted_payload: dict[str, str]


class BatchDeleteSessionsRequest(BaseModel):
    session_ids: list[str]


def _decode_secure_file(
    payload: dict,
    key: str,
    max_mb: int,
) -> bytes:
    raw = str(payload.get(key) or "")
    if not raw:
        raise HTTPException(status_code=400, detail=f"缺少加密文件: {key}")
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail=f"加密文件编码无效: {key}")
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail=f"文件大小超过限制 {max_mb}MB"
        )
    return data


async def _decrypt_secure_files(
    request_data: SecureFileRequest,
    max_mb: int,
) -> dict:
    try:
        return client_crypto_service.decrypt_json(
            request_data.encrypted_payload,
            max_bytes=max_mb * 1024 * 1024 * 2 + 1024 * 1024,
        )
    except ClientCryptoError as e:
        raise HTTPException(status_code=400, detail=str(e))


class BatchQueryRequest(BaseModel):
    agent_id: str
    plates: list[str]


# ==================== 路由 ====================

@chat_router.post("/init")
async def init_session(req: InitSessionRequest, request: Request):
    """初始化会话 - 用户登录后自动携带业务员ID"""
    identity = getattr(request.state, "auth", None)
    if not identity:
        raise HTTPException(status_code=401, detail="请先登录")
    session = await session_store.create_session(
        agent_id=identity.agent_id,
        user_id=identity.subject,
        role=UserRole.CUSTOMER,
    )
    return {
        "success": True,
        "session_id": session.session_id,
        "message": "欢迎使用车险出单智能体！\n请选择保险类型：车险（目前仅支持车险）。",
        "available_types": [
            {"type": "car", "name": "车险", "available": True},
            {"type": "property", "name": "财险", "available": False},
            {"type": "life", "name": "人寿险", "available": False},
        ],
    }


@chat_router.get("/provider-status")
async def get_provider_status(request: Request):
    """返回真实报价、核保和支付 API 的配置状态。"""
    identity = getattr(request.state, "auth", None)
    if not identity or identity.role != "customer":
        raise HTTPException(status_code=403, detail="该接口仅限客户访问")
    status = {
        "quote": provider_gateway.quote_configured(),
        "underwriting": provider_gateway.underwriting_configured(),
        "payment": provider_gateway.payment_configured(),
    }
    labels = {
        "quote": "报价",
        "underwriting": "核保",
        "payment": "支付",
    }
    unavailable = [
        f"{labels[key]}API未完成"
        for key, ready in status.items()
        if not ready
    ]
    return {
        "success": True,
        "provider_status": status,
        "all_ready": all(status.values()),
        "message": "；".join(unavailable),
    }


@chat_router.post("/select-type")
async def select_insurance_type(req: SelectInsuranceTypeRequest, request: Request):
    """选择保险类型"""
    try:
        ins_type = InsuranceType(req.insurance_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"不支持的保险类型: {req.insurance_type}")

    await _require_customer_session(request, req.session_id)
    result = await engine.select_insurance_type(req.session_id, ins_type)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/upload-vehicle-license")
async def upload_vehicle_license(
    request: Request,
    session_id: str = Form(...),
    front_image: UploadFile = File(...),
    back_image: UploadFile = File(...),
):
    """新用户上传行驶证正副页"""
    await _require_customer_session(request, session_id)
    front_bytes = await read_upload(front_image)
    back_bytes = await read_upload(back_image)

    result = await engine.collect_info_new_customer(session_id, front_bytes, back_bytes)
    if result.get("transferred_to_human"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/secure/upload-vehicle-license")
async def upload_vehicle_license_secure(
    req: SecureFileRequest,
    request: Request,
):
    """前端 AES-GCM 加密上传行驶证。"""
    await _require_customer_session(request, req.session_id)
    payload = await _decrypt_secure_files(req, max_mb=20)
    front_bytes = _decode_secure_file(payload, "front_image", 10)
    back_bytes = _decode_secure_file(payload, "back_image", 10)
    result = await engine.collect_info_new_customer(
        req.session_id, front_bytes, back_bytes
    )
    if result.get("transferred_to_human"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/query-by-plate")
async def query_by_plate(req: ReturningCustomerRequest, request: Request):
    """老用户凭车牌号/车架号查询"""
    await _require_customer_session(request, req.session_id)
    result = await engine.collect_info_returning_customer(
        req.session_id, plate=req.plate, vin=req.vin
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/recommend")
async def generate_recommendation(req: SessionActionRequest, request: Request):
    """智能推荐投保方案"""
    await _require_customer_session(request, req.session_id)
    result = await engine.generate_recommendation(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/use-last-year")
async def use_last_year_plan(req: SessionActionRequest, request: Request):
    """老用户沿用去年的投保方案"""
    await _require_customer_session(request, req.session_id)
    result = await engine.use_last_year_plan(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/modify-plan")
async def modify_plan(req: ModifyPlanRequest, request: Request):
    """自然语言修改投保方案"""
    session = await _require_customer_session(request, req.session_id)
    result = await engine.modify_plan(req.session_id, req.user_input)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    session.append_conversation_message(
        "customer",
        req.user_input.strip(),
        sender_id=session.user_id,
        sender_name="客户",
    )
    session.append_conversation_message(
        "assistant",
        str(result["message"]),
        sender_name="智能体",
    )
    await session_store.save_session(session)
    return result


@chat_router.post("/secure/modify-plan")
async def modify_plan_secure(req: SecureTextRequest, request: Request):
    """前端加密的自然语言方案修改。"""
    session = await _require_customer_session(request, req.session_id)
    try:
        payload = client_crypto_service.decrypt_json(
            req.encrypted_payload, max_bytes=512 * 1024
        )
    except ClientCryptoError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user_input = str(payload.get("user_input") or "").strip()
    if not user_input:
        raise HTTPException(status_code=400, detail="修改内容不能为空")
    result = await engine.modify_plan(req.session_id, user_input)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    updated = await session_store.get_session(req.session_id) or session
    updated.append_conversation_message(
        "customer",
        user_input,
        sender_id=updated.user_id,
        sender_name="客户",
    )
    updated.append_conversation_message(
        "assistant",
        str(result["message"]),
        sender_name="智能体",
    )
    await session_store.save_session(updated)
    return result


@chat_router.post("/upload-id-card")
async def upload_id_card(
    request: Request,
    session_id: str = Form(...),
    front_image: UploadFile = File(...),
    back_image: UploadFile = File(...),
):
    """上传身份证正反面"""
    await _require_customer_session(request, session_id)
    front_bytes = await read_upload(front_image)
    back_bytes = await read_upload(back_image)

    result = await engine.upload_applicant_id_card(session_id, front_bytes, back_bytes)
    if result.get("transferred_to_human"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/secure/upload-id-card")
async def upload_id_card_secure(
    req: SecureFileRequest,
    request: Request,
):
    """前端 AES-GCM 加密上传身份证正反面。"""
    await _require_customer_session(request, req.session_id)
    payload = await _decrypt_secure_files(req, max_mb=20)
    front_bytes = _decode_secure_file(payload, "front_image", 10)
    back_bytes = _decode_secure_file(payload, "back_image", 10)
    result = await engine.upload_applicant_id_card(
        req.session_id, front_bytes, back_bytes
    )
    if result.get("transferred_to_human"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/generate-quotation")
async def generate_quotation(req: SessionActionRequest, request: Request):
    """生成报价单"""
    await _require_customer_session(request, req.session_id)
    result = await engine.generate_quotation(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/submit-underwriting")
async def submit_underwriting(req: SessionActionRequest, request: Request):
    """提交核保"""
    await _require_customer_session(request, req.session_id)
    result = await engine.submit_underwriting(req.session_id)
    if result.get("needs_verification"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/submit-verification")
async def submit_verification(
    request: Request,
    session_id: str = Form(...),
    photo: UploadFile = File(None),
    video: UploadFile = File(None),
):
    """补充提交验车材料"""
    await _require_customer_session(request, session_id)
    photo_bytes = await read_upload(photo, max_mb=10)
    video_bytes = await read_upload(video, max_mb=50)

    result = await engine.submit_verification_material(session_id, photo_bytes, video_bytes)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/secure/submit-verification")
async def submit_verification_secure(
    req: SecureFileRequest,
    request: Request,
):
    """前端 AES-GCM 加密上传验车照片或视频。"""
    await _require_customer_session(request, req.session_id)
    payload = await _decrypt_secure_files(req, max_mb=60)
    photo_bytes = (
        _decode_secure_file(payload, "photo", 10)
        if payload.get("photo")
        else None
    )
    video_bytes = (
        _decode_secure_file(payload, "video", 50)
        if payload.get("video")
        else None
    )
    if not photo_bytes and not video_bytes:
        raise HTTPException(status_code=400, detail="请至少上传照片或视频")
    result = await engine.submit_verification_material(
        req.session_id, photo_bytes, video_bytes
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/initiate-payment")
async def initiate_payment(req: SessionActionRequest, request: Request):
    """发起支付"""
    await _require_customer_session(request, req.session_id)
    result = await engine.initiate_payment(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/verify-payment")
async def verify_payment(req: SessionActionRequest, request: Request):
    """核验支付结果"""
    await _require_customer_session(request, req.session_id)
    result = await engine.verify_payment(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@chat_router.post("/rollback")
async def rollback_workflow(req: RollbackRequest, request: Request):
    """回退业务流程，并失效目标阶段之后的报价、核保和支付数据。"""
    await _require_customer_session(request, req.session_id)
    result = await engine.rollback_session(req.session_id, req.target_phase)
    if not result["success"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@chat_router.post("/message")
async def send_conversation_message(req: ConversationMessageRequest, request: Request):
    """统一处理用户自然语言问答和流程操作。"""
    session = await _require_customer_session(request, req.session_id)
    if settings.security.REQUIRE_CLIENT_ENCRYPTION and not req.encrypted_payload:
        raise HTTPException(status_code=400, detail="消息必须使用前端加密")
    if req.encrypted_payload:
        try:
            encrypted = client_crypto_service.decrypt_json(req.encrypted_payload)
            message = str(encrypted.get("message") or "")
            history = encrypted.get("history") or []
        except ClientCryptoError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        message = req.message
        history = req.history or []
    if not message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    clean_message = message.strip()
    if session.phase == WorkflowPhase.TRANSFERRED_TO_HUMAN:
        session.append_conversation_message(
            "customer",
            clean_message,
            sender_id=session.user_id,
            sender_name="客户",
        )
        await session_store.save_session(session)
        return {
            "success": True,
            "action": "waiting_human",
            "message": "",
            "trace": [],
            "phase": session.phase.value,
            "conversation_messages": (session.conversation_messages or [])[-50:],
        }
    session.append_conversation_message(
        "customer",
        clean_message,
        sender_id=session.user_id,
        sender_name="客户",
    )
    await session_store.save_session(session)

    result = await conversation_service.respond(
        session,
        clean_message,
        history,
    )
    updated = await session_store.get_session(session.session_id) or session
    if result.get("message"):
        updated.append_conversation_message(
            "assistant",
            str(result["message"]),
            sender_name="智能体",
            metadata={
                "knowledge_sources": result.get("knowledge_sources") or []
            },
        )
    await session_store.save_session(updated)
    result["conversation_messages"] = (updated.conversation_messages or [])[-100:]
    return result


@chat_router.get("/session/{session_id}")
async def get_session_status(session_id: str, request: Request):
    """获取会话当前状态"""
    session = await _require_customer_session(request, session_id)

    return {
        "success": True,
        "session": session.to_dict(),
        "phase": session.phase.value,
        "plate": session.license_plate,
        "vehicle": session.vehicle_info.to_dict() if session.vehicle_info else None,
        "applicant": (
            session.applicant_id_card.to_dict()
            if session.applicant_id_card
            else None
        ),
        "plan": session.insurance_plan.to_dict() if session.insurance_plan else None,
        "quotation": session.quotation.to_dict() if session.quotation else None,
        "underwriting": (
            session.underwriting_result.to_dict()
            if session.underwriting_result
            else None
        ),
        "payment": (
            session.payment_result.to_dict() if session.payment_result else None
        ),
        "policy": session.policy.to_dict() if session.policy else None,
        "conversation_messages": (session.conversation_messages or [])[-200:],
    }


@chat_router.get("/sessions/recent")
async def list_recent_customer_sessions(request: Request, limit: int = 10):
    """返回当前客户最近的可恢复会话，供刷新后恢复聊天记忆。"""
    identity = getattr(request.state, "auth", None)
    if not identity or identity.role != "customer":
        raise HTTPException(status_code=403, detail="该接口仅限客户访问")
    limit = max(1, min(limit, 30))
    sessions = await session_store.get_user_sessions(identity.subject)
    resumable = [
        session
        for session in sessions
        if session.conversation_messages
        or session.vehicle_info
        or session.insurance_plan
    ]
    resumable.sort(key=lambda item: item.updated_at, reverse=True)
    return {
        "success": True,
        "sessions": [
            {
                "session_id": session.session_id,
                "phase": session.phase.value,
                "plate": session.license_plate,
                "plan_total": (
                    str(session.insurance_plan.total_premium)
                    if session.insurance_plan
                    else None
                ),
                "updated_at": session.updated_at.isoformat(),
                "message_count": len(session.conversation_messages or []),
                "last_message": (
                    str(session.conversation_messages[-1].get("content") or "")[:120]
                    if session.conversation_messages
                    else ""
                ),
            }
            for session in resumable[:limit]
        ],
    }


@chat_router.delete("/sessions/{session_id}")
async def delete_customer_session(session_id: str, request: Request):
    """删除当前客户自己的会话记录。"""
    await _require_customer_session(request, session_id)
    await session_store.remove_session(session_id)
    return {"success": True, "session_id": session_id}


@chat_router.post("/sessions/batch-delete")
async def batch_delete_customer_sessions(
    req: BatchDeleteSessionsRequest,
    request: Request,
):
    """批量删除当前客户的会话记录，无法删除的会话会单独返回。"""
    identity = getattr(request.state, "auth", None)
    if not identity or identity.role != "customer":
        raise HTTPException(status_code=403, detail="该接口仅限客户访问")
    session_ids = list(dict.fromkeys(item for item in req.session_ids if item))
    deleted: list[str] = []
    skipped: list[str] = []
    for session_id in session_ids:
        session = await session_store.get_session(session_id)
        if not session or session.user_id != identity.subject:
            skipped.append(session_id)
            continue
        await session_store.remove_session(session_id)
        deleted.append(session_id)
    return {
        "success": True,
        "deleted": deleted,
        "skipped": skipped,
        "deleted_count": len(deleted),
    }


@chat_router.get("/session/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    request: Request,
    after: str = "",
):
    """获取人工接管期间的会话消息，用于客户页轮询。"""
    session = await _require_customer_session(request, session_id)
    messages = session.conversation_messages or []
    if after:
        messages = [
            message
            for message in messages
            if str(message.get("created_at") or "") > after
        ]
    return {
        "success": True,
        "messages": messages[-100:],
        "phase": session.phase.value,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@chat_router.post("/batch-query")
async def batch_query_plates(req: BatchQueryRequest):
    """批量查询车牌号状态"""
    result = await engine.batch_query_by_plates(req.agent_id, req.plates)
    return result


@chat_router.get("/knowledge/search")
async def search_knowledge(q: str = ""):
    """知识库查询 - 行业术语对齐"""
    if not q:
        return {"success": True, "terminology": list(knowledge_parser._terminology.keys())}

    matches = knowledge_parser.find_matches(q)
    normalized = knowledge_parser.normalize(q)

    return {
        "success": True,
        "query": q,
        "normalized": normalized,
        "matched_terms": matches,
    }
