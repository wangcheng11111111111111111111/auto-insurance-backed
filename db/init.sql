-- ============================================================
-- 车险出单智能体 - 数据库初始化脚本
-- 数据库: auto_insurance
-- ============================================================

CREATE DATABASE IF NOT EXISTS auto_insurance
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE auto_insurance;


-- ============================================================
-- 1. 业务员表
-- ============================================================
CREATE TABLE IF NOT EXISTS agents (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    agent_id        VARCHAR(64)   NOT NULL UNIQUE COMMENT '业务员唯一标识',
    name            VARCHAR(100)  NOT NULL COMMENT '业务员姓名',
    phone           VARCHAR(20)   DEFAULT '' COMMENT '手机号',
    employee_no     VARCHAR(50)   DEFAULT '' COMMENT '工号',
    department      VARCHAR(100)  DEFAULT '' COMMENT '所属部门',
    password_hash   VARCHAR(255)  DEFAULT '' COMMENT '密码哈希',
    status          TINYINT       DEFAULT 1 COMMENT '状态: 1-在职 0-离职',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_agent_phone (phone)
) ENGINE=InnoDB COMMENT='业务员表';


-- ============================================================
-- 2. 用户表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         VARCHAR(64)   NOT NULL UNIQUE COMMENT '用户唯一标识',
    name            VARCHAR(100)  DEFAULT '' COMMENT '用户姓名',
    id_number       VARCHAR(18)   DEFAULT '' COMMENT '身份证号（加密存储）',
    phone           VARCHAR(20)   DEFAULT '' COMMENT '手机号',
    agent_id        VARCHAR(64)   DEFAULT '' COMMENT '关联业务员ID',
    password_hash   VARCHAR(255)  DEFAULT '' COMMENT '密码哈希',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_phone (phone),
    INDEX idx_user_agent (agent_id)
) ENGINE=InnoDB COMMENT='用户表';


-- ============================================================
-- 3. 车辆信息表
-- ============================================================
CREATE TABLE IF NOT EXISTS vehicle_info (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    license_plate       VARCHAR(10)   NOT NULL COMMENT '车牌号',
    vin                 VARCHAR(17)   DEFAULT '' COMMENT '车架号(VIN)',
    engine_number       VARCHAR(50)   DEFAULT '' COMMENT '发动机号',
    brand_model         VARCHAR(100)  DEFAULT '' COMMENT '品牌型号',
    registration_date   DATE          DEFAULT NULL COMMENT '注册日期',
    owner_name          VARCHAR(100)  DEFAULT '' COMMENT '所有人姓名',
    use_type            VARCHAR(20)   DEFAULT '非营运' COMMENT '使用性质',
    seat_count          INT           DEFAULT 5 COMMENT '核定载客数',
    vehicle_type        VARCHAR(30)   DEFAULT '小型轿车' COMMENT '车辆类型',
    created_at          DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_plate (license_plate),
    INDEX idx_vin (vin)
) ENGINE=InnoDB COMMENT='车辆信息表';


-- ============================================================
-- 4. 身份证信息表
-- ============================================================
CREATE TABLE IF NOT EXISTS id_card_info (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id             VARCHAR(64)   DEFAULT '' COMMENT '关联用户ID',
    name                VARCHAR(100)  NOT NULL COMMENT '姓名',
    id_number           VARCHAR(18)   NOT NULL COMMENT '身份证号（加密存储）',
    gender              VARCHAR(10)   DEFAULT '' COMMENT '性别',
    ethnicity           VARCHAR(20)   DEFAULT '' COMMENT '民族',
    birth_date          DATE          DEFAULT NULL COMMENT '出生日期',
    address             VARCHAR(500)  DEFAULT '' COMMENT '住址',
    issuing_authority   VARCHAR(200)  DEFAULT '' COMMENT '签发机关',
    valid_from          DATE          DEFAULT NULL COMMENT '有效期起',
    valid_to            DATE          DEFAULT NULL COMMENT '有效期止',
    front_image_url     VARCHAR(500)  DEFAULT '' COMMENT '身份证正面照URL',
    back_image_url      VARCHAR(500)  DEFAULT '' COMMENT '身份证背面照URL',
    created_at          DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_id_card_user (user_id)
) ENGINE=InnoDB COMMENT='身份证信息表';


-- ============================================================
-- 5. 保险公司表
-- ============================================================
CREATE TABLE IF NOT EXISTS insurance_companies (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    code            VARCHAR(20)   NOT NULL UNIQUE COMMENT '公司代码: PINGAN/PICC/CPIC',
    name            VARCHAR(100)  NOT NULL COMMENT '公司名称',
    discount_rate   DECIMAL(4,3)  DEFAULT 1.000 COMMENT '默认折扣率',
    contact_phone   VARCHAR(20)   DEFAULT '' COMMENT '联系电话',
    status          TINYINT       DEFAULT 1 COMMENT '状态: 1-启用 0-停用',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB COMMENT='保险公司表';


-- 预置保险公司
INSERT INTO insurance_companies (code, name, discount_rate, contact_phone) VALUES
('PINGAN', '平安保险', 0.850, '95511'),
('PICC',   '人保财险', 0.900, '95518'),
('CPIC',   '太平洋保险', 0.880, '95500');


-- ============================================================
-- 6. 险种模板表
-- ============================================================
CREATE TABLE IF NOT EXISTS insurance_item_templates (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    code            VARCHAR(30)   NOT NULL UNIQUE COMMENT '险种代码',
    name            VARCHAR(50)   NOT NULL COMMENT '险种名称',
    coverage_amount DECIMAL(12,2) DEFAULT 0 COMMENT '默认保额',
    premium         DECIMAL(10,2) DEFAULT 0 COMMENT '参考保费',
    deductible      DECIMAL(10,2) DEFAULT 0 COMMENT '免赔额',
    is_compulsory   TINYINT       DEFAULT 0 COMMENT '是否强制险',
    sort_order      INT           DEFAULT 0 COMMENT '排序',
    status          TINYINT       DEFAULT 1 COMMENT '状态: 1-启用 0-停用',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB COMMENT='险种模板表';


INSERT INTO insurance_item_templates (code, name, coverage_amount, premium, deductible, is_compulsory, sort_order) VALUES
('COMPULSORY',       '交强险',         122000,  950,    0, 1, 1),
('THIRD_PARTY',      '第三者责任险',   2000000, 2500,   0, 0, 2),
('VEHICLE_LOSS',     '车辆损失险',     150000,  2200, 500, 0, 3),
('DRIVER_PASSENGER', '车上人员责任险', 10000,    200,   0, 0, 4),
('SCRATCH',          '车身划痕险',      5000,    400, 200, 0, 5),
('THEFT',            '盗抢险',         150000,   800, 500, 0, 6),
('GLASS',            '玻璃单独破碎险',  5000,    150,   0, 0, 7),
('SPONTANEOUS',      '自燃损失险',     150000,   300, 500, 0, 8),
('WADING',           '发动机涉水险',   150000,   250, 500, 0, 9);


-- ============================================================
-- 7. 会话表（以车牌号命名）
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)   NOT NULL UNIQUE COMMENT '会话唯一标识',
    agent_id        VARCHAR(64)   NOT NULL COMMENT '业务员ID',
    user_id         VARCHAR(64)   DEFAULT '' COMMENT '用户ID',
    role            VARCHAR(20)   DEFAULT 'customer' COMMENT '角色: customer/agent',
    license_plate   VARCHAR(10)   DEFAULT '' COMMENT '关联车牌号',
    vin             VARCHAR(64)   DEFAULT '' COMMENT '关联车架号（明文索引，state_json 中为密文）',
    insurance_type  VARCHAR(20)   DEFAULT 'car' COMMENT '保险类型: car/property/life',
    phase           VARCHAR(40)   DEFAULT 'insurance_type_selection' COMMENT '当前阶段',
    customer_type   VARCHAR(20)   DEFAULT 'new' COMMENT '客户类型: new/returning',

    -- 关联数据ID
    vehicle_info_id     BIGINT DEFAULT NULL COMMENT '关联车辆信息',
    applicant_id_card_id BIGINT DEFAULT NULL COMMENT '投保人身份证ID',
    insured_id_card_id  BIGINT DEFAULT NULL COMMENT '被保人身份证ID',
    insurance_plan_id   BIGINT DEFAULT NULL COMMENT '关联投保方案',
    quotation_id        BIGINT DEFAULT NULL COMMENT '关联报价单',
    underwriting_id     BIGINT DEFAULT NULL COMMENT '关联核保记录',
    payment_id          BIGINT DEFAULT NULL COMMENT '关联支付记录',
    policy_id           BIGINT DEFAULT NULL COMMENT '关联保单',
    state_json      TEXT          DEFAULT NULL COMMENT '会话完整状态 JSON 快照（Redis/内存恢复源）',

    status          TINYINT       DEFAULT 1 COMMENT '状态: 1-活跃 0-已关闭',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_session_agent (agent_id),
    INDEX idx_session_user (user_id),
    INDEX idx_session_plate (license_plate),
    INDEX idx_session_vin (vin),
    INDEX idx_session_phase (phase)
) ENGINE=InnoDB COMMENT='会话表（以车牌号命名）';


-- ============================================================
-- 8. 投保方案表
-- ============================================================
CREATE TABLE IF NOT EXISTS insurance_plans (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)   NOT NULL COMMENT '关联会话',
    company_code    VARCHAR(20)   DEFAULT '' COMMENT '保险公司代码',
    discount_rate   DECIMAL(4,3)  DEFAULT 1.000 COMMENT '折扣率',
    total_premium   DECIMAL(10,2) DEFAULT 0 COMMENT '总保费',
    is_last_year    TINYINT       DEFAULT 0 COMMENT '是否沿用去年方案',
    status          VARCHAR(20)   DEFAULT 'draft' COMMENT '状态: draft/confirmed/modified',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_plan_session (session_id)
) ENGINE=InnoDB COMMENT='投保方案表';


-- ============================================================
-- 9. 方案险种明细表
-- ============================================================
CREATE TABLE IF NOT EXISTS plan_items (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    plan_id         BIGINT        NOT NULL COMMENT '关联方案ID',
    item_code       VARCHAR(30)   NOT NULL COMMENT '险种代码',
    item_name       VARCHAR(50)   NOT NULL COMMENT '险种名称',
    coverage_amount DECIMAL(12,2) NOT NULL COMMENT '保额',
    premium         DECIMAL(10,2) NOT NULL COMMENT '保费',
    deductible      DECIMAL(10,2) DEFAULT 0 COMMENT '免赔额',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_plan_item_plan (plan_id),
    FOREIGN KEY (plan_id) REFERENCES insurance_plans(id) ON DELETE CASCADE
) ENGINE=InnoDB COMMENT='方案险种明细表';


-- ============================================================
-- 10. 报价单表
-- ============================================================
CREATE TABLE IF NOT EXISTS quotations (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    quotation_id    VARCHAR(64)   NOT NULL UNIQUE COMMENT '报价单唯一标识',
    session_id      VARCHAR(64)   NOT NULL COMMENT '关联会话',
    plan_id         BIGINT        DEFAULT NULL COMMENT '关联方案',
    vehicle_id      BIGINT        DEFAULT NULL COMMENT '关联车辆信息',
    applicant_id    BIGINT        DEFAULT NULL COMMENT '投保人身份证ID',
    insured_id      BIGINT        DEFAULT NULL COMMENT '被保人身份证ID',
    effective_date  DATE          DEFAULT NULL COMMENT '生效日期',
    expire_date     DATE          DEFAULT NULL COMMENT '截止日期',
    total_premium   DECIMAL(10,2) DEFAULT 0 COMMENT '总保费',
    status          VARCHAR(30)   DEFAULT 'draft' COMMENT 'draft/pending_underwriting/approved/rejected/paid',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_quotation_session (session_id),
    INDEX idx_quotation_status (status)
) ENGINE=InnoDB COMMENT='报价单表';


-- ============================================================
-- 11. 核保记录表
-- ============================================================
CREATE TABLE IF NOT EXISTS underwriting_records (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)   NOT NULL COMMENT '关联会话',
    quotation_id    VARCHAR(64)   DEFAULT '' COMMENT '关联报价单',
    status          VARCHAR(30)   DEFAULT 'pending' COMMENT 'pending/approved/rejected/needs_verification',
    message         VARCHAR(500)  DEFAULT '' COMMENT '核保结果消息',
    requires_photo  TINYINT       DEFAULT 0 COMMENT '是否需要检车照片',
    requires_video  TINYINT       DEFAULT 0 COMMENT '是否需要验车视频',
    photo_url       VARCHAR(500)  DEFAULT '' COMMENT '检车照片URL',
    video_url       VARCHAR(500)  DEFAULT '' COMMENT '验车视频URL',
    checked_at      DATETIME      DEFAULT NULL COMMENT '核保时间',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_underwriting_session (session_id),
    INDEX idx_underwriting_status (status)
) ENGINE=InnoDB COMMENT='核保记录表';


-- ============================================================
-- 12. 支付记录表
-- ============================================================
CREATE TABLE IF NOT EXISTS payment_records (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)   NOT NULL COMMENT '关联会话',
    quotation_id    VARCHAR(64)   DEFAULT '' COMMENT '关联报价单',
    transaction_id  VARCHAR(64)   DEFAULT '' COMMENT '第三方交易ID',
    payment_url     VARCHAR(500)  DEFAULT '' COMMENT '支付链接',
    amount          DECIMAL(10,2) DEFAULT 0 COMMENT '支付金额',
    status          VARCHAR(20)   DEFAULT 'pending' COMMENT 'pending/processing/success/failed',
    paid_at         DATETIME      DEFAULT NULL COMMENT '支付时间',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_payment_session (session_id),
    INDEX idx_payment_txn (transaction_id),
    INDEX idx_payment_status (status)
) ENGINE=InnoDB COMMENT='支付记录表';


-- ============================================================
-- 13. 保单表
-- ============================================================
CREATE TABLE IF NOT EXISTS policies (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    policy_id       VARCHAR(64)   NOT NULL UNIQUE COMMENT '保单唯一标识',
    policy_number   VARCHAR(50)   NOT NULL UNIQUE COMMENT '保单号',
    session_id      VARCHAR(64)   NOT NULL COMMENT '关联会话',
    quotation_id    VARCHAR(64)   DEFAULT '' COMMENT '关联报价单',
    applicant_id    BIGINT        DEFAULT NULL COMMENT '投保人身份证ID',
    insured_id      BIGINT        DEFAULT NULL COMMENT '被保人身份证ID',
    vehicle_id      BIGINT        DEFAULT NULL COMMENT '关联车辆信息',
    company_code    VARCHAR(20)   DEFAULT '' COMMENT '保险公司代码',
    total_premium   DECIMAL(10,2) DEFAULT 0 COMMENT '总保费',
    effective_date  DATE          DEFAULT NULL COMMENT '生效日期',
    expire_date     DATE          DEFAULT NULL COMMENT '截止日期',
    status          VARCHAR(20)   DEFAULT 'active' COMMENT 'active/expired/cancelled',
    issued_at       DATETIME      DEFAULT NULL COMMENT '出单时间',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_policy_session (session_id),
    INDEX idx_policy_number (policy_number),
    INDEX idx_policy_plate (vehicle_id),
    INDEX idx_policy_status (status)
) ENGINE=InnoDB COMMENT='保单表';


-- ============================================================
-- 14. 保单险种明细表
-- ============================================================
CREATE TABLE IF NOT EXISTS policy_items (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    policy_id       BIGINT        NOT NULL COMMENT '关联保单ID',
    item_code       VARCHAR(30)   NOT NULL COMMENT '险种代码',
    item_name       VARCHAR(50)   NOT NULL COMMENT '险种名称',
    coverage_amount DECIMAL(12,2) NOT NULL COMMENT '保额',
    premium         DECIMAL(10,2) NOT NULL COMMENT '保费',
    deductible      DECIMAL(10,2) DEFAULT 0 COMMENT '免赔额',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_policy_item_policy (policy_id),
    FOREIGN KEY (policy_id) REFERENCES policies(id) ON DELETE CASCADE
) ENGINE=InnoDB COMMENT='保单险种明细表';


-- ============================================================
-- 15. 保单条款表
-- ============================================================
CREATE TABLE IF NOT EXISTS policy_terms (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    policy_id       BIGINT        NOT NULL COMMENT '关联保单ID',
    term_order      INT           DEFAULT 0 COMMENT '条款序号',
    content         VARCHAR(1000) NOT NULL COMMENT '条款内容',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_policy_term_policy (policy_id),
    FOREIGN KEY (policy_id) REFERENCES policies(id) ON DELETE CASCADE
) ENGINE=InnoDB COMMENT='保单条款表';


-- ============================================================
-- 16. 人工接管记录表
-- ============================================================
CREATE TABLE IF NOT EXISTS human_takeover_records (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    transfer_id     VARCHAR(64)   NOT NULL UNIQUE COMMENT '转接ID',
    session_id      VARCHAR(64)   NOT NULL COMMENT '关联会话',
    reason          VARCHAR(500)  DEFAULT '' COMMENT '转人工原因',
    source_phase    VARCHAR(40)   DEFAULT '' COMMENT '触发阶段',
    handler_id      VARCHAR(64)   DEFAULT '' COMMENT '处理人ID',
    handle_status   VARCHAR(20)   DEFAULT 'pending' COMMENT 'pending/processing/resolved',
    handle_remark   VARCHAR(500)  DEFAULT '' COMMENT '处理备注',
    requested_at    DATETIME      DEFAULT CURRENT_TIMESTAMP COMMENT '请求时间',
    resolved_at     DATETIME      DEFAULT NULL COMMENT '解决时间',
    INDEX idx_takeover_session (session_id),
    INDEX idx_takeover_status (handle_status)
) ENGINE=InnoDB COMMENT='人工接管记录表';


-- ============================================================
-- 17. 知识库术语表
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_terms (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    standard_term   VARCHAR(100)  NOT NULL COMMENT '标准行业术语',
    alias           VARCHAR(100)  NOT NULL COMMENT '口语化别名',
    category        VARCHAR(30)   DEFAULT '' COMMENT '分类: insurance_type/company/document/amount',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_term_alias (standard_term, alias),
    INDEX idx_term_standard (standard_term)
) ENGINE=InnoDB COMMENT='知识库术语表';


-- 预置术语数据
INSERT INTO knowledge_terms (standard_term, alias, category) VALUES
('交强险',          '强制险',       'insurance_type'),
('交强险',          '交强',         'insurance_type'),
('交强险',          '强险',         'insurance_type'),
('第三者责任险',    '三者',         'insurance_type'),
('第三者责任险',    '三责险',       'insurance_type'),
('第三者责任险',    '三责',         'insurance_type'),
('车辆损失险',      '车损险',       'insurance_type'),
('车辆损失险',      '车损',         'insurance_type'),
('车上人员责任险',  '座位险',       'insurance_type'),
('车上人员责任险',  '乘客险',       'insurance_type'),
('车身划痕险',      '划痕险',       'insurance_type'),
('盗抢险',          '盗抢',         'insurance_type'),
('盗抢险',          '全车盗抢',     'insurance_type'),
('玻璃单独破碎险',  '玻璃险',       'insurance_type'),
('自燃损失险',      '自燃险',       'insurance_type'),
('发动机涉水险',    '涉水险',       'insurance_type'),
('发动机涉水险',    '发动机涉水',   'insurance_type'),
('平安保险',        '平安',         'company'),
('平安保险',        '平安车险',     'company'),
('人保财险',        '人保',         'company'),
('人保财险',        'picc',         'company'),
('人保财险',        'PICC',         'company'),
('太平洋保险',      '太平洋',       'company'),
('太平洋保险',      '太保',         'company'),
('行驶证',          '行驶本',       'document'),
('行驶证',          '行车证',       'document'),
('车架号',          'vin',          'document'),
('车架号',          'VIN码',        'document'),
('车架号',          'VIN',          'document'),
('保额',            '赔付额度',     'amount'),
('保额',            '赔偿额度',     'amount');


-- ============================================================
-- 18. OCR 识别记录表
-- ============================================================
CREATE TABLE IF NOT EXISTS ocr_records (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)   NOT NULL COMMENT '关联会话',
    image_type      VARCHAR(20)   NOT NULL COMMENT '图片类型: vehicle_license/id_card_front/id_card_back',
    image_url       VARCHAR(500)  DEFAULT '' COMMENT '图片URL',
    ocr_raw_text    TEXT          COMMENT 'OCR原始识别文本',
    ocr_result_json JSON          COMMENT 'OCR结构化结果',
    is_valid        TINYINT       DEFAULT 1 COMMENT '识别是否完整',
    error_message   VARCHAR(500)  DEFAULT '' COMMENT '错误信息',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_ocr_session (session_id)
) ENGINE=InnoDB COMMENT='OCR识别记录表';


-- ============================================================
-- 19. 操作日志表
-- ============================================================
CREATE TABLE IF NOT EXISTS operation_logs (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)   DEFAULT '' COMMENT '关联会话',
    operator_id     VARCHAR(64)   DEFAULT '' COMMENT '操作人ID',
    operator_role   VARCHAR(20)   DEFAULT '' COMMENT '操作人角色',
    action          VARCHAR(50)   NOT NULL COMMENT '操作动作',
    phase_from      VARCHAR(40)   DEFAULT '' COMMENT '操作前阶段',
    phase_to        VARCHAR(40)   DEFAULT '' COMMENT '操作后阶段',
    detail          JSON          COMMENT '操作详情',
    ip_address      VARCHAR(50)   DEFAULT '' COMMENT 'IP地址',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_log_session (session_id),
    INDEX idx_log_operator (operator_id),
    INDEX idx_log_action (action),
    INDEX idx_log_created (created_at)
) ENGINE=InnoDB COMMENT='操作日志表';


-- ============================================================
-- 20. 知识库 - 保司口语映射表
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_company_mappings (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    slang           VARCHAR(100)  NOT NULL COMMENT '口语简称',
    full_name       VARCHAR(200)  NOT NULL COMMENT '完整公司名',
    remark          VARCHAR(200)  DEFAULT '' COMMENT '备注',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_company_slang (slang)
) ENGINE=InnoDB COMMENT='知识库-保司口语映射';


-- ============================================================
-- 21. 知识库 - 场景口语表
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_scenarios (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    slang           VARCHAR(100)  NOT NULL COMMENT '场景口语',
    description     VARCHAR(500)  NOT NULL COMMENT '完整描述',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_scenario_slang (slang)
) ENGINE=InnoDB COMMENT='知识库-场景口语';


-- ============================================================
-- 22. 知识库 - 出单必收字段表
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_required_fields (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    group_name      VARCHAR(50)   DEFAULT '' COMMENT '分组',
    sort_order      INT           DEFAULT 0 COMMENT '序号',
    field_name      VARCHAR(100)  NOT NULL COMMENT '字段名',
    required        VARCHAR(10)   DEFAULT '' COMMENT '是否必填',
    description     VARCHAR(500)  DEFAULT '' COMMENT '说明',
    common_say      VARCHAR(200)  DEFAULT '' COMMENT '业务员常见说法',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_field_group (group_name)
) ENGINE=InnoDB COMMENT='知识库-出单必收字段';


-- ============================================================
-- 23. 知识库 - 典型场景追问表
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_followups (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    agent_say       VARCHAR(500)  NOT NULL COMMENT '业务员原话',
    obtained        VARCHAR(500)  DEFAULT '' COMMENT '已获得信息',
    missing         VARCHAR(500)  DEFAULT '' COMMENT '缺失项',
    followup        VARCHAR(500)  DEFAULT '' COMMENT '追问话术',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB COMMENT='知识库-典型场景追问';


-- ============================================================
-- 24. 知识库 - 收集规则表
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_rules (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    sort_order      INT           DEFAULT 0 COMMENT '序号',
    rule_text       TEXT          NOT NULL COMMENT '规则内容',
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB COMMENT='知识库-收集规则';


-- ============================================================
-- 25. 知识库 - 话术条目模板表
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_script_templates (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    code                VARCHAR(50)   NOT NULL COMMENT '编号',
    intent              VARCHAR(200)  DEFAULT '' COMMENT '意图',
    category            VARCHAR(50)   DEFAULT '' COMMENT '分类',
    security            VARCHAR(20)   DEFAULT '' COMMENT '密级(内部/对外)',
    insurance_type      VARCHAR(200)  DEFAULT '' COMMENT '适用险种',
    agent_questions     TEXT          COMMENT '业务员问法(分号分隔)',
    info_needed         VARCHAR(500)  DEFAULT '' COMMENT '所需信息',
    followup            TEXT          COMMENT '追问话术',
    standard_answer     TEXT          COMMENT '标准回答',
    customer_version    TEXT          COMMENT '可转客户口径',
    data_deps           VARCHAR(200)  DEFAULT '' COMMENT '数据依赖',
    prohibited          VARCHAR(500)  DEFAULT '' COMMENT '禁止回答',
    escalate_condition  VARCHAR(500)  DEFAULT '' COMMENT '转人工条件',
    adversarial         TEXT          COMMENT '对抗样例',
    basis               VARCHAR(500)  DEFAULT '' COMMENT '依据文件与生效日期',
    source              VARCHAR(200)  DEFAULT '' COMMENT '来源',
    created_at          DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_template_code (code),
    INDEX idx_template_intent (intent),
    INDEX idx_template_category (category)
) ENGINE=InnoDB COMMENT='知识库-话术条目模板';
