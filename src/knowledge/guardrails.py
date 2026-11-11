"""报价和核保前的确定性知识规则校验。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.api.schemas import Session
from src.knowledge.parser import knowledge_parser
from src.knowledge.retrieval import KnowledgeSource


@dataclass
class KnowledgeValidation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[KnowledgeSource] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.errors

    def add_source(self, source_type: str, item: dict, title: str, excerpt: str):
        source = KnowledgeSource(
            type=source_type,
            sheet="收集规则",
            record_id=item.get("source_id"),
            title=title,
            excerpt=excerpt,
        )
        if all(
            (existing.record_id, existing.title)
            != (source.record_id, source.title)
            for existing in self.sources
        ):
            self.sources.append(source)


class KnowledgeGuardrailService:
    """执行可由当前业务数据验证的知识规则。"""

    def _rule(self, keyword: str) -> dict | None:
        for item in knowledge_parser.get_collection_rule_entries():
            if keyword in item["text"]:
                return item
        return None

    def _add_rule(
        self,
        validation: KnowledgeValidation,
        keyword: str,
        message: str,
        error: bool = False,
    ):
        rule = self._rule(keyword)
        if error:
            validation.errors.append(message)
        else:
            validation.warnings.append(message)
        if rule:
            validation.add_source(
                "rule",
                rule,
                f"规则 #{rule.get('source_id', '')}",
                rule["text"],
            )

    def validate_quote(self, session: Session) -> KnowledgeValidation:
        validation = KnowledgeValidation()
        plan = session.insurance_plan
        vehicle = session.vehicle_info
        applicant = session.applicant_id_card

        if not plan or not plan.items:
            validation.errors.append("当前没有可报价的险种方案")
            self._add_rule(
                validation,
                "险种组合",
                "险种组合不能为空",
                error=True,
            )
            return validation
        if not vehicle or not vehicle.is_complete():
            validation.errors.append("车辆信息不完整，不能发起报价")

        codes = {item.code for item in plan.items}
        if "COMPULSORY" not in codes:
            self._add_rule(
                validation,
                "交强险",
                "方案缺少交强险，不能发起报价",
                error=True,
            )

        third_party = next(
            (item for item in plan.items if item.code == "THIRD_PARTY"),
            None,
        )
        if not third_party:
            self._add_rule(
                validation,
                "三者保额底线",
                "方案未配置第三者责任险，建议补充后再报价",
            )
        elif third_party.coverage_amount < Decimal("2000000"):
            self._add_rule(
                validation,
                "三者保额底线",
                "三者险保额低于知识库建议的 200 万元",
            )

        use_type = vehicle.use_type if vehicle else ""
        is_operating = (
            "非营运" not in use_type
            and "非营业" not in use_type
            and any(
                word in use_type
                for word in ("营运", "货运", "租赁", "网约", "滴滴")
            )
        )
        if is_operating:
            self._add_rule(
                validation,
                "营运车辆核验",
                "车辆可能属于营运性质，需人工核验营运证和道路运输证",
                error=True,
            )

        if (
            vehicle
            and applicant
            and vehicle.owner_name
            and applicant.name
            and vehicle.owner_name != applicant.name
        ):
            self._add_rule(
                validation,
                "证件信息一致性",
                "投保人与行驶证所有人姓名不一致，请确认投保关系",
            )

        return validation

    def validate_underwriting(self, session: Session) -> KnowledgeValidation:
        validation = self.validate_quote(session)
        quotation = session.quotation
        if not quotation:
            validation.errors.append("缺少保司报价单，不能提交核保")
            return validation
        if not quotation.version:
            validation.errors.append("报价单缺少版本号，不能提交核保")
        if not quotation.provider_reference:
            validation.errors.append("报价单缺少保司流水号，不能提交核保")
        if not quotation.rate_source:
            validation.errors.append("报价单缺少费率来源，不能提交核保")
        if quotation.is_estimate:
            validation.errors.append("预估方案不能提交正式核保")
        return validation


knowledge_guardrails = KnowledgeGuardrailService()
