"""
车险出单智能体 - 数据模型 / Schema 定义
"""
from __future__ import annotations

import uuid
from datetime import datetime, date, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, List, Union, get_origin, get_args, get_type_hints
from dataclasses import dataclass, field, fields, is_dataclass

from src.utils.crypto import encrypt_pii_fields, decrypt_pii_fields


def now_utc() -> datetime:
    """返回带 UTC 时区的当前时间，避免 naive datetime 导致的时区/TTL 比较错误。"""
    return datetime.now(timezone.utc)


# ==================== 枚举定义 ====================

class InsuranceType(str, Enum):
    """保险大类"""
    CAR = "car"
    PROPERTY = "property"
    LIFE = "life"


class UserRole(str, Enum):
    """用户角色"""
    CUSTOMER = "customer"
    AGENT = "agent"


class WorkflowPhase(str, Enum):
    """车险业务流程的七个阶段"""
    INSURANCE_TYPE_SELECTION = "insurance_type_selection"
    COLLECTING_INFO = "collecting_info"
    RECOMMENDING = "recommending"
    IDENTITY_VERIFICATION = "identity_verification"
    QUOTING = "quoting"
    UNDERWRITING = "underwriting"
    PAYMENT = "payment"
    COMPLETED = "completed"
    TRANSFERRED_TO_HUMAN = "transferred_to_human"


class CustomerType(str, Enum):
    """客户类型"""
    NEW = "new"
    RETURNING = "returning"


class UnderwritingStatus(str, Enum):
    """核保状态"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_VERIFICATION = "needs_verification"


class PaymentStatus(str, Enum):
    """支付状态"""
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


# ==================== 险种定义 ====================

@dataclass
class InsuranceItem:
    """单个险种"""
    name: str
    code: str
    coverage_amount: Decimal
    premium: Decimal
    deductible: Decimal = Decimal("0")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "code": self.code,
            "coverage_amount": str(self.coverage_amount),
            "premium": str(self.premium),
            "deductible": str(self.deductible),
        }

    def modify_coverage(self, new_amount: Decimal):
        """自然语言修改保额"""
        self.coverage_amount = new_amount


# 预设险种模板
PRESET_INSURANCE_ITEMS = {
    "compulsory": InsuranceItem(
        name="交强险", code="COMPULSORY",
        coverage_amount=Decimal("122000"), premium=Decimal("950"), deductible=Decimal("0")
    ),
    "third_party_500k": InsuranceItem(
        name="第三者责任险", code="THIRD_PARTY",
        coverage_amount=Decimal("500000"), premium=Decimal("1200"), deductible=Decimal("0")
    ),
    "third_party_1m": InsuranceItem(
        name="第三者责任险", code="THIRD_PARTY",
        coverage_amount=Decimal("1000000"), premium=Decimal("1800"), deductible=Decimal("0")
    ),
    "third_party_2m": InsuranceItem(
        name="第三者责任险", code="THIRD_PARTY",
        coverage_amount=Decimal("2000000"), premium=Decimal("2500"), deductible=Decimal("0")
    ),
    "vehicle_loss": InsuranceItem(
        name="车辆损失险", code="VEHICLE_LOSS",
        coverage_amount=Decimal("150000"), premium=Decimal("2200"), deductible=Decimal("500")
    ),
    "driver_passenger": InsuranceItem(
        name="车上人员责任险", code="DRIVER_PASSENGER",
        coverage_amount=Decimal("10000"), premium=Decimal("200"), deductible=Decimal("0")
    ),
    "scratch": InsuranceItem(
        name="车身划痕险", code="SCRATCH",
        coverage_amount=Decimal("5000"), premium=Decimal("400"), deductible=Decimal("200")
    ),
    "theft": InsuranceItem(
        name="盗抢险", code="THEFT",
        coverage_amount=Decimal("150000"), premium=Decimal("800"), deductible=Decimal("500")
    ),
    "glass": InsuranceItem(
        name="玻璃单独破碎险", code="GLASS",
        coverage_amount=Decimal("5000"), premium=Decimal("150"), deductible=Decimal("0")
    ),
    "spontaneous": InsuranceItem(
        name="自燃损失险", code="SPONTANEOUS",
        coverage_amount=Decimal("150000"), premium=Decimal("300"), deductible=Decimal("500")
    ),
    "wading": InsuranceItem(
        name="发动机涉水险", code="WADING",
        coverage_amount=Decimal("150000"), premium=Decimal("250"), deductible=Decimal("500")
    ),
}


# ==================== 保险公司定义 ====================

@dataclass
class InsuranceCompany:
    """保险公司"""
    name: str
    code: str
    available_items: List[str] = field(default_factory=list)
    discount_rate: Decimal = Decimal("1.0")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "code": self.code,
            "available_items": self.available_items,
            "discount_rate": str(self.discount_rate),
        }


PRESET_COMPANIES = {
    "pingan": InsuranceCompany(
        name="平安保险", code="PINGAN",
        available_items=["compulsory", "third_party_500k", "third_party_1m", "third_party_2m",
                         "vehicle_loss", "driver_passenger", "scratch", "theft", "glass"],
        discount_rate=Decimal("0.85")
    ),
    "picc": InsuranceCompany(
        name="人保财险", code="PICC",
        available_items=["compulsory", "third_party_500k", "third_party_1m", "third_party_2m",
                         "vehicle_loss", "driver_passenger", "theft", "spontaneous", "wading"],
        discount_rate=Decimal("0.90")
    ),
    "cpic": InsuranceCompany(
        name="太平洋保险", code="CPIC",
        available_items=["compulsory", "third_party_500k", "third_party_1m", "third_party_2m",
                         "vehicle_loss", "driver_passenger", "scratch", "glass", "wading"],
        discount_rate=Decimal("0.88")
    ),
}


# ==================== 核心数据模型 ====================

@dataclass
class VehicleInfo:
    """车辆信息（从行驶证OCR或历史数据获取）"""
    license_plate: str = ""
    vin: str = ""
    engine_number: str = ""
    brand_model: str = ""
    registration_date: Optional[date] = None
    owner_name: str = ""
    use_type: str = "非营运"
    seat_count: int = 5
    vehicle_type: str = "小型轿车"

    def to_dict(self) -> dict:
        return {
            "license_plate": self.license_plate,
            "vin": self.vin,
            "engine_number": self.engine_number,
            "brand_model": self.brand_model,
            "registration_date": self.registration_date.isoformat() if self.registration_date else None,
            "owner_name": self.owner_name,
            "use_type": self.use_type,
            "seat_count": self.seat_count,
            "vehicle_type": self.vehicle_type,
        }

    def is_complete(self) -> bool:
        return bool(self.license_plate and self.vin and self.brand_model)


@dataclass
class IDCardInfo:
    """身份证信息"""
    name: str = ""
    id_number: str = ""
    gender: str = ""
    ethnicity: str = ""
    birth_date: Optional[date] = None
    address: str = ""
    issuing_authority: str = ""
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    front_image_url: str = ""
    back_image_url: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "id_number": self.id_number[-4:].rjust(len(self.id_number), "*") if self.id_number else "",
            "gender": self.gender,
            "address": self.address,
            "front_image_url": self.front_image_url,
            "back_image_url": self.back_image_url,
        }

    def is_complete(self) -> bool:
        return bool(self.name and self.id_number)


@dataclass
class InsurancePlan:
    """投保方案"""
    company: Optional[InsuranceCompany] = None
    items: List[InsuranceItem] = field(default_factory=list)
    total_premium: Decimal = Decimal("0")
    discount_rate: Decimal = Decimal("1.0")
    is_last_year_plan: bool = False

    def calculate_total(self):
        total = sum(item.premium for item in self.items)
        self.total_premium = (total * self.discount_rate).quantize(Decimal("0.01"))
        return self.total_premium

    def to_dict(self) -> dict:
        return {
            "company": self.company.to_dict() if self.company else None,
            "items": [item.to_dict() for item in self.items],
            "total_premium": str(self.total_premium),
            "discount_rate": str(self.discount_rate),
            "is_last_year_plan": self.is_last_year_plan,
        }


@dataclass
class Quotation:
    """报价单"""
    quotation_id: str = ""
    version: str = "1"
    plan: Optional[InsurancePlan] = None
    vehicle: Optional[VehicleInfo] = None
    applicant: Optional[IDCardInfo] = None
    insured: Optional[IDCardInfo] = None
    effective_date: Optional[date] = None
    expire_date: Optional[date] = None
    created_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    rate_source: str = ""
    provider_reference: str = ""
    request_digest: str = ""
    raw_response: str = ""
    is_estimate: bool = False
    status: str = "draft"
    knowledge_sources: List[dict] = field(default_factory=list)
    validation_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "quotation_id": self.quotation_id,
            "version": self.version,
            "total_premium": (
                str(self.plan.total_premium) if self.plan else "0"
            ),
            "plan": self.plan.to_dict() if self.plan else None,
            "vehicle": self.vehicle.to_dict() if self.vehicle else None,
            "applicant": self.applicant.to_dict() if self.applicant else None,
            "insured": self.insured.to_dict() if self.insured else None,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "expire_date": self.expire_date.isoformat() if self.expire_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "rate_source": self.rate_source,
            "provider_reference": self.provider_reference,
            "request_digest": self.request_digest,
            "is_estimate": self.is_estimate,
            "status": self.status,
            "knowledge_sources": self.knowledge_sources,
            "validation_warnings": self.validation_warnings,
        }


@dataclass
class UnderwritingResult:
    """核保结果"""
    status: UnderwritingStatus = UnderwritingStatus.PENDING
    message: str = ""
    requires_photo: bool = False
    requires_video: bool = False
    checked_at: Optional[datetime] = None
    provider_reference: str = ""
    request_digest: str = ""
    raw_response: str = ""
    quote_version: str = ""
    approved_amount: Decimal = Decimal("0")

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "message": self.message,
            "requires_photo": self.requires_photo,
            "requires_video": self.requires_video,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "provider_reference": self.provider_reference,
            "request_digest": self.request_digest,
            "quote_version": self.quote_version,
            "approved_amount": str(self.approved_amount),
        }


@dataclass
class PaymentResult:
    """支付结果"""
    status: PaymentStatus = PaymentStatus.PENDING
    transaction_id: str = ""
    payment_url: str = ""
    paid_amount: Decimal = Decimal("0")
    paid_at: Optional[datetime] = None
    quotation_id: str = ""
    quote_version: str = ""
    expected_amount: Decimal = Decimal("0")
    provider_reference: str = ""
    raw_response: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "transaction_id": self.transaction_id,
            "payment_url": self.payment_url,
            "paid_amount": str(self.paid_amount),
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "quotation_id": self.quotation_id,
            "quote_version": self.quote_version,
            "expected_amount": str(self.expected_amount),
            "provider_reference": self.provider_reference,
        }


@dataclass
class Policy:
    """保险单"""
    policy_id: str = ""
    policy_number: str = ""
    quotation: Optional[Quotation] = None
    applicant: Optional[IDCardInfo] = None
    insured: Optional[IDCardInfo] = None
    vehicle: Optional[VehicleInfo] = None
    items_detail: List[InsuranceItem] = field(default_factory=list)
    total_premium: Decimal = Decimal("0")
    effective_date: Optional[date] = None
    expire_date: Optional[date] = None
    terms_and_conditions: List[str] = field(default_factory=list)
    issued_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "policy_number": self.policy_number,
            "applicant": self.applicant.to_dict() if self.applicant else None,
            "insured": self.insured.to_dict() if self.insured else None,
            "vehicle": self.vehicle.to_dict() if self.vehicle else None,
            "items_detail": [item.to_dict() for item in self.items_detail],
            "total_premium": str(self.total_premium),
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "expire_date": self.expire_date.isoformat() if self.expire_date else None,
            "terms_and_conditions": self.terms_and_conditions,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
        }


# ==================== 状态序列化（用于持久化，保留完整字段） ====================


def _to_jsonable(value):
    """递归将 dataclass / 枚举 / Decimal / 日期 等转换为 JSON 可序列化结构。

    与展示用途的 ``to_dict`` 不同，此处会完整保留所有嵌套字段，
    保证持久化后可以无损还原会话对象。
    """
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return value


def _from_jsonable(cls, value):
    """从 JSON 结构递归还原 dataclass / 枚举 / Decimal / 日期 等。

    ``cls`` 为期望的目标类型（通过 ``get_type_hints`` 解析字符串注解）。
    """
    if value is None:
        return None

    origin = get_origin(cls)
    if origin is Union:
        args = [a for a in get_args(cls) if a is not type(None)]
        if len(args) == 1:
            cls = args[0]
            origin = get_origin(cls)
    if origin is list:
        item_cls = get_args(cls)[0] if get_args(cls) else Any
        return [_from_jsonable(item_cls, v) for v in value]
    if origin is dict:
        val_cls = get_args(cls)[1] if len(get_args(cls)) == 2 else Any
        return {k: _from_jsonable(val_cls, v) for k, v in value.items()}
    if cls is Decimal:
        return Decimal(value)
    if cls is datetime:
        dt = datetime.fromisoformat(value)
        # 历史数据可能存的是无时区字符串，统一按 UTC 解释，保证 .timestamp() 与 time.time() 一致
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    if cls is date:
        return date.fromisoformat(value)
    if isinstance(cls, type) and issubclass(cls, Enum):
        return cls(value)
    if isinstance(cls, type) and is_dataclass(cls):
        hints = get_type_hints(cls)
        kwargs = {}
        for f in fields(cls):
            field_type = hints.get(f.name, f.type)
            kwargs[f.name] = _from_jsonable(field_type, value.get(f.name))
        return cls(**kwargs)
    return value


# ==================== 会话模型 ====================

@dataclass
class Session:
    """会话 - 以车牌号命名"""
    session_id: str = ""
    agent_id: str = ""
    user_id: str = ""
    role: UserRole = UserRole.CUSTOMER
    license_plate: str = ""
    insurance_type: InsuranceType = InsuranceType.CAR
    phase: WorkflowPhase = WorkflowPhase.INSURANCE_TYPE_SELECTION
    customer_type: CustomerType = CustomerType.NEW

    vehicle_info: Optional[VehicleInfo] = None
    applicant_id_card: Optional[IDCardInfo] = None
    insured_id_card: Optional[IDCardInfo] = None
    insurance_plan: Optional[InsurancePlan] = None
    quotation: Optional[Quotation] = None
    underwriting_result: Optional[UnderwritingResult] = None
    payment_result: Optional[PaymentResult] = None
    policy: Optional[Policy] = None

    # 人工接管期间的会话消息，随会话状态一起加密持久化
    conversation_messages: List[dict] = field(default_factory=list)

    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def __post_init__(self):
        # 兼容新增字段之前持久化的历史会话。
        if self.conversation_messages is None:
            self.conversation_messages = []

    def generate_id(self):
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        return self.session_id

    def set_license_plate(self, plate: str):
        self.license_plate = plate
        self.updated_at = now_utc()

    def advance_phase(self, phase: WorkflowPhase):
        self.phase = phase
        self.updated_at = now_utc()

    def append_conversation_message(
        self,
        role: str,
        content: str,
        sender_id: str = "",
        sender_name: str = "",
        metadata: Optional[dict] = None,
    ) -> dict:
        message = {
            "id": str(uuid.uuid4()),
            "role": role,
            "content": content,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "metadata": metadata or {},
            "created_at": now_utc().isoformat(),
        }
        self.conversation_messages.append(message)
        self.conversation_messages = self.conversation_messages[-200:]
        self.updated_at = now_utc()
        return message

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "role": self.role.value,
            "license_plate": self.license_plate,
            "insurance_type": self.insurance_type.value,
            "phase": self.phase.value,
            "customer_type": self.customer_type.value,
        }

    def to_state_dict(self) -> dict:
        """转换为可持久化的完整状态结构（用于 Redis / MySQL 存储）。

        敏感个人信息字段（姓名/身份证/住址/电话/车牌/车架号等）在此加密，
        保证落盘/进缓存前已是密文。
        """
        return encrypt_pii_fields(_to_jsonable(self))

    @classmethod
    def from_state_dict(cls, data: dict) -> "Session":
        """从持久化状态结构无损还原会话对象（自动解密敏感字段）。"""
        return _from_jsonable(cls, decrypt_pii_fields(data))
