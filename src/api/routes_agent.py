"""
车险出单智能体 - 业务员端 API 路由
业务员可以批量操作出单流程
"""
import asyncio
import json
import logging
import re
from io import BytesIO
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pydantic import BaseModel

from src.api.deps import read_upload
from src.api.schemas import (
    IDCardInfo,
    InsuranceType,
    UserRole,
    VehicleInfo,
    WorkflowPhase,
)
from src.core.engine import engine
from src.core.state import session_store
from src.services.notification import notification_hub
from src.storage.mysql import get_db
from src.storage.repository import TakeoverRepository
from src.utils.plate_validator import is_valid_plate, normalize_plate

logger = logging.getLogger(__name__)

agent_router = APIRouter(prefix="/api/agent", tags=["业务员端"])

_REPORT_PHASE_LABELS = {
    "insurance_type_selection": "选择保险类型",
    "collecting_info": "收集车辆信息",
    "recommending": "智能推荐方案",
    "identity_verification": "身份验证",
    "quoting": "生成报价",
    "underwriting": "核保",
    "payment": "支付",
    "completed": "投保完成",
    "transferred_to_human": "人工接管",
}


def _takeover_status_label(status: str) -> str:
    return {
        "pending": "待认领",
        "processing": "处理中",
        "resolved": "已办结",
    }.get(status, "")


def _excel_datetime(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _sse_event(data: dict) -> str:
    """构造一条 SSE 事件帧（data: {...}\n\n）。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _identity(request: Request):
    identity = getattr(request.state, "auth", None)
    if not identity or identity.role != "agent":
        raise HTTPException(status_code=403, detail="该接口仅限业务员访问")
    return identity


async def _require_agent_session(request: Request, session_id: str):
    identity = _identity(request)
    session = await session_store.get_session(session_id)
    if not session or session.agent_id != identity.agent_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


async def _takeover_payload(record) -> dict:
    session = await session_store.get_session(record.session_id)
    requested_at = record.requested_at
    ended_at = record.resolved_at or datetime.now()
    waiting_seconds = (
        max(0, int((ended_at - requested_at).total_seconds()))
        if requested_at
        else 0
    )
    return {
        "id": record.id,
        "transfer_id": record.transfer_id,
        "session_id": record.session_id,
        "reason": record.reason,
        "source_phase": record.source_phase,
        "handler_id": record.handler_id or "",
        "handle_status": record.handle_status,
        "handle_remark": record.handle_remark or "",
        "requested_at": requested_at.isoformat() if requested_at else None,
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
        "waiting_seconds": waiting_seconds,
        "plate": session.license_plate if session else "",
        "phase": session.phase.value if session else "",
    }


async def _complete_manual_review_ticket(
    session_id: str,
    identity,
    remark: str,
) -> bool:
    """人工补录完成后办结当前业务员持有的接管工单。"""
    resolved = False
    try:
        async with get_db() as db:
            record = await TakeoverRepository.get_active_by_session(db, session_id)
            if (
                record
                and record.handle_status == "processing"
                and record.handler_id == identity.agent_id
            ):
                resolved = await TakeoverRepository.resolve(
                    db,
                    id=record.id,
                    handler_id=identity.agent_id,
                    remark=remark,
                )
    except Exception as e:
        logger.warning("人工审核完成后办结工单失败: %s", e)
        return False

    if resolved:
        try:
            await notification_hub.broadcast_all(
                {
                    "type": "takeover_resolved",
                    "session_id": session_id,
                    "handler_id": identity.agent_id,
                }
            )
        except Exception:
            pass
    return resolved


async def _require_manual_review_owner(session_id: str, identity) -> None:
    """存在活动工单时，只有认领它的业务员可以补录审核。"""
    try:
        async with get_db() as db:
            record = await TakeoverRepository.get_active_by_session(db, session_id)
    except Exception as e:
        logger.warning("校验人工审核权限失败: %s", e)
        raise HTTPException(status_code=503, detail="数据库不可用，请稍后重试")
    if record and (
        record.handle_status != "processing"
        or record.handler_id != identity.agent_id
    ):
        raise HTTPException(status_code=403, detail="请先认领该工单再进行人工审核")


# ==================== 请求模型 ====================

class AgentInitRequest(BaseModel):
    agent_id: str


class AgentSelectTypeRequest(BaseModel):
    session_id: str
    insurance_type: str


class AgentPlateRequest(BaseModel):
    session_id: str
    plate: str = ""
    vin: str = ""


class AgentInputRequest(BaseModel):
    session_id: str
    user_input: str


class AgentSessionRequest(BaseModel):
    session_id: str


class AgentBatchPlatesRequest(BaseModel):
    agent_id: str
    plates: list[str]


class ResolveTakeoverRequest(BaseModel):
    handler_id: str
    remark: str = ""


class ManualVehicleRequest(BaseModel):
    license_plate: str
    vin: str
    brand_model: str
    owner_name: str
    registration_date: str = ""
    use_type: str = "非营运"
    seat_count: int = 5
    vehicle_type: str = "小型轿车"
    engine_number: str = ""


class ManualApplicantRequest(BaseModel):
    name: str
    id_number: str


class AgentChatMessageRequest(BaseModel):
    content: str


# ==================== 路由 ====================

@agent_router.post("/init")
async def agent_init_session(req: AgentInitRequest, request: Request):
    """业务员初始化会话"""
    identity = _identity(request)
    session = await session_store.create_session(
        agent_id=identity.agent_id,
        role=UserRole.AGENT,
    )
    return {
        "success": True,
        "session_id": session.session_id,
        "message": "业务员工作台已就绪。请选择保险类型：车险。",
        "available_types": [
            {"type": "car", "name": "车险", "available": True},
            {"type": "property", "name": "财险", "available": False},
            {"type": "life", "name": "人寿险", "available": False},
        ],
    }


@agent_router.post("/select-type")
async def agent_select_type(req: AgentSelectTypeRequest, request: Request):
    """业务员选择保险类型"""
    try:
        ins_type = InsuranceType(req.insurance_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"不支持的保险类型: {req.insurance_type}")

    await _require_agent_session(request, req.session_id)
    result = await engine.select_insurance_type(req.session_id, ins_type)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/upload-vehicle-license")
async def agent_upload_vehicle_license(
    request: Request,
    session_id: str = Form(...),
    front_image: UploadFile = File(...),
    back_image: UploadFile = File(...),
):
    """业务员代新用户上传行驶证"""
    await _require_agent_session(request, session_id)
    front_bytes = await read_upload(front_image)
    back_bytes = await read_upload(back_image)

    result = await engine.collect_info_new_customer(session_id, front_bytes, back_bytes)
    if result.get("transferred_to_human"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/query-by-plate")
async def agent_query_by_plate(req: AgentPlateRequest, request: Request):
    """业务员凭车牌号/车架号查询"""
    await _require_agent_session(request, req.session_id)
    result = await engine.collect_info_returning_customer(
        req.session_id, plate=req.plate, vin=req.vin
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/recommend")
async def agent_recommend(req: AgentSessionRequest, request: Request):
    """业务员触发智能推荐"""
    await _require_agent_session(request, req.session_id)
    result = await engine.generate_recommendation(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/use-last-year")
async def agent_use_last_year(req: AgentSessionRequest, request: Request):
    """业务员确认沿用去年的投保方案"""
    await _require_agent_session(request, req.session_id)
    result = await engine.use_last_year_plan(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/modify-plan")
async def agent_modify_plan(req: AgentInputRequest, request: Request):
    """业务员修改投保方案"""
    await _require_agent_session(request, req.session_id)
    result = await engine.modify_plan(req.session_id, req.user_input)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/upload-id-card")
async def agent_upload_id_card(
    request: Request,
    session_id: str = Form(...),
    front_image: UploadFile = File(...),
    back_image: UploadFile = File(...),
):
    """业务员代用户上传身份证"""
    await _require_agent_session(request, session_id)
    front_bytes = await read_upload(front_image)
    back_bytes = await read_upload(back_image)

    result = await engine.upload_applicant_id_card(session_id, front_bytes, back_bytes)
    if result.get("transferred_to_human"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/generate-quotation")
async def agent_generate_quotation(req: AgentSessionRequest, request: Request):
    """业务员触发生成报价单"""
    await _require_agent_session(request, req.session_id)
    result = await engine.generate_quotation(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/submit-underwriting")
async def agent_submit_underwriting(req: AgentSessionRequest, request: Request):
    """业务员触发核保"""
    await _require_agent_session(request, req.session_id)
    result = await engine.submit_underwriting(req.session_id)
    if result.get("needs_verification"):
        return result
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/submit-verification")
async def agent_submit_verification(
    request: Request,
    session_id: str = Form(...),
    photo: UploadFile = File(None),
    video: UploadFile = File(None),
):
    """业务员代用户提交验车材料"""
    await _require_agent_session(request, session_id)
    photo_bytes = await read_upload(photo, max_mb=10)
    video_bytes = await read_upload(video, max_mb=50)

    result = await engine.submit_verification_material(session_id, photo_bytes, video_bytes)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/initiate-payment")
async def agent_initiate_payment(req: AgentSessionRequest, request: Request):
    """业务员发起支付"""
    await _require_agent_session(request, req.session_id)
    result = await engine.initiate_payment(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@agent_router.post("/verify-payment")
async def agent_verify_payment(req: AgentSessionRequest, request: Request):
    """业务员核验支付"""
    await _require_agent_session(request, req.session_id)
    result = await engine.verify_payment(req.session_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


# ==================== 批量操作 ====================

@agent_router.post("/batch/query")
async def batch_query(req: AgentBatchPlatesRequest, request: Request):
    """批量查询车牌号对应的会话状态"""
    identity = _identity(request)
    result = await engine.batch_query_by_plates(identity.agent_id, req.plates)
    return result


@agent_router.post("/batch/create")
async def batch_create(req: AgentBatchPlatesRequest, request: Request):
    """批量创建会话"""
    identity = _identity(request)
    result = await engine.batch_create_sessions(identity.agent_id, req.plates)
    return result


@agent_router.get("/sessions")
async def list_agent_sessions(request: Request):
    """查看业务员的所有活跃会话"""
    identity = _identity(request)
    agent_id = identity.agent_id
    sessions = await session_store.get_agent_sessions(agent_id)
    return {
        "success": True,
        "agent_id": agent_id,
        "total": len(sessions),
        "sessions": [
            {
                "session_id": s.session_id,
                "plate": s.license_plate,
                "phase": s.phase.value,
                "plan_total": str(s.insurance_plan.total_premium) if s.insurance_plan else None,
                "updated_at": s.updated_at.isoformat(),
                "message_count": len(s.conversation_messages or []),
                "last_message": (
                    str(s.conversation_messages[-1].get("content") or "")[:80]
                    if s.conversation_messages
                    else ""
                ),
            }
            for s in sessions
        ],
    }


@agent_router.get("/reports/sessions.xlsx")
async def export_agent_sessions_report(
    request: Request,
    phase: str = "all",
    takeover_status: str = "all",
    keyword: str = "",
    hide_empty: bool = False,
):
    """导出当前业务员名下会话与人工接管信息为 Excel 报表。"""
    identity = _identity(request)
    sessions = await session_store.get_agent_sessions(identity.agent_id)
    try:
        async with get_db() as db:
            takeover_records = await TakeoverRepository.list_by_status(
                db,
                status=takeover_status
                if takeover_status in {"pending", "processing", "resolved"}
                else "all",
                limit=10000,
            )
    except Exception as e:
        logger.warning("导出报表查询人工工单失败: %s", e)
        raise HTTPException(status_code=503, detail="数据库不可用，无法导出报表")

    takeover_by_session = {
        item.session_id: item for item in takeover_records
    }
    search = keyword.strip().lower()
    filtered = []
    for session in sessions:
        takeover = takeover_by_session.get(session.session_id)
        if takeover_status != "all" and not takeover:
            continue
        if phase != "all" and session.phase.value != phase:
            continue
        searchable = [
            session.license_plate,
            session.session_id,
            session.user_id,
            session.vehicle_info.brand_model if session.vehicle_info else "",
            session.vehicle_info.vin if session.vehicle_info else "",
            session.applicant_id_card.name if session.applicant_id_card else "",
            (
                str(session.conversation_messages[-1].get("content") or "")
                if session.conversation_messages
                else ""
            ),
        ]
        if search and not any(search in str(value).lower() for value in searchable):
            continue
        if (
            hide_empty
            and not session.license_plate
            and not session.conversation_messages
            and not session.insurance_plan
        ):
            continue
        filtered.append((session, takeover))

    filtered.sort(key=lambda item: item[0].updated_at, reverse=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "会话报表"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A5"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.sheet_properties.pageSetUpPr.fitToPage = True

    headers = [
        "序号",
        "会话ID",
        "客户ID",
        "车牌号",
        "流程阶段",
        "客户类型",
        "品牌型号",
        "车架号",
        "投保人",
        "投保人证件号",
        "保险公司",
        "险种数量",
        "方案总保费",
        "报价版本",
        "费率来源",
        "核保状态",
        "支付状态",
        "保单号",
        "接管状态",
        "接管原因",
        "处理人",
        "接管请求时间",
        "更新时间",
        "最近沟通",
        "消息数",
    ]
    last_column = get_column_letter(len(headers))
    sheet.merge_cells(f"A1:{last_column}1")
    title = sheet["A1"]
    title.value = f"业务员会话报表 · {identity.name or identity.agent_id}"
    title.font = Font(size=16, bold=True, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor="1D4ED8")
    title.alignment = Alignment(horizontal="left", vertical="center")
    sheet.row_dimensions[1].height = 28

    pending_count = sum(
        1
        for _, takeover in filtered
        if takeover and takeover.handle_status == "pending"
    )
    processing_count = sum(
        1
        for _, takeover in filtered
        if takeover and takeover.handle_status == "processing"
    )
    premium_total = sum(
        (
            session.insurance_plan.total_premium
            if session.insurance_plan
            else Decimal("0")
        )
        for session, _ in filtered
    )
    filter_text = (
        f"阶段：{phase if phase != 'all' else '全部'} · "
        f"工单：{takeover_status if takeover_status != 'all' else '全部'} · "
        f"关键词：{keyword or '无'}"
    )
    summary_values = [
        "会话总数",
        len(filtered),
        "待认领",
        pending_count,
        "处理中",
        processing_count,
        "方案总保费",
        float(premium_total),
        "筛选条件",
        filter_text,
    ]
    for index, value in enumerate(summary_values, start=1):
        cell = sheet.cell(row=2, column=index, value=value)
        if index in {1, 3, 5, 7, 9}:
            cell.font = Font(bold=True, color="475569")
            cell.fill = PatternFill("solid", fgColor="E2E8F0")
        else:
            cell.font = Font(bold=True, color="0F172A")
    sheet["H2"].number_format = '"¥"#,##0.00'
    sheet.merge_cells("J2:Y2")
    sheet["J2"].alignment = Alignment(horizontal="left")

    header_fill = PatternFill("solid", fgColor="DBEAFE")
    header_font = Font(bold=True, color="1E3A8A")
    thin = Side(style="thin", color="CBD5E1")
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=4, column=column, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=thin)
    sheet.row_dimensions[4].height = 30

    for row_index, (session, takeover) in enumerate(filtered, start=5):
        vehicle = session.vehicle_info
        applicant = session.applicant_id_card
        plan = session.insurance_plan
        quotation = session.quotation
        underwriting = session.underwriting_result
        payment = session.payment_result
        policy = session.policy
        last_message = (
            str(session.conversation_messages[-1].get("content") or "")
            if session.conversation_messages
            else ""
        )
        values = [
            row_index - 4,
            session.session_id,
            session.user_id,
            session.license_plate,
            _REPORT_PHASE_LABELS.get(session.phase.value, session.phase.value),
            "老客户" if session.customer_type.value == "returning" else "新客户",
            vehicle.brand_model if vehicle else "",
            vehicle.vin if vehicle else "",
            applicant.name if applicant else "",
            applicant.to_dict().get("id_number", "") if applicant else "",
            plan.company.name if plan and plan.company else "",
            len(plan.items) if plan else 0,
            float(plan.total_premium) if plan else 0,
            quotation.version if quotation else "",
            quotation.rate_source if quotation else "",
            underwriting.status.value if underwriting else "",
            payment.status.value if payment else "",
            policy.policy_number if policy else "",
            _takeover_status_label(takeover.handle_status if takeover else ""),
            takeover.reason if takeover else "",
            takeover.handler_id if takeover else "",
            _excel_datetime(takeover.requested_at) if takeover else None,
            _excel_datetime(session.updated_at),
            last_message,
            len(session.conversation_messages or []),
        ]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=column, value=value)
            cell.border = Border(bottom=Side(style="hair", color="E2E8F0"))
            cell.alignment = Alignment(vertical="top", wrap_text=column in {7, 15, 20, 24})
            if row_index % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F8FAFC")
        sheet.cell(row=row_index, column=13).number_format = '"¥"#,##0.00'
        for column in (22, 23):
            sheet.cell(row=row_index, column=column).number_format = "yyyy-mm-dd hh:mm"

    if not filtered:
        sheet.merge_cells(f"A5:{last_column}5")
        sheet["A5"] = "当前筛选条件下没有可导出的会话"
        sheet["A5"].alignment = Alignment(horizontal="center")
        sheet["A5"].font = Font(color="64748B")

    widths = {
        1: 7,
        2: 38,
        3: 18,
        4: 14,
        5: 16,
        6: 10,
        7: 24,
        8: 20,
        9: 12,
        10: 20,
        11: 16,
        12: 10,
        13: 14,
        14: 14,
        15: 28,
        16: 16,
        17: 14,
        18: 24,
        19: 12,
        20: 38,
        21: 16,
        22: 18,
        23: 18,
        24: 42,
        25: 10,
    }
    for column, width in widths.items():
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.auto_filter.ref = f"A4:{last_column}{max(4, len(filtered) + 4)}"

    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    filename = f"agent-sessions-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    return StreamingResponse(
        stream,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@agent_router.get("/session/{session_id}")
async def get_session_detail(session_id: str, request: Request):
    """查看会话详情"""
    session = await _require_agent_session(request, session_id)

    return {
        "success": True,
        "session": session.to_dict(),
        "phase": session.phase.value,
        "plate": session.license_plate,
        "vehicle": session.vehicle_info.to_dict() if session.vehicle_info else None,
        "applicant": session.applicant_id_card.to_dict() if session.applicant_id_card else None,
        "plan": session.insurance_plan.to_dict() if session.insurance_plan else None,
        "quotation": session.quotation.to_dict() if session.quotation else None,
        "underwriting": session.underwriting_result.to_dict() if session.underwriting_result else None,
        "payment": session.payment_result.to_dict() if session.payment_result else None,
        "policy": session.policy.to_dict() if session.policy else None,
        "conversation_messages": (session.conversation_messages or [])[-100:],
    }


@agent_router.post("/session/{session_id}/manual-vehicle")
async def update_vehicle_manually(
    session_id: str,
    req: ManualVehicleRequest,
    request: Request,
):
    """人工补录车辆信息，并恢复到智能推荐阶段。"""
    session = await _require_agent_session(request, session_id)
    identity = _identity(request)
    await _require_manual_review_owner(session_id, identity)
    plate = normalize_plate(req.license_plate)
    vin = req.vin.strip().upper()
    if not is_valid_plate(plate):
        raise HTTPException(status_code=400, detail="车牌号格式不正确")
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise HTTPException(status_code=400, detail="车架号必须为 17 位")
    if not req.brand_model.strip() or not req.owner_name.strip():
        raise HTTPException(status_code=400, detail="品牌型号和所有人不能为空")
    registration_date = None
    if req.registration_date:
        try:
            registration_date = datetime.strptime(
                req.registration_date, "%Y-%m-%d"
            ).date()
        except ValueError:
            raise HTTPException(status_code=400, detail="注册日期格式应为 YYYY-MM-DD")

    session.vehicle_info = VehicleInfo(
        license_plate=plate,
        vin=vin,
        engine_number=req.engine_number.strip().upper(),
        brand_model=req.brand_model.strip(),
        registration_date=registration_date,
        owner_name=req.owner_name.strip(),
        use_type=req.use_type.strip() or "非营运",
        seat_count=max(1, min(req.seat_count, 60)),
        vehicle_type=req.vehicle_type.strip() or "小型轿车",
    )
    session.set_license_plate(plate)
    session.advance_phase(WorkflowPhase.RECOMMENDING)
    session.append_conversation_message(
        "system",
        "业务员已人工核准车辆信息，AI 流程已恢复。",
        sender_name="系统",
    )
    await session_store.save_session(session)
    resolved = await _complete_manual_review_ticket(
        session_id,
        identity,
        "人工核准车辆信息",
    )
    return {
        "success": True,
        "message": "车辆信息已补录，可以继续智能推荐流程。",
        "vehicle": session.vehicle_info.to_dict(),
        "phase": session.phase.value,
        "takeover_resolved": resolved,
    }


@agent_router.post("/session/{session_id}/manual-applicant")
async def update_applicant_manually(
    session_id: str,
    req: ManualApplicantRequest,
    request: Request,
):
    """人工补录投保人信息，并恢复到报价阶段。"""
    session = await _require_agent_session(request, session_id)
    identity = _identity(request)
    await _require_manual_review_owner(session_id, identity)
    name = req.name.strip()
    id_number = req.id_number.strip().upper()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="投保人姓名不完整")
    if not re.fullmatch(r"\d{17}[\dX]", id_number):
        raise HTTPException(status_code=400, detail="身份证号格式不正确")
    id_card = IDCardInfo(name=name, id_number=id_number)
    session.applicant_id_card = id_card
    session.insured_id_card = id_card
    session.advance_phase(WorkflowPhase.QUOTING)
    session.append_conversation_message(
        "system",
        "业务员已人工核准投保人信息，AI 流程已恢复。",
        sender_name="系统",
    )
    await session_store.save_session(session)
    resolved = await _complete_manual_review_ticket(
        session_id,
        identity,
        "人工核准投保人信息",
    )
    return {
        "success": True,
        "message": "投保人信息已补录，可以继续生成报价。",
        "applicant": id_card.to_dict(),
        "phase": session.phase.value,
        "takeover_resolved": resolved,
    }


@agent_router.get("/session/{session_id}/messages")
async def get_agent_session_messages(session_id: str, request: Request):
    """获取接管会话的沟通记录。"""
    session = await _require_agent_session(request, session_id)
    return {
        "success": True,
        "messages": (session.conversation_messages or [])[-100:],
    }


@agent_router.post("/session/{session_id}/messages")
async def send_agent_session_message(
    session_id: str,
    req: AgentChatMessageRequest,
    request: Request,
):
    """业务员向接管中的客户发送消息。"""
    identity = _identity(request)
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="消息不能超过 2000 字")
    session = await _require_agent_session(request, session_id)
    if session.phase != WorkflowPhase.TRANSFERRED_TO_HUMAN:
        raise HTTPException(status_code=409, detail="当前会话不在人工接管中")
    try:
        async with get_db() as db:
            takeover = await TakeoverRepository.get_active_by_session(
                db, session_id
            )
    except Exception as e:
        logger.warning("校验接管工单失败: %s", e)
        raise HTTPException(status_code=503, detail="数据库不可用，请稍后重试")
    if (
        not takeover
        or takeover.handle_status != "processing"
        or takeover.handler_id != identity.agent_id
    ):
        raise HTTPException(status_code=403, detail="请先认领该工单再回复客户")
    message = session.append_conversation_message(
        "agent",
        content,
        sender_id=identity.subject,
        sender_name=identity.name or identity.agent_id,
    )
    await session_store.save_session(session)
    return {"success": True, "message_item": message}


# ==================== 实时通知 (SSE) ====================

@agent_router.get("/notifications/stream")
async def agent_notifications_stream(agent_id: str, request: Request):
    """坐席端建立 SSE 长连接，实时接收「新工单」推送。

    客户端用法（浏览器）：
        const es = new EventSource(`/api/agent/notifications/stream?agent_id=${agentId}`);
        es.onmessage = (e) => { const ticket = JSON.parse(e.data); ... };
    """
    identity = _identity(request)
    agent_id = identity.agent_id
    queue = await notification_hub.subscribe(agent_id)

    async def event_generator():
        try:
            # 握手成功帧，便于客户端确认连接已建立
            yield _sse_event({"type": "connected", "agent_id": agent_id})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse_event(event)
                except asyncio.TimeoutError:
                    # 心跳保活，同时探测客户端是否已断开
                    yield ": keep-alive\n\n"
        finally:
            try:
                await notification_hub.unsubscribe(agent_id, queue)
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲，保证实时性
        },
    )


# ==================== 人工接管工单 ====================

@agent_router.get("/takeovers/pending")
async def list_pending_takeovers(limit: int = 20):
    """查看待处理的人工接管工单队列。"""
    limit = max(1, min(limit, 100))
    try:
        async with get_db() as db:
            records = await TakeoverRepository.get_pending(db, limit=limit)
    except Exception as e:
        logger.warning("查询待处理工单失败: %s", e)
        raise HTTPException(status_code=503, detail="数据库不可用，请稍后重试")

    return {
        "success": True,
        "total": len(records),
        "takeovers": [await _takeover_payload(r) for r in records],
    }


@agent_router.get("/takeovers")
async def list_takeovers(status: str = "pending", limit: int = 50):
    """按状态查看人工接管工单，并返回队列统计。"""
    status = status if status in {"pending", "processing", "resolved", "all"} else "pending"
    limit = max(1, min(limit, 100))
    try:
        async with get_db() as db:
            records = await TakeoverRepository.list_by_status(db, status, limit)
            counts = await TakeoverRepository.counts(db)
    except Exception as e:
        logger.warning("查询人工接管工单失败: %s", e)
        raise HTTPException(status_code=503, detail="数据库不可用，请稍后重试")
    return {
        "success": True,
        "status": status,
        "total": len(records),
        "counts": {
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0),
            "resolved": counts.get("resolved", 0),
        },
        "takeovers": [await _takeover_payload(r) for r in records],
    }


@agent_router.post("/takeovers/{takeover_id}/claim")
async def claim_takeover(takeover_id: int, request: Request):
    """业务员认领工单。"""
    identity = _identity(request)
    try:
        async with get_db() as db:
            record = await TakeoverRepository.get_by_id(db, takeover_id)
            if not record:
                raise HTTPException(status_code=404, detail="工单不存在")
            if record.handle_status == "resolved":
                raise HTTPException(status_code=409, detail="工单已办结")
            if (
                record.handle_status == "processing"
                and record.handler_id != identity.agent_id
            ):
                raise HTTPException(status_code=409, detail="工单已被其他业务员认领")
            if record.handle_status == "pending":
                await TakeoverRepository.claim(db, takeover_id, identity.agent_id)
            record = await TakeoverRepository.get_by_id(db, takeover_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("认领工单失败: %s", e)
        raise HTTPException(status_code=503, detail="数据库不可用，请稍后重试")
    return {
        "success": True,
        "takeover": await _takeover_payload(record),
    }


@agent_router.post("/takeovers/{takeover_id}/resolve")
async def resolve_takeover(
    takeover_id: int,
    req: ResolveTakeoverRequest,
    request: Request,
):
    """坐席办结人工接管工单。"""
    identity = _identity(request)
    record = None
    try:
        async with get_db() as db:
            record = await TakeoverRepository.get_by_id(db, takeover_id)
            if not record:
                raise HTTPException(status_code=404, detail="工单不存在")
            if record.handle_status == "resolved":
                raise HTTPException(status_code=409, detail="工单已办结")
            if (
                record.handle_status == "processing"
                and record.handler_id != identity.agent_id
            ):
                raise HTTPException(
                    status_code=403, detail="工单由其他业务员处理中"
                )
            updated = await TakeoverRepository.resolve(
                db,
                id=takeover_id,
                handler_id=identity.agent_id,
                remark=req.remark,
            )
            if not updated:
                raise HTTPException(status_code=409, detail="工单状态已变化")
            record = await TakeoverRepository.get_by_id(db, takeover_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("办结工单失败: %s", e)
        raise HTTPException(status_code=503, detail="数据库不可用，请稍后重试")

    session = await session_store.get_session(record.session_id)
    if (
        session
        and session.phase.value == "transferred_to_human"
        and record.source_phase
    ):
        try:
            session.advance_phase(WorkflowPhase(record.source_phase))
            await session_store.save_session(session)
        except ValueError:
            logger.warning("无法恢复工单来源阶段: %s", record.source_phase)

    try:
        await notification_hub.broadcast_all({
            "type": "takeover_resolved",
            "takeover_id": takeover_id,
            "session_id": record.session_id,
            "handler_id": identity.agent_id,
        })
    except Exception:
        pass

    return {
        "success": True,
        "takeover": await _takeover_payload(record),
    }
