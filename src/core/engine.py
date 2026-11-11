"""
车险出单智能体 - 核心工作流引擎
编排车险出单 7 个阶段的完整业务流程
"""
import copy
import json
import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Tuple

from src.api.schemas import (
    Session, WorkflowPhase, CustomerType, UserRole,
    InsuranceType, InsurancePlan, Quotation,
    UnderwritingResult, UnderwritingStatus,
    PaymentResult, PaymentStatus,
    Policy, VehicleInfo, IDCardInfo,
    PRESET_INSURANCE_ITEMS, PRESET_COMPANIES, now_utc,
)
from src.core.state import session_store
from src.services.ocr_service import ocr_service
from src.services.human_service import human_service
from src.services.underwriting_service import underwriting_service
from src.services.payment_service import payment_service
from src.services.quote_service import quote_service
from src.services.provider_gateway import ProviderAPIError, ProviderAPIUnavailable
from src.knowledge.rules import recommendation_engine
from src.knowledge.guardrails import knowledge_guardrails
from src.utils.plate_validator import is_valid_plate, normalize_plate
from src.storage.mysql import get_db
from src.storage.repository import (
    OperationLogRepository,
    PaymentRepository,
    UnderwritingRepository,
)

logger = logging.getLogger(__name__)

_WORKFLOW_LABELS = {
    WorkflowPhase.INSURANCE_TYPE_SELECTION.value: "选择保险类型",
    WorkflowPhase.COLLECTING_INFO.value: "收集车辆信息",
    WorkflowPhase.RECOMMENDING.value: "智能推荐方案",
    WorkflowPhase.IDENTITY_VERIFICATION.value: "身份验证",
    WorkflowPhase.QUOTING.value: "生成报价",
    WorkflowPhase.UNDERWRITING.value: "核保",
    WorkflowPhase.PAYMENT.value: "支付",
}


def _policy_expire_date(effective: date) -> date:
    """计算一年期保单截止日（生效日次年同日 - 1 天）。

    处理闰年 2/29 生效的特殊情况：次年平年无 2/29，退到 2/28，
    避免 ``date.replace(year+1)`` 抛 ValueError。
    """
    try:
        anniversary = effective.replace(year=effective.year + 1)
    except ValueError:
        anniversary = effective.replace(year=effective.year + 1, day=28)
    return anniversary - timedelta(days=1)


def _plan_change_summary(before: dict, after: dict) -> str:
    """根据结构化方案差异生成简短、可信的调整说明。"""
    changes: list[str] = []

    before_company = (before.get("company") or {}).get("name", "")
    after_company = (after.get("company") or {}).get("name", "")
    if before_company != after_company:
        changes.append(f"保险公司由{before_company or '未指定'}调整为{after_company}")

    before_items = {item["code"]: item for item in before.get("items", [])}
    after_items = {item["code"]: item for item in after.get("items", [])}

    removed = [
        before_items[code]["name"]
        for code in before_items.keys() - after_items.keys()
    ]
    added = [
        after_items[code]["name"]
        for code in after_items.keys() - before_items.keys()
    ]
    if removed:
        changes.append(f"移除{'、'.join(removed)}")
    if added:
        changes.append(f"增加{'、'.join(added)}")

    for code in before_items.keys() & after_items.keys():
        old_item = before_items[code]
        new_item = after_items[code]
        if old_item["coverage_amount"] != new_item["coverage_amount"]:
            changes.append(
                f"{new_item['name']}保额由{Decimal(old_item['coverage_amount']):,.0f}元"
                f"调整为{Decimal(new_item['coverage_amount']):,.0f}元"
            )
        elif old_item["premium"] != new_item["premium"]:
            changes.append(
                f"{new_item['name']}保费调整为{Decimal(new_item['premium']):,.2f}元"
            )

    return "；".join(changes)


async def _write_provider_audit(
    session: Session,
    action: str,
    phase_from: str,
    phase_to: str,
    detail: dict,
) -> None:
    """写入第三方调用审计；审计失败不回滚主业务，但必须记录错误。"""
    try:
        async with get_db() as db:
            await OperationLogRepository.log(
                db,
                session_id=session.session_id,
                operator_id="system",
                operator_role="system",
                action=action,
                phase_from=phase_from,
                phase_to=phase_to,
                detail=detail,
            )
    except Exception as exc:
        logger.error(
            "第三方调用审计写入失败: session=%s action=%s err=%s",
            session.session_id[:8],
            action,
            exc,
        )


async def _persist_payment_record(session: Session) -> None:
    payment = session.payment_result
    quotation = session.quotation
    if not payment or not quotation:
        return
    try:
        async with get_db() as db:
            existing = await PaymentRepository.get_by_transaction(
                db, payment.transaction_id
            )
            if existing:
                await PaymentRepository.update_status(
                    db,
                    existing.id,
                    payment.status.value,
                    paid_at=payment.paid_at,
                )
            else:
                await PaymentRepository.create(
                    db,
                    session_id=session.session_id,
                    quotation_id=quotation.quotation_id,
                    transaction_id=payment.transaction_id,
                    payment_url=payment.payment_url,
                    amount=payment.paid_amount,
                    status=payment.status.value,
                    paid_at=payment.paid_at,
                )
    except Exception as exc:
        logger.error(
            "支付记录落库失败: session=%s txn=%s err=%s",
            session.session_id[:8],
            payment.transaction_id,
            exc,
        )


async def _persist_underwriting_record(session: Session) -> None:
    result = session.underwriting_result
    quotation = session.quotation
    if not result or not quotation:
        return
    try:
        async with get_db() as db:
            await UnderwritingRepository.create(
                db,
                session_id=session.session_id,
                quotation_id=quotation.quotation_id,
                status=result.status.value,
                message=result.message[:500],
                requires_photo=1 if result.requires_photo else 0,
                requires_video=1 if result.requires_video else 0,
                checked_at=result.checked_at,
            )
    except Exception as exc:
        logger.error(
            "核保记录落库失败: session=%s quotation=%s err=%s",
            session.session_id[:8],
            quotation.quotation_id,
            exc,
        )


class InsuranceWorkflowEngine:
    """车险出单核心工作流引擎"""

    # ==================== Phase 0: 保险类型选择 ====================

    async def select_insurance_type(
        self, session_id: str, insurance_type: InsuranceType
    ) -> dict:
        """
        选择保险类型。目前仅支持车险。

        Args:
            session_id: 会话ID
            insurance_type: 保险类型

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if insurance_type != InsuranceType.CAR:
            return {
                "success": False,
                "message": f"目前仅支持车险（{InsuranceType.CAR.value}），{insurance_type.value} 暂未开放。",
            }

        session.insurance_type = InsuranceType.CAR
        session.advance_phase(WorkflowPhase.COLLECTING_INFO)
        await session_store.save_session(session)

        return {
            "success": True,
            "message": "已选择车险，请提供您的车辆信息。\n新用户请上传行驶证正副页照片；\n老用户请提供车牌号或车架号查询。",
            "phase": session.phase.value,
            "session": session.to_dict(),
        }

    # ==================== Phase 1: 收集信息 ====================

    async def collect_info_new_customer(
        self, session_id: str, front_image: bytes, back_image: bytes
    ) -> dict:
        """
        新用户：上传行驶证正副页，OCR 识别车辆信息。

        Args:
            session_id: 会话ID
            front_image: 行驶证主页
            back_image: 行驶证副页

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        vehicle = await ocr_service.recognize_vehicle_license(front_image, back_image)

        is_ok, issue = human_service.check_ocr_quality(vehicle)
        if not is_ok:
            await human_service.request_human_takeover(session, issue)
            await session_store.save_session(session)
            return {
                "success": True,
                "transferred_to_human": True,
                "message": f"OCR 识别信息不完整（{issue}），已为您转接人工客服。",
                "phase": session.phase.value,
            }

        session.vehicle_info = vehicle
        session.customer_type = CustomerType.NEW
        await session_store.link_plate(session.session_id, vehicle.license_plate)

        session.advance_phase(WorkflowPhase.RECOMMENDING)
        await session_store.save_session(session)

        return {
            "success": True,
            "message": f"行驶证识别成功！\n车牌号：{vehicle.license_plate}\n品牌型号：{vehicle.brand_model}\n所有人：{vehicle.owner_name}\n\n正在为您生成智能推荐方案...",
            "vehicle": vehicle.to_dict(),
            "phase": session.phase.value,
        }

    async def collect_info_returning_customer(
        self, session_id: str, plate: str = "", vin: str = ""
    ) -> dict:
        """
        老用户：凭车牌号或车架号查询历史信息。
        优先从已有会话中查找 OCR 过的车辆，未找到则提示上传行驶证。

        Args:
            session_id: 会话ID
            plate: 车牌号
            vin: 车架号

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        query_plate = normalize_plate(plate) if plate else ""
        query_vin = vin.strip().upper() if vin else ""

        if not query_plate and not query_vin:
            return {"success": False, "message": "请提供车牌号或车架号进行查询。"}

        if query_plate and not is_valid_plate(query_plate):
            return {"success": False, "message": f"车牌号 '{plate}' 格式不正确，请检查后重新输入。"}

        # 优先按车牌查，未提供车牌或车牌无结果时按车架号查
        vehicle = None
        if query_plate:
            vehicle = await session_store.get_vehicle_by_plate(query_plate)
        if not vehicle and query_vin:
            vehicle = await session_store.get_vehicle_by_vin(query_vin)
        if not vehicle:
            key_desc = f"车牌号 '{query_plate}'" if query_plate else f"车架号 '{query_vin}'"
            return {
                "success": False,
                "message": (
                    f"未查询到{key_desc}的历史投保记录。\n"
                    "请上传行驶证正副页图片，我们将为您进行 OCR 识别并建档。"
                ),
                "suggest_next": "upload_vehicle_license",
            }

        session.vehicle_info = vehicle
        session.customer_type = CustomerType.RETURNING
        session.advance_phase(WorkflowPhase.RECOMMENDING)
        if vehicle.license_plate:
            await session_store.link_plate(session.session_id, vehicle.license_plate)
        await session_store.save_session(session)

        # 尝试获取该车辆的历史投保方案
        last_year_plan = recommendation_engine.get_last_year_plan(vehicle)
        if last_year_plan:
            last_year_plan.is_last_year_plan = True
            last_year_plan.calculate_total()
            plan_text = (
                f"您去年的投保方案为：\n"
                f"  - 保险公司：{last_year_plan.company.name}\n"
                f"  - 险种：{', '.join(i.name for i in last_year_plan.items)}\n"
                f"  - 总保费：{last_year_plan.total_premium} 元\n\n"
                f"是否沿用去年的投保方案？如果是，我们将直接为您报价；"
                f"如需修改，请告诉我您的需求。"
            )
            plan_data = last_year_plan.to_dict()
        else:
            plan_text = "未查询到去年的投保方案，即将为您生成智能推荐方案。"
            plan_data = None

        return {
            "success": True,
            "message": f"查询成功！欢迎回来，{vehicle.owner_name}。\n"
                        f"车辆：{vehicle.brand_model}（{vehicle.license_plate}）\n{plan_text}",
            "vehicle": vehicle.to_dict(),
            "last_year_plan": plan_data,
            "customer_type": "returning",
            "phase": session.phase.value,
        }

    # ==================== Phase 2: 智能推荐 ====================

    async def generate_recommendation(self, session_id: str) -> dict:
        """
        智能推荐：根据车辆信息生成投保方案。

        Args:
            session_id: 会话ID

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if session.customer_type == CustomerType.RETURNING:
            if session.insurance_plan and session.insurance_plan.is_last_year_plan:
                return {
                    "success": True,
                    "message": "您选择沿用去年的投保方案，直接进入报价阶段。",
                    "plan": session.insurance_plan.to_dict(),
                    "skip_recommendation": True,
                }

        plan = recommendation_engine.generate_recommendation(session.vehicle_info)
        session.insurance_plan = plan
        session.advance_phase(WorkflowPhase.RECOMMENDING)
        await session_store.save_session(session)

        return {
            "success": True,
            "message": f"已生成推荐方案：{plan.company.name} · {len(plan.items)} 项保障\n"
                        f"💰 总保费：¥{plan.total_premium}（折扣率 {plan.discount_rate}）\n\n"
                        f"方案明细已整理在下方卡片，可直接选择“更优惠方案”或自定义调整。",
            "plan": plan.to_dict(),
            "phase": session.phase.value,
        }

    async def use_last_year_plan(self, session_id: str) -> dict:
        """
        老用户选择沿用去年的投保方案。

        Args:
            session_id: 会话ID

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if session.customer_type != CustomerType.RETURNING:
            return {"success": False, "message": "仅老用户可以使用此功能。"}

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                copy.deepcopy(PRESET_INSURANCE_ITEMS["compulsory"]),
                copy.deepcopy(PRESET_INSURANCE_ITEMS["third_party_1m"]),
                copy.deepcopy(PRESET_INSURANCE_ITEMS["vehicle_loss"]),
                copy.deepcopy(PRESET_INSURANCE_ITEMS["driver_passenger"]),
            ],
            discount_rate=PRESET_COMPANIES["pingan"].discount_rate,
            is_last_year_plan=True,
        )
        plan.calculate_total()

        session.insurance_plan = plan
        session.advance_phase(WorkflowPhase.IDENTITY_VERIFICATION)
        await session_store.save_session(session)

        return {
            "success": True,
            "message": f"已确认沿用去年的投保方案。\n"
                        f"总保费：{plan.total_premium} 元\n\n"
                        f"下一步：请上传投保人的身份证正反面照片。",
            "plan": plan.to_dict(),
            "phase": session.phase.value,
        }

    async def modify_plan(self, session_id: str, user_input: str) -> dict:
        """
        自然语言修改投保方案。

        Args:
            session_id: 会话ID
            user_input: 用户自然语言输入

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if not session.insurance_plan:
            return {"success": False, "message": "当前没有投保方案，请先生成推荐方案。"}

        if "确认" in user_input or "确定" in user_input or "没问题" in user_input:
            session.advance_phase(WorkflowPhase.IDENTITY_VERIFICATION)
            await session_store.save_session(session)
            return {
                "success": True,
                "message": f"方案已确认！\n"
                            f"总保费：{session.insurance_plan.total_premium} 元\n\n"
                            f"下一步：请上传投保人的身份证正反面照片。",
                "plan": session.insurance_plan.to_dict(),
                "phase": session.phase.value,
            }

        if recommendation_engine.is_removing_compulsory(user_input):
            suggestions: list[str] = []
            if any(
                item.code == "VEHICLE_LOSS" for item in session.insurance_plan.items
            ):
                suggestions.append("去掉车损险")
            if any(
                item.code == "DRIVER_PASSENGER"
                for item in session.insurance_plan.items
            ):
                suggestions.append("去掉车上人员责任险")
            third_party = next(
                (
                    item
                    for item in session.insurance_plan.items
                    if item.code == "THIRD_PARTY"
                ),
                None,
            )
            if third_party and third_party.coverage_amount > Decimal("500000"):
                suggestions.append("降低三者险保额")
            suggestion_text = (
                f"如果目标是降低保费，可以{'、'.join(suggestions)}。"
                if suggestions
                else "可以尝试获取更优惠的组合。"
            )
            return {
                "success": True,
                "message": (
                    "交强险是国家法律规定的强制保险，不能从方案中移除，"
                    "因此本次没有修改方案。\n\n"
                    f"当前方案仍保留交强险，总保费 ¥{session.insurance_plan.total_premium:,.2f}。\n"
                    f"{suggestion_text}"
                ),
                "plan": session.insurance_plan.to_dict(),
                "phase": session.phase.value,
            }

        before_plan = session.insurance_plan.to_dict()
        plan = recommendation_engine.parse_modification_intent(user_input, session.insurance_plan)
        session.insurance_plan = plan
        await session_store.save_session(session)

        if plan.to_dict() == before_plan:
            return {
                "success": True,
                "message": (
                    "当前方案没有发生可执行的调整。您可以明确说明要修改的险种、"
                    "保额或保险公司，例如“三者降到100万”或“去掉车上人员责任险”。"
                ),
                "plan": plan.to_dict(),
                "phase": session.phase.value,
            }

        change_summary = _plan_change_summary(before_plan, plan.to_dict())
        old_total = Decimal(before_plan["total_premium"])
        new_total = plan.total_premium
        total_delta = old_total - new_total
        if total_delta > 0:
            total_line = (
                f"💰 新总保费：¥{new_total:,.2f}，"
                f"较原方案节省 ¥{total_delta:,.2f}。"
            )
        elif total_delta < 0:
            total_line = (
                f"💰 新总保费：¥{new_total:,.2f}，"
                f"较原方案增加 ¥{abs(total_delta):,.2f}。"
            )
        else:
            total_line = f"💰 新总保费：¥{new_total:,.2f}"

        coverage_note = ""
        if (
            len(plan.items) == 1
            and plan.items[0].code == "COMPULSORY"
        ):
            coverage_note = (
                "\n⚠️ 当前仅保留交强险。交强险对第三者财产损失的赔偿限额较低，"
                "请确认是否符合您的风险保障需求。"
            )

        return {
            "success": True,
            "message": f"方案已更新\n\n{change_summary}。\n\n{total_line}\n\n"
                        f"{coverage_note}"
                        f"完整明细见下方最新方案卡，可继续调整或确认方案。",
            "plan": plan.to_dict(),
            "phase": session.phase.value,
        }

    # ==================== Phase 3: 身份验证 ====================

    async def upload_applicant_id_card(
        self, session_id: str, front_image: bytes, back_image: bytes
    ) -> dict:
        """
        上传投保人身份证正反面。

        Args:
            session_id: 会话ID
            front_image: 身份证正面
            back_image: 身份证背面

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if not session.insurance_plan:
            return {"success": False, "message": "请先确认投保方案，再上传身份证。"}

        id_card = await ocr_service.recognize_id_card(front_image, back_image)
        session.applicant_id_card = id_card
        session.insured_id_card = id_card

        valid, missing = ocr_service.validate_id_card(id_card)
        if not valid:
            await human_service.request_human_takeover(session, f"身份证信息不完整: {', '.join(missing)}")
            await session_store.save_session(session)
            return {
                "success": True,
                "transferred_to_human": True,
                "message": f"身份证识别信息不完整（{', '.join(missing)}），已为您转接人工客服。",
                "phase": session.phase.value,
            }

        session.advance_phase(WorkflowPhase.QUOTING)
        await session_store.save_session(session)

        return {
            "success": True,
            "message": f"身份证识别成功！\n"
                        f"姓名：{id_card.name}\n"
                        f"身份证号：{id_card.id_number[:4]}****{id_card.id_number[-4:]}\n\n"
                        f"即将为您生成报价...",
            "id_card": id_card.to_dict(),
            "phase": session.phase.value,
        }

    # ==================== Phase 4: 报价 ====================

    async def generate_quotation(self, session_id: str) -> dict:
        """
        生成报价单。

        Args:
            session_id: 会话ID

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if not session.insurance_plan:
            return {"success": False, "message": "请先生成投保方案。"}

        if not session.applicant_id_card:
            return {"success": False, "message": "请先上传身份证。"}

        pre_validation = knowledge_guardrails.validate_quote(session)
        if not pre_validation.allowed:
            return {
                "success": False,
                "message": "知识库规则校验未通过：" + "；".join(pre_validation.errors),
                "validation_errors": pre_validation.errors,
                "validation_warnings": pre_validation.warnings,
                "knowledge_sources": [
                    source.to_dict() for source in pre_validation.sources
                ],
            }

        today = date.today()
        effective_date = today + timedelta(days=1)
        expire_date = _policy_expire_date(effective_date)

        phase_from = session.phase.value
        try:
            quotation = await quote_service.create_quote(
                session,
                effective_date=effective_date,
                expire_date=expire_date,
            )
        except (ProviderAPIUnavailable, ProviderAPIError) as exc:
            logger.warning("报价失败: %s", exc)
            return {"success": False, "message": str(exc)}

        original_plan = session.insurance_plan
        session.insurance_plan = quotation.plan
        post_validation = knowledge_guardrails.validate_quote(session)
        if not post_validation.allowed:
            session.insurance_plan = original_plan
            return {
                "success": False,
                "message": "保司报价触发知识库拦截：" + "；".join(post_validation.errors),
                "validation_errors": post_validation.errors,
                "validation_warnings": post_validation.warnings,
                "knowledge_sources": [
                    source.to_dict() for source in post_validation.sources
                ],
            }
        quotation.validation_warnings = post_validation.warnings
        quotation.knowledge_sources = [
            source.to_dict() for source in post_validation.sources
        ]
        session.insurance_plan = quotation.plan
        session.quotation = quotation
        session.advance_phase(WorkflowPhase.QUOTING)
        await session_store.save_session(session)
        await _write_provider_audit(
            session,
            action="quote.success",
            phase_from=phase_from,
            phase_to=session.phase.value,
            detail={
                "quotation_id": quotation.quotation_id,
                "provider_reference": quotation.provider_reference,
                "quote_version": quotation.version,
                "request_digest": quotation.request_digest,
                "valid_until": quotation.valid_until.isoformat()
                if quotation.valid_until
                else "",
                "rate_source": quotation.rate_source,
                "amount": str(session.insurance_plan.total_premium),
                "raw_response": quotation.raw_response,
            },
        )
        warning_text = (
            "⚠️ 知识库提示："
            + "；".join(post_validation.warnings)
            + "\n\n"
            if post_validation.warnings
            else ""
        )

        return {
            "success": True,
            "message": f"📋 报价单已生成，请确认以下关键信息：\n\n"
                        f"🚗 车辆信息\n"
                        f"  车牌号：{session.vehicle_info.license_plate}\n"
                        f"  品牌型号：{session.vehicle_info.brand_model}\n"
                        f"  车架号：{session.vehicle_info.vin}\n\n"
                        f"👤 投保人信息\n"
                        f"  姓名：{session.applicant_id_card.name}\n\n"
                        f"🏢 保险公司：{session.insurance_plan.company.name}\n\n"
                        f"📌 报价版本：{quotation.version}\n"
                        f"📌 费率来源：{quotation.rate_source}\n"
                        f"⏳ 报价有效期至：{quotation.valid_until}\n\n"
                        f"📋 险种明细：\n" +
                        "\n".join(f"  • {item.name}：保额 {item.coverage_amount} 元（保费 {item.premium} 元）"
                                  for item in session.insurance_plan.items) +
                        f"\n\n💰 总保费：{session.insurance_plan.total_premium} 元\n"
                        f"📅 生效日期：{effective_date}\n"
                        f"📅 截止日期：{expire_date}\n\n"
                        + warning_text
                        + f"确认无误后，请点击报价进入核保流程。",
            "quotation": quotation.to_dict(),
            "phase": session.phase.value,
            "validation_warnings": post_validation.warnings,
            "knowledge_sources": quotation.knowledge_sources,
        }

    # ==================== Phase 5: 核保 ====================

    async def submit_underwriting(self, session_id: str) -> dict:
        """
        提交核保申请。

        Args:
            session_id: 会话ID

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if not session.quotation:
            return {"success": False, "message": "请先生成报价单。"}

        validation = knowledge_guardrails.validate_underwriting(session)
        if not validation.allowed:
            return {
                "success": False,
                "message": "核保前知识库校验未通过：" + "；".join(validation.errors),
                "validation_errors": validation.errors,
                "validation_warnings": validation.warnings,
                "knowledge_sources": [
                    source.to_dict() for source in validation.sources
                ],
            }

        phase_from = session.phase.value
        quotation = session.quotation
        if (
            quotation.valid_until
            and quotation.valid_until <= now_utc()
        ):
            return {
                "success": False,
                "message": "报价单已过期，请重新获取保司报价。",
            }
        try:
            result = await underwriting_service.submit_for_underwriting(
                quotation.to_dict()
            )
        except (ProviderAPIUnavailable, ProviderAPIError) as exc:
            logger.warning("核保提交失败: %s", exc)
            return {"success": False, "message": str(exc)}
        session.underwriting_result = result
        session.advance_phase(WorkflowPhase.UNDERWRITING)
        await session_store.save_session(session)
        await _persist_underwriting_record(session)
        await _write_provider_audit(
            session,
            action="underwriting.submit",
            phase_from=phase_from,
            phase_to=session.phase.value,
            detail={
                "quotation_id": quotation.quotation_id,
                "quote_version": quotation.version,
                "provider_reference": result.provider_reference,
                "request_digest": result.request_digest,
                "amount": str(result.approved_amount),
                "raw_response": result.raw_response,
            },
        )

        if result.status == UnderwritingStatus.PENDING:
            warning_text = (
                "；".join(validation.warnings) if validation.warnings else ""
            )
            return {
                "success": True,
                "message": "核保处理中，等待保险公司正式回调。"
                + (f"\n知识库提示：{warning_text}" if warning_text else ""),
                "underwriting": result.to_dict(),
                "phase": session.phase.value,
                "validation_warnings": validation.warnings,
                "knowledge_sources": [
                    source.to_dict() for source in validation.sources
                ],
            }

        if result.status == UnderwritingStatus.APPROVED:
            return {
                "success": False,
                "message": "核保结果必须通过保司正式回调确认。",
                "underwriting": result.to_dict(),
                "phase": session.phase.value,
            }
        elif result.status == UnderwritingStatus.NEEDS_VERIFICATION:
            return {
                "success": True,
                "needs_verification": True,
                "message": f"⚠️ {result.message}",
                "underwriting": result.to_dict(),
                "phase": session.phase.value,
            }
        else:
            return {
                "success": False,
                "message": f"❌ {result.message}",
                "underwriting": result.to_dict(),
                "phase": session.phase.value,
            }

    async def apply_underwriting_callback(
        self, quotation_id: str, result: UnderwritingResult
    ) -> bool:
        """
        核保回调到达后，将会话推进到对应阶段。

        生产环境下 submit_underwriting 会立即返回 PENDING 且不推进 phase，
        必须由本方法在回调时把结果写回会话并推进状态机，否则会话会卡在报价阶段。
        """
        session = await session_store.find_by_quotation_id(quotation_id)
        if not session:
            logger.warning("核保回调未找到对应会话: quotation_id=%s", quotation_id)
            return False

        if session.phase == WorkflowPhase.COMPLETED:
            return True

        quotation = session.quotation
        pending = session.underwriting_result
        if not quotation or not pending:
            logger.warning("核保回调缺少报价或待核保记录: quotation_id=%s", quotation_id)
            return False
        expected_amount = (
            quotation.plan.total_premium if quotation.plan else Decimal("0")
        )
        if (
            result.quote_version != quotation.version
            or result.approved_amount != expected_amount
            or result.provider_reference != pending.provider_reference
        ):
            logger.warning(
                "核保回调业务字段不一致: quotation_id=%s version=%s amount=%s ref=%s",
                quotation_id,
                result.quote_version,
                result.approved_amount,
                result.provider_reference,
            )
            await _write_provider_audit(
                session,
                action="underwriting.callback_rejected",
                phase_from=session.phase.value,
                phase_to=session.phase.value,
                detail={
                    "quotation_id": quotation_id,
                    "expected_amount": str(expected_amount),
                    "callback_amount": str(result.approved_amount),
                    "expected_quote_version": quotation.version,
                    "callback_quote_version": result.quote_version,
                    "expected_provider_reference": pending.provider_reference,
                    "callback_provider_reference": result.provider_reference,
                },
            )
            return False

        phase_from = session.phase.value
        session.underwriting_result = result
        session.advance_phase(WorkflowPhase.UNDERWRITING)
        if result.status == UnderwritingStatus.APPROVED:
            session.advance_phase(WorkflowPhase.PAYMENT)
        await session_store.save_session(session)
        await _persist_underwriting_record(session)
        await _write_provider_audit(
            session,
            action=f"underwriting.callback_{result.status.value}",
            phase_from=phase_from,
            phase_to=session.phase.value,
            detail={
                "quotation_id": quotation_id,
                "quote_version": result.quote_version,
                "provider_reference": result.provider_reference,
                "request_digest": result.request_digest,
                "amount": str(result.approved_amount),
                "raw_response": result.raw_response,
            },
        )

        logger.info(
            "核保回调已推进会话: session=%s, status=%s",
            session.session_id[:8], result.status.value,
        )
        return True

    async def submit_verification_material(
        self, session_id: str, photo: bytes = None, video: bytes = None
    ) -> dict:
        """
        补充提交验车照片或视频。

        Args:
            session_id: 会话ID
            photo: 检车照片
            video: 验车视频

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if not session.quotation:
            return {"success": False, "message": "请先生成报价单。"}

        try:
            result = await underwriting_service.submit_verification_material(
                session.quotation.to_dict(), photo, video
            )
        except (ProviderAPIUnavailable, ProviderAPIError) as exc:
            logger.warning("核保补充材料提交失败: %s", exc)
            return {"success": False, "message": str(exc)}
        session.underwriting_result = result
        session.advance_phase(WorkflowPhase.UNDERWRITING)
        await session_store.save_session(session)
        await _persist_underwriting_record(session)
        await _write_provider_audit(
            session,
            action="underwriting.verification_submitted",
            phase_from=WorkflowPhase.UNDERWRITING.value,
            phase_to=session.phase.value,
            detail={
                "quotation_id": session.quotation.quotation_id,
                "quote_version": session.quotation.version,
                "provider_reference": result.provider_reference,
                "request_digest": result.request_digest,
                "raw_response": result.raw_response,
            },
        )

        return {
            "success": True,
            "message": result.message,
            "underwriting": result.to_dict(),
            "phase": session.phase.value,
        }

    # ==================== Phase 6: 支付 ====================

    async def initiate_payment(self, session_id: str) -> dict:
        """
        发起支付。

        Args:
            session_id: 会话ID

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if not session.insurance_plan:
            return {"success": False, "message": "暂无投保方案。"}

        if not session.quotation:
            return {"success": False, "message": "请先生成报价单。"}

        if (
            not session.underwriting_result
            or session.underwriting_result.status
            != UnderwritingStatus.APPROVED
        ):
            return {
                "success": False,
                "message": "保险公司尚未通过正式回调确认核保。",
            }
        try:
            payment = await payment_service.create_payment(
                session.quotation.to_dict()
            )
        except (ProviderAPIUnavailable, ProviderAPIError) as exc:
            logger.warning("创建支付订单失败: %s", exc)
            return {"success": False, "message": str(exc)}
        session.payment_result = payment
        session.advance_phase(WorkflowPhase.PAYMENT)
        await session_store.save_session(session)
        await _persist_payment_record(session)
        await _write_provider_audit(
            session,
            action="payment.created",
            phase_from=WorkflowPhase.PAYMENT.value,
            phase_to=session.phase.value,
            detail={
                "quotation_id": session.quotation.quotation_id,
                "quote_version": session.quotation.version,
                "transaction_id": payment.transaction_id,
                "provider_reference": payment.provider_reference,
                "amount": str(payment.paid_amount),
                "raw_response": payment.raw_response,
            },
        )

        return {
            "success": True,
            "message": f"💳 订单已创建\n"
                        f"金额：{payment.paid_amount} 元\n"
                        f"请点击以下链接完成支付：\n{payment.payment_url}",
            "payment": payment.to_dict(),
            "phase": session.phase.value,
        }

    async def verify_payment(self, session_id: str) -> dict:
        """
        核验支付结果。

        Args:
            session_id: 会话ID

        Returns:
            操作结果
        """
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}

        if not session.payment_result:
            return {"success": False, "message": "暂无支付记录。"}

        result = payment_service.verify_payment(
            session.payment_result.transaction_id,
            expected_amount=str(session.payment_result.expected_amount),
            expected_quote_version=session.quotation.version,
            expected_provider_reference=session.payment_result.provider_reference,
            quotation_id=session.quotation.quotation_id,
        )
        session.payment_result = result

        if result.status == PaymentStatus.SUCCESS:
            await session_store.save_session(session)
            await _persist_payment_record(session)
            await _write_provider_audit(
                session,
                action="payment.callback_success",
                phase_from=WorkflowPhase.PAYMENT.value,
                phase_to=session.phase.value,
                detail={
                    "quotation_id": session.quotation.quotation_id,
                    "quote_version": session.quotation.version,
                    "transaction_id": result.transaction_id,
                    "provider_reference": result.provider_reference,
                    "amount": str(result.paid_amount),
                    "raw_response": result.raw_response,
                },
            )

            return {
                "success": True,
                "message": "✅ 支付已由保司回调确认。保单出单接口尚未完成，暂不生成模拟保单。",
                "payment": result.to_dict(),
                "phase": session.phase.value,
            }
        else:
            await session_store.save_session(session)
            await _persist_payment_record(session)
            return {
                "success": False,
                "message": f"支付状态：{result.status.value}",
                "payment": result.to_dict(),
                "phase": session.phase.value,
            }

    async def rollback_session(self, session_id: str, target_phase: str) -> dict:
        """回退业务流程，并失效目标阶段之后的所有报价、核保和支付数据。"""
        session = await session_store.get_session(session_id)
        if not session:
            return {"success": False, "message": "会话不存在"}
        if session.phase in {
            WorkflowPhase.COMPLETED,
            WorkflowPhase.TRANSFERRED_TO_HUMAN,
        }:
            return {
                "success": False,
                "message": "当前阶段不允许回退，请联系业务员处理。",
            }
        if session.policy:
            return {
                "success": False,
                "message": "保单已生成，不能回退流程。",
            }
        if (
            session.payment_result
            and session.payment_result.status
            == PaymentStatus.SUCCESS
        ):
            return {
                "success": False,
                "message": "支付已确认成功，不能回退流程。",
            }
        if session.payment_result and session.payment_result.payment_url:
            return {
                "success": False,
                "message": "已有支付订单，请先联系保司取消订单后再回退。",
            }

        order = [
            WorkflowPhase.INSURANCE_TYPE_SELECTION.value,
            WorkflowPhase.COLLECTING_INFO.value,
            WorkflowPhase.RECOMMENDING.value,
            WorkflowPhase.IDENTITY_VERIFICATION.value,
            WorkflowPhase.QUOTING.value,
            WorkflowPhase.UNDERWRITING.value,
            WorkflowPhase.PAYMENT.value,
        ]
        current = session.phase.value
        if target_phase not in order or current not in order:
            return {"success": False, "message": "不支持的回退阶段"}
        target_index = order.index(target_phase)
        current_index = order.index(current)
        if target_index >= current_index:
            return {"success": False, "message": "只能回退到当前阶段之前的步骤"}

        phase_from = session.phase.value
        session.payment_result = None
        session.policy = None

        if target_index < order.index(WorkflowPhase.UNDERWRITING.value):
            session.underwriting_result = None
        if target_index < order.index(WorkflowPhase.QUOTING.value):
            session.quotation = None
        if target_index < order.index(WorkflowPhase.IDENTITY_VERIFICATION.value):
            session.applicant_id_card = None
            session.insured_id_card = None
        if target_index < order.index(WorkflowPhase.RECOMMENDING.value):
            session.insurance_plan = None
            session.customer_type = CustomerType.NEW
        if target_index < order.index(WorkflowPhase.COLLECTING_INFO.value):
            session.vehicle_info = None
            session.license_plate = ""

        session.advance_phase(WorkflowPhase(target_phase))
        session.append_conversation_message(
            "system",
            f"流程已回退至{_WORKFLOW_LABELS.get(target_phase, target_phase)}，后续报价和核保数据已失效。",
            sender_name="系统",
        )
        await session_store.save_session(session)
        await _write_provider_audit(
            session,
            action="workflow.rollback",
            phase_from=phase_from,
            phase_to=session.phase.value,
            detail={
                "target_phase": target_phase,
                "invalidated_quotation": session.quotation is None,
                "invalidated_underwriting": session.underwriting_result is None,
                "invalidated_payment": session.payment_result is None,
            },
        )
        return {
            "success": True,
            "message": f"已回退至{_WORKFLOW_LABELS.get(target_phase, target_phase)}。",
            "phase": session.phase.value,
            "quotation": session.quotation.to_dict() if session.quotation else None,
            "underwriting": (
                session.underwriting_result.to_dict()
                if session.underwriting_result
                else None
            ),
            "payment": None,
            "policy": None,
        }

    # ==================== Phase 7: 生成保单 ====================

    def _generate_policy(self, session: Session) -> Policy:
        """生成保险单"""
        today = date.today()
        effective = today + timedelta(days=1)
        expire = _policy_expire_date(effective)

        policy_number = f"PX{now_utc().strftime('%Y%m%d%H%M%S')}{session.license_plate[-3:]}"

        return Policy(
            policy_id=str(uuid.uuid4()),
            policy_number=policy_number,
            quotation=session.quotation,
            applicant=session.applicant_id_card,
            insured=session.insured_id_card,
            vehicle=session.vehicle_info,
            items_detail=session.insurance_plan.items if session.insurance_plan else [],
            total_premium=session.insurance_plan.total_premium if session.insurance_plan else Decimal("0"),
            effective_date=effective,
            expire_date=expire,
            terms_and_conditions=[
                "1. 本保单自生效日期起承担保险责任。",
                "2. 被保险人应如实告知车辆使用情况，如有变更请及时通知。",
                "3. 发生保险事故后，请在48小时内报案。",
                "4. 理赔时需提供有效行驶证、驾驶证、事故证明等材料。",
                "5. 本保单最终解释权归保险公司所有。",
                "6. 如需退保，请联系业务员或拨打客服热线。",
                "7. 保险到期前30天将收到续保提醒。",
            ],
            issued_at=now_utc(),
        )

    # ==================== 批量操作（业务员专用） ====================

    async def batch_query_by_plates(
        self, agent_id: str, plates: list[str]
    ) -> dict:
        """
        业务员批量查询车牌号对应的会话状态。

        Args:
            agent_id: 业务员ID
            plates: 车牌号列表

        Returns:
            批量查询结果
        """
        plate_norms = [normalize_plate(plate) for plate in plates]
        found = await session_store.find_by_plates(plate_norms)

        results = []
        for plate, plate_norm in zip(plates, plate_norms):
            session = found.get(plate_norm)
            if session:
                results.append({
                    "plate": plate_norm,
                    "session_id": session.session_id,
                    "phase": session.phase.value,
                    "plan": session.insurance_plan.to_dict() if session.insurance_plan else None,
                })
            else:
                results.append({
                    "plate": plate_norm,
                    "session_id": None,
                    "phase": "not_found",
                    "plan": None,
                })

        return {
            "success": True,
            "agent_id": agent_id,
            "results": results,
            "total": len(results),
        }

    async def batch_create_sessions(
        self, agent_id: str, plates: list[str]
    ) -> dict:
        """
        业务员批量创建会话。

        Args:
            agent_id: 业务员ID
            plates: 车牌号列表

        Returns:
            批量创建结果
        """
        results = []
        valid_plates = []
        for plate in plates:
            plate_norm = normalize_plate(plate)
            if not is_valid_plate(plate_norm):
                results.append({"plate": plate, "error": "车牌号格式不正确"})
                continue
            valid_plates.append((plate, plate_norm))

        # 一次性批量查找已有会话，避免逐牌 N+1 查询
        existing = await session_store.find_by_plates([pn for _, pn in valid_plates])

        for plate, plate_norm in valid_plates:
            session = existing.get(plate_norm)
            if session and session.phase != WorkflowPhase.COMPLETED:
                # 复用活跃会话
                results.append({
                    "plate": plate_norm,
                    "session_id": session.session_id,
                })
                continue

            session = await session_store.get_or_create_by_plate(
                plate_norm, agent_id, role=UserRole.AGENT
            )
            results.append({
                "plate": plate_norm,
                "session_id": session.session_id,
            })

        return {
            "success": True,
            "agent_id": agent_id,
            "results": results,
            "total": len(results),
        }


engine = InsuranceWorkflowEngine()
