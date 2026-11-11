"""
SQLAlchemy ORM 模型 - 对应 db/init.sql 中所有表
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Column, BigInteger, String, Integer, Float, Date,
    DateTime, Text, JSON, ForeignKey, DECIMAL, SmallInteger,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship

from src.storage.mysql import Base


class AgentModel(Base):
    __tablename__ = "agents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    agent_id = Column(String(64), unique=True, nullable=False, comment="业务员唯一标识")
    name = Column(String(100), nullable=False, comment="业务员姓名")
    phone = Column(String(20), default="", comment="手机号")
    employee_no = Column(String(50), default="", comment="工号")
    department = Column(String(100), default="", comment="所属部门")
    password_hash = Column(String(255), default="", comment="密码哈希")
    status = Column(SmallInteger, default=1, comment="状态: 1-在职 0-离职")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class UserModel(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(64), unique=True, nullable=False, comment="用户唯一标识")
    name = Column(String(100), default="", comment="用户姓名")
    id_number = Column(String(18), default="", comment="身份证号（加密存储）")
    phone = Column(String(20), default="", comment="手机号")
    agent_id = Column(String(64), default="", comment="关联业务员ID")
    password_hash = Column(String(255), default="", comment="密码哈希")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_user_phone", "phone"),
        Index("idx_user_agent", "agent_id"),
    )


class VehicleInfoModel(Base):
    __tablename__ = "vehicle_info"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    license_plate = Column(String(10), nullable=False, comment="车牌号")
    vin = Column(String(17), default="", comment="车架号(VIN)")
    engine_number = Column(String(50), default="", comment="发动机号")
    brand_model = Column(String(100), default="", comment="品牌型号")
    registration_date = Column(Date, nullable=True, comment="注册日期")
    owner_name = Column(String(100), default="", comment="所有人姓名")
    use_type = Column(String(20), default="非营运", comment="使用性质")
    seat_count = Column(Integer, default=5, comment="核定载客数")
    vehicle_type = Column(String(30), default="小型轿车", comment="车辆类型")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_plate", "license_plate", unique=True),
        Index("idx_vin", "vin"),
    )


class IDCardInfoModel(Base):
    __tablename__ = "id_card_info"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(64), default="", comment="关联用户ID")
    name = Column(String(100), nullable=False, comment="姓名")
    id_number = Column(String(18), nullable=False, comment="身份证号")
    gender = Column(String(10), default="", comment="性别")
    ethnicity = Column(String(20), default="", comment="民族")
    birth_date = Column(Date, nullable=True, comment="出生日期")
    address = Column(String(500), default="", comment="住址")
    issuing_authority = Column(String(200), default="", comment="签发机关")
    valid_from = Column(Date, nullable=True, comment="有效期起")
    valid_to = Column(Date, nullable=True, comment="有效期止")
    front_image_url = Column(String(500), default="", comment="身份证正面照URL")
    back_image_url = Column(String(500), default="", comment="身份证背面照URL")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_id_card_user", "user_id"),
    )


class InsuranceCompanyModel(Base):
    __tablename__ = "insurance_companies"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False, comment="公司代码")
    name = Column(String(100), nullable=False, comment="公司名称")
    discount_rate = Column(DECIMAL(4, 3), default=1.0, comment="默认折扣率")
    contact_phone = Column(String(20), default="", comment="联系电话")
    status = Column(SmallInteger, default=1, comment="状态: 1-启用 0-停用")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class InsuranceItemTemplateModel(Base):
    __tablename__ = "insurance_item_templates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False, comment="险种代码")
    name = Column(String(50), nullable=False, comment="险种名称")
    coverage_amount = Column(DECIMAL(12, 2), default=0, comment="默认保额")
    premium = Column(DECIMAL(10, 2), default=0, comment="参考保费")
    deductible = Column(DECIMAL(10, 2), default=0, comment="免赔额")
    is_compulsory = Column(SmallInteger, default=0, comment="是否强制险")
    sort_order = Column(Integer, default=0, comment="排序")
    status = Column(SmallInteger, default=1, comment="状态: 1-启用 0-停用")
    created_at = Column(DateTime, default=datetime.now)


class SessionModel(Base):
    __tablename__ = "sessions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(64), unique=True, nullable=False, comment="会话唯一标识")
    agent_id = Column(String(64), nullable=False, comment="业务员ID")
    user_id = Column(String(64), default="", comment="用户ID")
    role = Column(String(20), default="customer", comment="角色")
    license_plate = Column(String(10), default="", comment="关联车牌号")
    vin = Column(String(64), default="", comment="关联车架号（明文索引，state_json 中为密文）")
    insurance_type = Column(String(20), default="car", comment="保险类型")
    phase = Column(String(40), default="insurance_type_selection", comment="当前阶段")
    customer_type = Column(String(20), default="new", comment="客户类型: new/returning")
    vehicle_info_id = Column(BigInteger, nullable=True, comment="关联车辆信息")
    applicant_id_card_id = Column(BigInteger, nullable=True, comment="投保人身份证ID")
    insured_id_card_id = Column(BigInteger, nullable=True, comment="被保人身份证ID")
    insurance_plan_id = Column(BigInteger, nullable=True, comment="关联投保方案")
    quotation_id = Column(BigInteger, nullable=True, comment="关联报价单")
    underwriting_id = Column(BigInteger, nullable=True, comment="关联核保记录")
    payment_id = Column(BigInteger, nullable=True, comment="关联支付记录")
    policy_id = Column(BigInteger, nullable=True, comment="关联保单")
    state_json = Column(Text, nullable=True, comment="会话完整状态 JSON 快照（Redis/内存恢复源）")
    status = Column(SmallInteger, default=1, comment="状态: 1-活跃 0-已关闭")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_session_agent", "agent_id"),
        Index("idx_session_user", "user_id"),
        Index("idx_session_plate", "license_plate"),
        Index("idx_session_vin", "vin"),
        Index("idx_session_phase", "phase"),
    )


class InsurancePlanModel(Base):
    __tablename__ = "insurance_plans"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=False, comment="关联会话")
    company_code = Column(String(20), default="", comment="保险公司代码")
    discount_rate = Column(DECIMAL(4, 3), default=1.0, comment="折扣率")
    total_premium = Column(DECIMAL(10, 2), default=0, comment="总保费")
    is_last_year = Column(SmallInteger, default=0, comment="是否沿用去年方案")
    status = Column(String(20), default="draft", comment="状态: draft/confirmed/modified")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    items = relationship("PlanItemModel", back_populates="plan", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_plan_session", "session_id"),
    )


class PlanItemModel(Base):
    __tablename__ = "plan_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    plan_id = Column(BigInteger, ForeignKey("insurance_plans.id", ondelete="CASCADE"), nullable=False)
    item_code = Column(String(30), nullable=False, comment="险种代码")
    item_name = Column(String(50), nullable=False, comment="险种名称")
    coverage_amount = Column(DECIMAL(12, 2), nullable=False, comment="保额")
    premium = Column(DECIMAL(10, 2), nullable=False, comment="保费")
    deductible = Column(DECIMAL(10, 2), default=0, comment="免赔额")
    created_at = Column(DateTime, default=datetime.now)

    plan = relationship("InsurancePlanModel", back_populates="items")

    __table_args__ = (
        Index("idx_plan_item_plan", "plan_id"),
    )


class QuotationModel(Base):
    __tablename__ = "quotations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    quotation_id = Column(String(64), unique=True, nullable=False, comment="报价单唯一标识")
    session_id = Column(String(64), nullable=False, comment="关联会话")
    plan_id = Column(BigInteger, nullable=True, comment="关联方案")
    vehicle_id = Column(BigInteger, nullable=True, comment="关联车辆信息")
    applicant_id = Column(BigInteger, nullable=True, comment="投保人身份证ID")
    insured_id = Column(BigInteger, nullable=True, comment="被保人身份证ID")
    effective_date = Column(Date, nullable=True, comment="生效日期")
    expire_date = Column(Date, nullable=True, comment="截止日期")
    total_premium = Column(DECIMAL(10, 2), default=0, comment="总保费")
    status = Column(String(30), default="draft", comment="draft/pending_underwriting/approved/rejected/paid")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_quotation_session", "session_id"),
        Index("idx_quotation_status", "status"),
    )


class UnderwritingRecordModel(Base):
    __tablename__ = "underwriting_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=False, comment="关联会话")
    quotation_id = Column(String(64), default="", comment="关联报价单")
    status = Column(String(30), default="pending", comment="pending/approved/rejected/needs_verification")
    message = Column(String(500), default="", comment="核保结果消息")
    requires_photo = Column(SmallInteger, default=0, comment="是否需要检车照片")
    requires_video = Column(SmallInteger, default=0, comment="是否需要验车视频")
    photo_url = Column(String(500), default="", comment="检车照片URL")
    video_url = Column(String(500), default="", comment="验车视频URL")
    checked_at = Column(DateTime, nullable=True, comment="核保时间")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_underwriting_session", "session_id"),
        Index("idx_underwriting_status", "status"),
    )


class PaymentRecordModel(Base):
    __tablename__ = "payment_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=False, comment="关联会话")
    quotation_id = Column(String(64), default="", comment="关联报价单")
    transaction_id = Column(String(64), default="", comment="第三方交易ID")
    payment_url = Column(String(500), default="", comment="支付链接")
    amount = Column(DECIMAL(10, 2), default=0, comment="支付金额")
    status = Column(String(20), default="pending", comment="pending/processing/success/failed")
    paid_at = Column(DateTime, nullable=True, comment="支付时间")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_payment_session", "session_id"),
        Index("idx_payment_txn", "transaction_id"),
        Index("idx_payment_status", "status"),
    )


class PolicyModel(Base):
    __tablename__ = "policies"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    policy_id = Column(String(64), unique=True, nullable=False, comment="保单唯一标识")
    policy_number = Column(String(50), unique=True, nullable=False, comment="保单号")
    session_id = Column(String(64), nullable=False, comment="关联会话")
    quotation_id = Column(String(64), default="", comment="关联报价单")
    applicant_id = Column(BigInteger, nullable=True, comment="投保人身份证ID")
    insured_id = Column(BigInteger, nullable=True, comment="被保人身份证ID")
    vehicle_id = Column(BigInteger, nullable=True, comment="关联车辆信息")
    company_code = Column(String(20), default="", comment="保险公司代码")
    total_premium = Column(DECIMAL(10, 2), default=0, comment="总保费")
    effective_date = Column(Date, nullable=True, comment="生效日期")
    expire_date = Column(Date, nullable=True, comment="截止日期")
    status = Column(String(20), default="active", comment="active/expired/cancelled")
    issued_at = Column(DateTime, nullable=True, comment="出单时间")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    items = relationship("PolicyItemModel", back_populates="policy", cascade="all, delete-orphan")
    terms = relationship("PolicyTermModel", back_populates="policy", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_policy_session", "session_id"),
        Index("idx_policy_number", "policy_number"),
        Index("idx_policy_status", "status"),
    )


class PolicyItemModel(Base):
    __tablename__ = "policy_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    policy_id = Column(BigInteger, ForeignKey("policies.id", ondelete="CASCADE"), nullable=False)
    item_code = Column(String(30), nullable=False, comment="险种代码")
    item_name = Column(String(50), nullable=False, comment="险种名称")
    coverage_amount = Column(DECIMAL(12, 2), nullable=False, comment="保额")
    premium = Column(DECIMAL(10, 2), nullable=False, comment="保费")
    deductible = Column(DECIMAL(10, 2), default=0, comment="免赔额")
    created_at = Column(DateTime, default=datetime.now)

    policy = relationship("PolicyModel", back_populates="items")

    __table_args__ = (
        Index("idx_policy_item_policy", "policy_id"),
    )


class PolicyTermModel(Base):
    __tablename__ = "policy_terms"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    policy_id = Column(BigInteger, ForeignKey("policies.id", ondelete="CASCADE"), nullable=False)
    term_order = Column(Integer, default=0, comment="条款序号")
    content = Column(String(1000), nullable=False, comment="条款内容")
    created_at = Column(DateTime, default=datetime.now)

    policy = relationship("PolicyModel", back_populates="terms")

    __table_args__ = (
        Index("idx_policy_term_policy", "policy_id"),
    )


class HumanTakeoverModel(Base):
    __tablename__ = "human_takeover_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    transfer_id = Column(String(64), unique=True, nullable=False, comment="转接ID")
    session_id = Column(String(64), nullable=False, comment="关联会话")
    reason = Column(String(500), default="", comment="转人工原因")
    source_phase = Column(String(40), default="", comment="触发阶段")
    handler_id = Column(String(64), default="", comment="处理人ID")
    handle_status = Column(String(20), default="pending", comment="pending/processing/resolved")
    handle_remark = Column(String(500), default="", comment="处理备注")
    requested_at = Column(DateTime, default=datetime.now, comment="请求时间")
    resolved_at = Column(DateTime, nullable=True, comment="解决时间")

    __table_args__ = (
        Index("idx_takeover_session", "session_id"),
        Index("idx_takeover_status", "handle_status"),
    )


class KnowledgeTermModel(Base):
    __tablename__ = "knowledge_terms"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    standard_term = Column(String(100), nullable=False, comment="标准行业术语")
    alias = Column(String(100), nullable=False, comment="口语化别名")
    category = Column(String(30), default="", comment="分类")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("standard_term", "alias", name="idx_term_alias"),
        Index("idx_term_standard", "standard_term"),
    )


class OcrRecordModel(Base):
    __tablename__ = "ocr_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=False, comment="关联会话")
    image_type = Column(String(20), nullable=False, comment="图片类型")
    image_url = Column(String(500), default="", comment="图片URL")
    ocr_raw_text = Column(Text, comment="OCR原始识别文本")
    ocr_result_json = Column(JSON, comment="OCR结构化结果")
    is_valid = Column(SmallInteger, default=1, comment="识别是否完整")
    error_message = Column(String(500), default="", comment="错误信息")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_ocr_session", "session_id"),
    )


class OperationLogModel(Base):
    __tablename__ = "operation_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(64), default="", comment="关联会话")
    operator_id = Column(String(64), default="", comment="操作人ID")
    operator_role = Column(String(20), default="", comment="操作人角色")
    action = Column(String(50), nullable=False, comment="操作动作")
    phase_from = Column(String(40), default="", comment="操作前阶段")
    phase_to = Column(String(40), default="", comment="操作后阶段")
    detail = Column(JSON, comment="操作详情")
    ip_address = Column(String(50), default="", comment="IP地址")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_log_session", "session_id"),
        Index("idx_log_operator", "operator_id"),
        Index("idx_log_action", "action"),
        Index("idx_log_created", "created_at"),
    )


class KnowledgeCompanyMappingModel(Base):
    __tablename__ = "knowledge_company_mappings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slang = Column(String(100), nullable=False, comment="口语简称")
    full_name = Column(String(200), nullable=False, comment="完整公司名")
    remark = Column(String(200), default="", comment="备注")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("slang", name="idx_company_slang"),
    )


class KnowledgeScenarioModel(Base):
    __tablename__ = "knowledge_scenarios"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slang = Column(String(100), nullable=False, comment="场景口语")
    description = Column(String(500), nullable=False, comment="完整描述")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("slang", name="idx_scenario_slang"),
    )


class KnowledgeRequiredFieldModel(Base):
    __tablename__ = "knowledge_required_fields"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    group_name = Column(String(50), default="", comment="分组")
    sort_order = Column(Integer, default=0, comment="序号")
    field_name = Column(String(100), nullable=False, comment="字段名")
    required = Column(String(10), default="", comment="是否必填")
    description = Column(String(500), default="", comment="说明")
    common_say = Column(String(200), default="", comment="业务员常见说法")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_field_group", "group_name"),
    )


class KnowledgeFollowupModel(Base):
    __tablename__ = "knowledge_followups"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    agent_say = Column(String(500), nullable=False, comment="业务员原话")
    obtained = Column(String(500), default="", comment="已获得信息")
    missing = Column(String(500), default="", comment="缺失项")
    followup = Column(String(500), default="", comment="追问话术")
    created_at = Column(DateTime, default=datetime.now)


class KnowledgeRuleModel(Base):
    __tablename__ = "knowledge_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sort_order = Column(Integer, default=0, comment="序号")
    rule_text = Column(Text, nullable=False, comment="规则内容")
    created_at = Column(DateTime, default=datetime.now)


class KnowledgeScriptTemplateModel(Base):
    __tablename__ = "knowledge_script_templates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(50), nullable=False, comment="编号")
    intent = Column(String(200), default="", comment="意图")
    category = Column(String(50), default="", comment="分类")
    security = Column(String(20), default="", comment="密级(内部/对外)")
    insurance_type = Column(String(200), default="", comment="适用险种")
    agent_questions = Column(Text, comment="业务员问法(分号分隔)")
    info_needed = Column(String(500), default="", comment="所需信息")
    followup = Column(Text, comment="追问话术")
    standard_answer = Column(Text, comment="标准回答")
    customer_version = Column(Text, comment="可转客户口径")
    data_deps = Column(String(200), default="", comment="数据依赖")
    prohibited = Column(String(500), default="", comment="禁止回答")
    escalate_condition = Column(String(500), default="", comment="转人工条件")
    adversarial = Column(Text, comment="对抗样例")
    basis = Column(String(500), default="", comment="依据文件与生效日期")
    source = Column(String(200), default="", comment="来源")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("code", name="idx_template_code"),
        Index("idx_template_intent", "intent"),
        Index("idx_template_category", "category"),
    )


class KnowledgeImportStateModel(Base):
    __tablename__ = "knowledge_import_state"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint = Column(String(64), nullable=False, comment="xlsx 内容指纹(sha256)")
    file_count = Column(Integer, default=0, comment="已导入的 xlsx 文件数")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
