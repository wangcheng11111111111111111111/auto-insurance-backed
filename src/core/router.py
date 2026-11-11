"""
车险出单智能体 - 路由分发器
按保险类型（车险/财险/人寿险）分发到对应的工作流
"""
import logging
from typing import Optional

from src.api.schemas import InsuranceType, Session

logger = logging.getLogger(__name__)


class InsuranceRouter:
    """保险类型路由器 - 未来扩展财险/人寿险的入口"""

    SUPPORTED_TYPES = {InsuranceType.CAR}

    def can_handle(self, insurance_type: InsuranceType) -> bool:
        """检查是否支持该保险类型"""
        return insurance_type in self.SUPPORTED_TYPES

    def get_supported_types(self) -> list[dict]:
        """获取当前支持的保险类型列表"""
        return [
            {"type": InsuranceType.CAR.value, "name": "车险", "available": True},
            {"type": InsuranceType.PROPERTY.value, "name": "财险", "available": False},
            {"type": InsuranceType.LIFE.value, "name": "人寿险", "available": False},
        ]

    def route_session(self, session: Session) -> Optional[str]:
        """
        根据会话中的保险类型路由到对应的处理引擎。

        Args:
            session: 当前会话

        Returns:
            引擎标识符
        """
        if session.insurance_type == InsuranceType.CAR:
            return "car_insurance_engine"
        elif session.insurance_type == InsuranceType.PROPERTY:
            return "property_insurance_engine"
        elif session.insurance_type == InsuranceType.LIFE:
            return "life_insurance_engine"
        return None


router = InsuranceRouter()