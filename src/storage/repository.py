"""
Repository 层 - 数据库操作的业务级封装
每个 Repository 对应一张表，提供面向业务的查询方法
"""
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.storage.models import (
    SessionModel, VehicleInfoModel, IDCardInfoModel,
    InsurancePlanModel, PlanItemModel, QuotationModel,
    UnderwritingRecordModel, PaymentRecordModel,
    PolicyModel, PolicyItemModel, PolicyTermModel,
    HumanTakeoverModel, KnowledgeTermModel,
    KnowledgeCompanyMappingModel, KnowledgeScenarioModel,
    KnowledgeRequiredFieldModel, KnowledgeFollowupModel,
    KnowledgeRuleModel, KnowledgeScriptTemplateModel,
    OcrRecordModel, OperationLogModel,
    AgentModel, UserModel, InsuranceCompanyModel,
    InsuranceItemTemplateModel,
)

logger = logging.getLogger(__name__)


# ==================== Session ====================

class SessionRepository:
    """会话 Repository"""

    @staticmethod
    async def get_by_id(session: AsyncSession, session_id: str) -> Optional[SessionModel]:
        result = await session.execute(
            select(SessionModel).where(SessionModel.session_id == session_id).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_plate(session: AsyncSession, plate: str) -> Optional[SessionModel]:
        result = await session.execute(
            select(SessionModel).where(
                SessionModel.license_plate == plate,
                SessionModel.status == 1,
            ).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_plates(session: AsyncSession, plates) -> list[SessionModel]:
        """按车牌列表一次性 IN 查询（消除逐牌 N+1）。"""
        plates = [p for p in plates if p]
        if not plates:
            return []
        result = await session.execute(
            select(SessionModel).where(
                SessionModel.license_plate.in_(plates),
                SessionModel.status == 1,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_vin(session: AsyncSession, vin: str) -> Optional[SessionModel]:
        """按车架号查询活跃会话（明文索引列）。"""
        result = await session.execute(
            select(SessionModel).where(
                SessionModel.vin == vin,
                SessionModel.status == 1,
            ).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_plain_index_dirty(
        session: AsyncSession, limit: int = 1000,
    ) -> list[SessionModel]:
        """返回需回填明文索引（vin/车牌）的历史会话：vin 为空或车牌为密文。"""
        result = await session.execute(
            select(SessionModel).where(
                SessionModel.state_json.isnot(None),
                or_(
                    SessionModel.vin == "",
                    SessionModel.license_plate.like("enc:%"),
                ),
            ).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def update_plain_index(
        session: AsyncSession, session_id: str, license_plate: str, vin: str,
    ):
        """仅回填明文索引列（vin/车牌），不触碰 state_json 与其他字段。"""
        await session.execute(
            update(SessionModel).where(SessionModel.session_id == session_id).values(
                license_plate=license_plate,
                vin=vin,
                updated_at=datetime.now(),
            )
        )
        await session.flush()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> SessionModel:
        obj = SessionModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def update_phase(
        session: AsyncSession, session_id: str,
        phase: str, customer_type: Optional[str] = None,
    ):
        values = {"phase": phase, "updated_at": datetime.now()}
        if customer_type:
            values["customer_type"] = customer_type
        await session.execute(
            update(SessionModel).where(SessionModel.session_id == session_id).values(**values)
        )
        await session.flush()

    @staticmethod
    async def link_resource(
        session: AsyncSession, session_id: str, **resource_ids,
    ):
        """关联资源ID：vehicle_info_id, applicant_id_card_id, plan_id 等"""
        resource_ids["updated_at"] = datetime.now()
        await session.execute(
            update(SessionModel).where(SessionModel.session_id == session_id).values(**resource_ids)
        )
        await session.flush()

    @staticmethod
    async def close_session(session: AsyncSession, session_id: str):
        await session.execute(
            update(SessionModel).where(SessionModel.session_id == session_id).values(
                status=0, updated_at=datetime.now()
            )
        )
        await session.flush()

    @staticmethod
    async def get_user_sessions(
        session: AsyncSession, user_id: str, limit: int = 50,
    ) -> list[SessionModel]:
        result = await session.execute(
            select(SessionModel).where(
                SessionModel.user_id == user_id,
                SessionModel.status == 1,
            ).order_by(SessionModel.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_agent_sessions(
        session: AsyncSession, agent_id: str, limit: int = 100,
    ) -> list[SessionModel]:
        result = await session.execute(
            select(SessionModel).where(
                SessionModel.agent_id == agent_id,
                SessionModel.status == 1,
            ).order_by(SessionModel.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_by_status(
        session: AsyncSession, status: int = 1, limit: int = 100,
    ) -> list[SessionModel]:
        result = await session.execute(
            select(SessionModel).where(SessionModel.status == status).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def upsert(session: AsyncSession, session_id: str, **values) -> SessionModel:
        """幂等写入会话：按 session_id 存在则更新、否则创建（覆盖标量列 + state_json）。"""
        obj = await SessionRepository.get_by_id(session, session_id)
        values["updated_at"] = datetime.now()
        if obj is None:
            values.setdefault("created_at", datetime.now())
            obj = SessionModel(session_id=session_id, **values)
            session.add(obj)
        else:
            for key, val in values.items():
                setattr(obj, key, val)
        await session.flush()
        return obj


# ==================== Vehicle ====================

class VehicleRepository:

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[VehicleInfoModel]:
        return await session.get(VehicleInfoModel, id)

    @staticmethod
    async def get_by_plate(session: AsyncSession, plate: str) -> Optional[VehicleInfoModel]:
        result = await session.execute(
            select(VehicleInfoModel).where(VehicleInfoModel.license_plate == plate).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_vin(session: AsyncSession, vin: str) -> Optional[VehicleInfoModel]:
        result = await session.execute(
            select(VehicleInfoModel).where(VehicleInfoModel.vin == vin).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> VehicleInfoModel:
        obj = VehicleInfoModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def update(session: AsyncSession, id: int, **kwargs) -> Optional[VehicleInfoModel]:
        kwargs["updated_at"] = datetime.now()
        await session.execute(
            update(VehicleInfoModel).where(VehicleInfoModel.id == id).values(**kwargs)
        )
        await session.flush()
        return await session.get(VehicleInfoModel, id)


# ==================== ID Card ====================

class IDCardRepository:

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[IDCardInfoModel]:
        return await session.get(IDCardInfoModel, id)

    @staticmethod
    async def get_by_user(session: AsyncSession, user_id: str) -> list[IDCardInfoModel]:
        result = await session.execute(
            select(IDCardInfoModel).where(IDCardInfoModel.user_id == user_id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> IDCardInfoModel:
        obj = IDCardInfoModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj


# ==================== Insurance Plan ====================

class PlanRepository:

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[InsurancePlanModel]:
        from sqlalchemy.orm import selectinload
        result = await session.execute(
            select(InsurancePlanModel).where(InsurancePlanModel.id == id).options(
                selectinload(InsurancePlanModel.items)
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> InsurancePlanModel:
        obj = InsurancePlanModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def add_item(session: AsyncSession, plan_id: int, **kwargs) -> PlanItemModel:
        obj = PlanItemModel(plan_id=plan_id, **kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def clear_items(session: AsyncSession, plan_id: int):
        await session.execute(
            delete(PlanItemModel).where(PlanItemModel.plan_id == plan_id)
        )
        await session.flush()

    @staticmethod
    async def update_status(session: AsyncSession, id: int, status: str):
        await session.execute(
            update(InsurancePlanModel).where(InsurancePlanModel.id == id).values(
                status=status, updated_at=datetime.now()
            )
        )
        await session.flush()


# ==================== Quotation ====================

class QuotationRepository:

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[QuotationModel]:
        return await session.get(QuotationModel, id)

    @staticmethod
    async def get_by_quotation_id(session: AsyncSession, quotation_id: str) -> Optional[QuotationModel]:
        result = await session.execute(
            select(QuotationModel).where(QuotationModel.quotation_id == quotation_id).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_session(session: AsyncSession, session_id: str) -> Optional[QuotationModel]:
        result = await session.execute(
            select(QuotationModel).where(
                QuotationModel.session_id == session_id
            ).order_by(QuotationModel.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> QuotationModel:
        obj = QuotationModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def update_status(session: AsyncSession, id: int, status: str):
        await session.execute(
            update(QuotationModel).where(QuotationModel.id == id).values(
                status=status, updated_at=datetime.now()
            )
        )
        await session.flush()


# ==================== Underwriting ====================

class UnderwritingRepository:

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[UnderwritingRecordModel]:
        return await session.get(UnderwritingRecordModel, id)

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> UnderwritingRecordModel:
        obj = UnderwritingRecordModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def update_status(
        session: AsyncSession, id: int, status: str, message: str = "",
    ):
        values = {"status": status, "message": message}
        if status in ("approved", "rejected"):
            values["checked_at"] = datetime.now()
        await session.execute(
            update(UnderwritingRecordModel).where(UnderwritingRecordModel.id == id).values(**values)
        )
        await session.flush()


# ==================== Payment ====================

class PaymentRepository:

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[PaymentRecordModel]:
        return await session.get(PaymentRecordModel, id)

    @staticmethod
    async def get_by_transaction(session: AsyncSession, transaction_id: str) -> Optional[PaymentRecordModel]:
        result = await session.execute(
            select(PaymentRecordModel).where(PaymentRecordModel.transaction_id == transaction_id).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> PaymentRecordModel:
        obj = PaymentRecordModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def update_status(
        session: AsyncSession, id: int, status: str, paid_at: Optional[datetime] = None,
    ):
        values = {"status": status, "updated_at": datetime.now()}
        if paid_at:
            values["paid_at"] = paid_at
        await session.execute(
            update(PaymentRecordModel).where(PaymentRecordModel.id == id).values(**values)
        )
        await session.flush()


# ==================== Policy ====================

class PolicyRepository:

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[PolicyModel]:
        from sqlalchemy.orm import selectinload
        result = await session.execute(
            select(PolicyModel).where(PolicyModel.id == id).options(
                selectinload(PolicyModel.items),
                selectinload(PolicyModel.terms),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_policy_id(session: AsyncSession, policy_id: str) -> Optional[PolicyModel]:
        result = await session.execute(
            select(PolicyModel).where(PolicyModel.policy_id == policy_id).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_session(session: AsyncSession, session_id: str) -> Optional[PolicyModel]:
        result = await session.execute(
            select(PolicyModel).where(
                PolicyModel.session_id == session_id
            ).order_by(PolicyModel.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> PolicyModel:
        obj = PolicyModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def add_item(session: AsyncSession, policy_id: int, **kwargs) -> PolicyItemModel:
        obj = PolicyItemModel(policy_id=policy_id, **kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def add_term(session: AsyncSession, policy_id: int, term_order: int, content: str) -> PolicyTermModel:
        obj = PolicyTermModel(policy_id=policy_id, term_order=term_order, content=content)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def update_status(session: AsyncSession, id: int, status: str):
        await session.execute(
            update(PolicyModel).where(PolicyModel.id == id).values(
                status=status, updated_at=datetime.now()
            )
        )
        await session.flush()


# ==================== Knowledge ====================

class KnowledgeRepository:

    @staticmethod
    async def get_all_terms(session: AsyncSession) -> list[KnowledgeTermModel]:
        result = await session.execute(select(KnowledgeTermModel))
        return list(result.scalars().all())

    @staticmethod
    async def search_by_alias(session: AsyncSession, alias: str) -> list[KnowledgeTermModel]:
        result = await session.execute(
            select(KnowledgeTermModel).where(
                KnowledgeTermModel.alias.like(f"%{alias}%")
            ).limit(10)
        )
        return list(result.scalars().all())

    @staticmethod
    async def search_by_term(session: AsyncSession, term: str) -> list[KnowledgeTermModel]:
        result = await session.execute(
            select(KnowledgeTermModel).where(
                KnowledgeTermModel.standard_term.like(f"%{term}%")
            ).limit(10)
        )
        return list(result.scalars().all())

    @staticmethod
    async def upsert(session: AsyncSession, standard_term: str, alias: str, category: str = ""):
        result = await session.execute(
            select(KnowledgeTermModel).where(
                KnowledgeTermModel.standard_term == standard_term,
                KnowledgeTermModel.alias == alias,
            ).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.category = category or existing.category
        else:
            session.add(KnowledgeTermModel(
                standard_term=standard_term, alias=alias, category=category,
            ))
        await session.flush()


# ==================== Knowledge - Company Mappings ====================

class CompanyMappingRepository:

    @staticmethod
    async def get_all(session: AsyncSession) -> list[KnowledgeCompanyMappingModel]:
        result = await session.execute(select(KnowledgeCompanyMappingModel))
        return list(result.scalars().all())

    @staticmethod
    async def upsert(session: AsyncSession, slang: str, full_name: str, remark: str = ""):
        result = await session.execute(
            select(KnowledgeCompanyMappingModel).where(
                KnowledgeCompanyMappingModel.slang == slang,
            ).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.full_name = full_name
            existing.remark = remark or existing.remark
        else:
            session.add(KnowledgeCompanyMappingModel(
                slang=slang, full_name=full_name, remark=remark,
            ))
        await session.flush()

    @staticmethod
    async def delete_all(session: AsyncSession):
        await session.execute(delete(KnowledgeCompanyMappingModel))
        await session.flush()


# ==================== Knowledge - Scenarios ====================

class ScenarioRepository:

    @staticmethod
    async def get_all(session: AsyncSession) -> list[KnowledgeScenarioModel]:
        result = await session.execute(select(KnowledgeScenarioModel))
        return list(result.scalars().all())

    @staticmethod
    async def upsert(session: AsyncSession, slang: str, description: str):
        result = await session.execute(
            select(KnowledgeScenarioModel).where(
                KnowledgeScenarioModel.slang == slang,
            ).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.description = description
        else:
            session.add(KnowledgeScenarioModel(slang=slang, description=description))
        await session.flush()

    @staticmethod
    async def delete_all(session: AsyncSession):
        await session.execute(delete(KnowledgeScenarioModel))
        await session.flush()


# ==================== Knowledge - Required Fields ====================

class RequiredFieldRepository:

    @staticmethod
    async def get_all(session: AsyncSession) -> list[KnowledgeRequiredFieldModel]:
        result = await session.execute(
            select(KnowledgeRequiredFieldModel).order_by(KnowledgeRequiredFieldModel.sort_order)
        )
        return list(result.scalars().all())

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> KnowledgeRequiredFieldModel:
        obj = KnowledgeRequiredFieldModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def delete_all(session: AsyncSession):
        await session.execute(delete(KnowledgeRequiredFieldModel))
        await session.flush()


# ==================== Knowledge - Followups ====================

class FollowupRepository:

    @staticmethod
    async def get_all(session: AsyncSession) -> list[KnowledgeFollowupModel]:
        result = await session.execute(select(KnowledgeFollowupModel))
        return list(result.scalars().all())

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> KnowledgeFollowupModel:
        obj = KnowledgeFollowupModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def delete_all(session: AsyncSession):
        await session.execute(delete(KnowledgeFollowupModel))
        await session.flush()


# ==================== Knowledge - Rules ====================

class RuleRepository:

    @staticmethod
    async def get_all(session: AsyncSession) -> list[KnowledgeRuleModel]:
        result = await session.execute(
            select(KnowledgeRuleModel).order_by(KnowledgeRuleModel.sort_order)
        )
        return list(result.scalars().all())

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> KnowledgeRuleModel:
        obj = KnowledgeRuleModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def delete_all(session: AsyncSession):
        await session.execute(delete(KnowledgeRuleModel))
        await session.flush()


# ==================== Knowledge - Script Templates ====================

class ScriptTemplateRepository:

    @staticmethod
    async def get_all(session: AsyncSession) -> list[KnowledgeScriptTemplateModel]:
        result = await session.execute(select(KnowledgeScriptTemplateModel))
        return list(result.scalars().all())

    @staticmethod
    async def search_by_intent(
        session: AsyncSession, query: str, limit: int = 10,
    ) -> list[KnowledgeScriptTemplateModel]:
        result = await session.execute(
            select(KnowledgeScriptTemplateModel).where(
                KnowledgeScriptTemplateModel.intent.like(f"%{query}%")
            ).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def upsert(session: AsyncSession, **kwargs) -> KnowledgeScriptTemplateModel:
        code = kwargs.get("code", "")
        result = await session.execute(
            select(KnowledgeScriptTemplateModel).where(
                KnowledgeScriptTemplateModel.code == code,
            ).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            for key, value in kwargs.items():
                if value:
                    setattr(existing, key, value)
            await session.flush()
            return existing
        else:
            obj = KnowledgeScriptTemplateModel(**kwargs)
            session.add(obj)
            await session.flush()
            return obj

    @staticmethod
    async def delete_all(session: AsyncSession):
        await session.execute(delete(KnowledgeScriptTemplateModel))
        await session.flush()


# ==================== Human Takeover ====================

class TakeoverRepository:

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> HumanTakeoverModel:
        obj = HumanTakeoverModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj

    @staticmethod
    async def resolve(session: AsyncSession, id: int, handler_id: str, remark: str = "") -> bool:
        result = await session.execute(
            update(HumanTakeoverModel).where(HumanTakeoverModel.id == id).values(
                handle_status="resolved",
                handler_id=handler_id,
                handle_remark=remark,
                resolved_at=datetime.now(),
            )
        )
        await session.flush()
        return result.rowcount > 0

    @staticmethod
    async def claim(session: AsyncSession, id: int, handler_id: str) -> bool:
        result = await session.execute(
            update(HumanTakeoverModel).where(
                HumanTakeoverModel.id == id,
                HumanTakeoverModel.handle_status == "pending",
            ).values(
                handle_status="processing",
                handler_id=handler_id,
            )
        )
        await session.flush()
        return result.rowcount > 0

    @staticmethod
    async def get_by_id(session: AsyncSession, id: int) -> Optional[HumanTakeoverModel]:
        return await session.get(HumanTakeoverModel, id)

    @staticmethod
    async def get_active_by_session(
        session: AsyncSession, session_id: str
    ) -> Optional[HumanTakeoverModel]:
        result = await session.execute(
            select(HumanTakeoverModel).where(
                HumanTakeoverModel.session_id == session_id,
                HumanTakeoverModel.handle_status.in_(["pending", "processing"]),
            ).order_by(HumanTakeoverModel.requested_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_pending(session: AsyncSession, limit: int = 20) -> list[HumanTakeoverModel]:
        result = await session.execute(
            select(HumanTakeoverModel).where(
                HumanTakeoverModel.handle_status == "pending"
            ).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_by_status(
        session: AsyncSession,
        status: str = "pending",
        limit: int = 50,
    ) -> list[HumanTakeoverModel]:
        stmt = select(HumanTakeoverModel)
        if status != "all":
            stmt = stmt.where(HumanTakeoverModel.handle_status == status)
        stmt = stmt.order_by(HumanTakeoverModel.requested_at.asc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def counts(session: AsyncSession) -> dict[str, int]:
        result = await session.execute(
            select(
                HumanTakeoverModel.handle_status,
                func.count(HumanTakeoverModel.id),
            ).group_by(HumanTakeoverModel.handle_status)
        )
        return {status: count for status, count in result.all()}


# ==================== Operation Log ====================

class OperationLogRepository:

    @staticmethod
    async def log(
        session: AsyncSession,
        session_id: str = "",
        operator_id: str = "",
        operator_role: str = "",
        action: str = "",
        phase_from: str = "",
        phase_to: str = "",
        detail: dict = None,
        ip_address: str = "",
    ):
        obj = OperationLogModel(
            session_id=session_id,
            operator_id=operator_id,
            operator_role=operator_role,
            action=action,
            phase_from=phase_from,
            phase_to=phase_to,
            detail=detail or {},
            ip_address=ip_address,
        )
        session.add(obj)
        await session.flush()

    @staticmethod
    async def get_by_session(
        session: AsyncSession, session_id: str, limit: int = 100,
    ) -> list[OperationLogModel]:
        result = await session.execute(
            select(OperationLogModel).where(
                OperationLogModel.session_id == session_id
            ).order_by(OperationLogModel.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_operator(
        session: AsyncSession, operator_id: str, limit: int = 100,
    ) -> list[OperationLogModel]:
        result = await session.execute(
            select(OperationLogModel).where(
                OperationLogModel.operator_id == operator_id
            ).order_by(OperationLogModel.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


# ==================== Agent & User ====================

class AgentRepository:

    @staticmethod
    async def get_by_agent_id(session: AsyncSession, agent_id: str) -> Optional[AgentModel]:
        result = await session.execute(
            select(AgentModel).where(AgentModel.agent_id == agent_id).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> AgentModel:
        obj = AgentModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj


class UserRepository:

    @staticmethod
    async def get_by_user_id(session: AsyncSession, user_id: str) -> Optional[UserModel]:
        result = await session.execute(
            select(UserModel).where(UserModel.user_id == user_id).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> UserModel:
        obj = UserModel(**kwargs)
        session.add(obj)
        await session.flush()
        return obj
