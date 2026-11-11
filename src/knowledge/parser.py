"""
车险出单智能体 - 知识库解析器
数据源: MySQL, 通过 load_from_db() 加载到内存缓存。
xlsx → MySQL 导入: 使用 scripts/import_knowledge.py
"""
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class KnowledgeParser:
    """知识库解析器 - 从 MySQL 加载, 内存缓存"""

    def __init__(self):
        self._terminology: Dict[str, List[str]] = {}
        self._company_map: Dict[str, str] = {}
        self._scenario_map: Dict[str, str] = {}
        self._scenarios: List[dict] = []
        self._required_fields: List[dict] = []
        self._scenario_followups: List[dict] = []
        self._collection_rules: List[dict] = []
        self._script_templates: List[dict] = []
        self._loaded = False

    # ==================== 加载入口 ====================

    async def load_from_db(self) -> Dict[str, List[str]]:
        """
        从 MySQL 加载知识库 (异步)。

        Returns:
            terminology 字典: {标准术语: [口语化别名列表]}
        """
        from src.storage.mysql import AsyncSessionLocal
        from src.storage.repository import (
            KnowledgeRepository, CompanyMappingRepository,
            ScenarioRepository, RequiredFieldRepository,
            FollowupRepository, RuleRepository, ScriptTemplateRepository,
        )

        logger.info("从 MySQL 加载知识库...")

        self._terminology.clear()
        self._company_map.clear()
        self._scenario_map.clear()
        self._scenarios.clear()
        self._required_fields.clear()
        self._scenario_followups.clear()
        self._collection_rules.clear()
        self._script_templates.clear()

        async with AsyncSessionLocal() as session:
            await self._load_terms_from_db(session, KnowledgeRepository)
            await self._load_companies_from_db(session, CompanyMappingRepository, KnowledgeRepository)
            await self._load_scenarios_from_db(session, ScenarioRepository, KnowledgeRepository)
            await self._load_fields_from_db(session, RequiredFieldRepository)
            await self._load_followups_from_db(session, FollowupRepository)
            await self._load_rules_from_db(session, RuleRepository)
            await self._load_templates_from_db(session, ScriptTemplateRepository)

        self._merge_supplementary()
        self._loaded = True
        self._log_summary()
        return self._terminology

    def _log_summary(self):
        logger.info(
            "知识库加载完成: 术语=%d, 保司=%d, 场景=%d, 字段=%d, 追问=%d, 规则=%d, 话术=%d",
            len(self._terminology), len(self._company_map), len(self._scenario_map),
            len(self._required_fields), len(self._scenario_followups),
            len(self._collection_rules), len(self._script_templates),
        )

    # ==================== SQL 加载子方法 ====================

    async def _load_terms_from_db(self, session, repo) -> None:
        rows = await repo.get_all_terms(session)
        for r in rows:
            std = r.standard_term
            alias = r.alias
            if std not in self._terminology:
                self._terminology[std] = []
            if alias and alias not in self._terminology[std]:
                self._terminology[std].append(alias)

    async def _load_companies_from_db(self, session, company_repo, term_repo) -> None:
        rows = await company_repo.get_all(session)
        for r in rows:
            self._company_map[r.slang] = r.full_name
            if r.full_name not in self._terminology:
                self._terminology[r.full_name] = []
            if r.slang not in self._terminology[r.full_name]:
                self._terminology[r.full_name].append(r.slang)

    async def _load_scenarios_from_db(self, session, scenario_repo, term_repo) -> None:
        rows = await scenario_repo.get_all(session)
        for r in rows:
            self._scenario_map[r.slang] = r.description
            self._scenarios.append(
                {
                    "source_id": r.id,
                    "slang": r.slang,
                    "description": r.description,
                }
            )
            std = r.description
            if std not in self._terminology:
                self._terminology[std] = []
            if r.slang not in self._terminology[std]:
                self._terminology[std].append(r.slang)

    async def _load_fields_from_db(self, session, repo) -> None:
        rows = await repo.get_all(session)
        for r in rows:
            self._required_fields.append({
                "source_id": r.id,
                "group": r.group_name,
                "field": r.field_name,
                "required": r.required,
                "description": r.description,
                "common_say": r.common_say,
            })

    async def _load_followups_from_db(self, session, repo) -> None:
        rows = await repo.get_all(session)
        for r in rows:
            self._scenario_followups.append({
                "source_id": r.id,
                "agent_say": r.agent_say,
                "obtained": r.obtained,
                "missing": r.missing,
                "followup": r.followup,
            })

    async def _load_rules_from_db(self, session, repo) -> None:
        rows = await repo.get_all(session)
        for r in rows:
            if r.rule_text:
                self._collection_rules.append(
                    {
                        "source_id": r.id,
                        "sort_order": r.sort_order,
                        "text": r.rule_text,
                    }
                )

    async def _load_templates_from_db(self, session, repo) -> None:
        rows = await repo.get_all(session)
        for r in rows:
            self._script_templates.append({
                "source_id": r.id,
                "code": r.code,
                "intent": r.intent,
                "category": r.category,
                "security": r.security,
                "insurance_type": r.insurance_type,
                "agent_questions": r.agent_questions or "",
                "info_needed": r.info_needed,
                "followup": r.followup or "",
                "standard_answer": r.standard_answer or "",
                "customer_version": r.customer_version or "",
                "data_deps": r.data_deps,
                "prohibited": r.prohibited,
                "escalate_condition": r.escalate_condition,
                "adversarial": r.adversarial or "",
                "basis": r.basis,
                "source": r.source,
            })

    def _merge_supplementary(self):
        """补充独立的险种别名, 与已有数据合并去重。"""
        supplementary = {
            "车辆损失险": ["车损险", "车损"],
            "车上人员责任险": ["座位险", "乘客险"],
            "车身划痕险": ["划痕险"],
            "盗抢险": ["盗抢", "全车盗抢"],
            "玻璃单独破碎险": ["玻璃险"],
            "自燃损失险": ["自燃险"],
            "发动机涉水险": ["涉水险", "发动机涉水"],
        }
        for standard, aliases in supplementary.items():
            if standard not in self._terminology:
                self._terminology[standard] = []
            for a in aliases:
                if a not in self._terminology[standard]:
                    self._terminology[standard].append(a)

    # ==================== 公共查询方法 ====================

    def normalize(self, raw_text: str) -> str:
        """将口语化表达标准化为行业术语。"""
        if not self._loaded:
            return raw_text

        for standard, aliases in self._terminology.items():
            for alias in sorted(aliases, key=len, reverse=True):
                if alias in raw_text:
                    return standard

        for slang, full_name in self._company_map.items():
            if slang in raw_text:
                return full_name

        return raw_text

    def find_matches(self, text: str) -> List[str]:
        """查找文本中匹配的所有标准术语。"""
        if not self._loaded:
            return []

        matched = []
        for standard, aliases in self._terminology.items():
            if standard in text or any(alias in text for alias in aliases):
                matched.append(standard)
        for slang, full_name in self._company_map.items():
            if slang in text and full_name not in matched:
                matched.append(full_name)
        return matched

    def get_aliases(self, standard_term: str) -> List[str]:
        """获取标准术语的所有口语化别名。"""
        if not self._loaded:
            return []
        return self._terminology.get(standard_term, [])

    def get_all_terms(self) -> List[dict]:
        """获取所有术语结构化列表, 供 Qdrant 同步使用。"""
        category_map = {}
        for t in self._script_templates:
            cat = t.get("category", "")
            intent = t.get("intent", "")
            if cat and intent:
                category_map[intent] = cat

        terms = []
        for standard, aliases in self._terminology.items():
            for alias in aliases:
                terms.append({
                    "standard_term": standard,
                    "alias": alias,
                    "category": category_map.get(standard, "terminology"),
                })
        for slang, full_name in self._company_map.items():
            terms.append({
                "standard_term": full_name,
                "alias": slang,
                "category": "company",
            })
        return terms

    def get_required_fields(self) -> List[dict]:
        """获取出单必收字段列表。"""
        return list(self._required_fields)

    def get_collection_rules(self) -> List[str]:
        """获取信息收集规则列表。"""
        return [
            item["text"] if isinstance(item, dict) else str(item)
            for item in self._collection_rules
        ]

    def get_scenario_entries(self) -> List[dict]:
        return list(self._scenarios)

    def get_followups(self) -> List[dict]:
        return list(self._scenario_followups)

    def get_collection_rule_entries(self) -> List[dict]:
        return [
            item if isinstance(item, dict) else {"text": str(item)}
            for item in self._collection_rules
        ]

    def find_script_templates(self, query: str) -> List[dict]:
        """根据用户问题匹配话术模板。"""
        results = []
        for t in self._script_templates:
            search_text = (
                t.get("intent", "") + " " +
                t.get("agent_questions", "") + " " +
                t.get("standard_answer", "")
            )
            if any(word in search_text for word in query.split()):
                results.append(t)
        return results

    def find_followup(self, agent_input: str) -> Optional[dict]:
        """根据业务员输入匹配典型场景追问。"""
        for item in self._scenario_followups:
            if item["agent_say"] in agent_input:
                return item
        return None

    def resolve_company(self, slang: str) -> Optional[str]:
        """口语保司名 → 完整公司名。"""
        return self._company_map.get(slang)

    def resolve_scenario(self, slang: str) -> Optional[str]:
        """场景口语 → 完整描述。"""
        return self._scenario_map.get(slang)

    # ==================== 属性访问 ====================

    @property
    def terminology(self) -> Dict[str, List[str]]:
        return dict(self._terminology)

    @property
    def company_map(self) -> Dict[str, str]:
        return dict(self._company_map)

    @property
    def script_templates(self) -> List[dict]:
        return list(self._script_templates)


knowledge_parser = KnowledgeParser()
