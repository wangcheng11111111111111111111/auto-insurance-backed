"""
车险出单智能体 - 人工接管服务
当 OCR 识别信息不正确时，触发转人工接口
"""
import logging
from datetime import datetime
from typing import Optional

from src.api.schemas import Session, WorkflowPhase
from src.storage.mysql import get_db
from src.storage.repository import TakeoverRepository
from src.services.notification import notification_hub

logger = logging.getLogger(__name__)


class HumanService:
    """人工接管服务"""

    async def request_human_takeover(self, session: Session, reason: str) -> dict:
        """
        触发转人工流程：记录工单并实时推送「新工单」事件给坐席端。

        Args:
            session: 当前会话
            reason: 转人工原因

        Returns:
            转接结果
        """
        logger.warning(
            "转人工: session=%s, plate=%s, reason=%s",
            session.session_id, session.license_plate, reason,
        )

        source_phase = session.phase.value
        session.append_conversation_message(
            "system",
            f"已转接人工处理：{reason}",
            sender_name="系统",
        )
        session.advance_phase(WorkflowPhase.TRANSFERRED_TO_HUMAN)

        transfer_id = f"HT-{session.session_id[:8]}-{int(datetime.now().timestamp())}"
        transferred_at = datetime.now().isoformat()

        # 工单持久化（MySQL 不可用时优雅降级，不阻断转人工流程）
        try:
            async with get_db() as db:
                await TakeoverRepository.create(
                    db,
                    transfer_id=transfer_id,
                    session_id=session.session_id,
                    reason=reason,
                    source_phase=source_phase,
                    handle_status="pending",
                )
        except Exception as e:
            logger.warning("人工接管工单落库失败（不影响转人工）: %s", e)

        # 主动推送「新工单」事件给所有已连接的坐席端
        event = {
            "type": "new_ticket",
            "transfer_id": transfer_id,
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "license_plate": session.license_plate,
            "reason": reason,
            "source_phase": source_phase,
            "handle_status": "pending",
            "transferred_at": transferred_at,
        }
        try:
            await notification_hub.broadcast_all(event)
        except Exception as e:
            logger.warning("新工单推送失败: %s", e)

        return {
            "transferred": True,
            "transfer_id": transfer_id,
            "reason": reason,
            "message": "已为您转接人工客服，请稍候...",
            "transferred_at": transferred_at,
        }

    def check_ocr_quality(self, vehicle_info) -> tuple[bool, str]:
        """
        检查 OCR 识别质量。如信息不完整或不正确，返回需要转人工。

        Args:
            vehicle_info: OCR 识别出的车辆信息

        Returns:
            (is_ok, issue_description)
        """
        issues = []

        if not vehicle_info.license_plate:
            issues.append("未识别到车牌号")
        if not vehicle_info.vin or len(vehicle_info.vin) < 8:
            issues.append("车架号识别不完整")
        if not vehicle_info.brand_model:
            issues.append("未识别到品牌型号")

        if issues:
            return False, "；".join(issues)
        return True, ""


human_service = HumanService()
