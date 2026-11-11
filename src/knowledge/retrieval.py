"""基于结构化知识库的场景检索服务。

使用 MySQL 加载到内存中的知识数据做轻量混合检索：术语命中、场景别名和
中文二元词重合度。结果同时用于提示词增强和前端来源展示。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from src.knowledge.parser import knowledge_parser


@dataclass
class KnowledgeSource:
    type: str
    sheet: str
    record_id: int | str | None
    title: str
    excerpt: str
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "sheet": self.sheet,
            "record_id": self.record_id,
            "title": self.title,
            "excerpt": self.excerpt,
            "score": round(self.score, 3),
        }


@dataclass
class RetrievedKnowledge:
    query: str
    normalized_query: str = ""
    matched_terms: list[str] = field(default_factory=list)
    scenarios: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    fields: list[dict] = field(default_factory=list)
    followups: list[dict] = field(default_factory=list)
    templates: list[dict] = field(default_factory=list)
    sources: list[KnowledgeSource] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.scenarios
            or self.rules
            or self.fields
            or self.followups
            or self.templates
        )

    def prompt_context(self, max_chars: int = 7000) -> str:
        lines: list[str] = []
        if self.scenarios:
            lines.append("【匹配场景】")
            lines.extend(
                f"- {item['description']}（来源：场景口语 #{item.get('source_id', '')}）"
                for item in self.scenarios[:4]
            )
        if self.rules:
            lines.append("【必须遵循的收集规则】")
            lines.extend(
                f"- 规则 #{item.get('source_id', '')}：{item['text']}"
                for item in self.rules[:8]
            )
        if self.fields:
            lines.append("【当前场景相关必收字段】")
            lines.extend(
                f"- {item['group']} / {item['field']}：{item.get('description', '')}"
                for item in self.fields[:10]
            )
        if self.followups:
            lines.append("【推荐追问方式】")
            lines.extend(
                f"- 客户表达：{item['agent_say']}；已获得：{item['obtained']}；"
                f"追问：{item['followup']}"
                for item in self.followups[:4]
            )
        if self.templates:
            lines.append("【标准回答和禁止事项】")
            for item in self.templates[:4]:
                answer = item.get("customer_version") or item.get("standard_answer") or ""
                prohibited = item.get("prohibited") or ""
                lines.append(
                    f"- 模板 {item.get('code', '')} / #{item.get('source_id', '')}："
                    f"{answer[:500]}"
                    + (f"；禁止：{prohibited[:180]}" if prohibited else "")
                )
        context = "\n".join(lines)
        return context[:max_chars]


class KnowledgeRetrievalService:
    """结构化 RAG 检索器。"""

    def _tokens(self, text: str) -> set[str]:
        compact = re.sub(r"\s+", "", text.lower())
        tokens = set(re.findall(r"[a-z0-9]{2,}", compact))
        chinese_runs = re.findall(r"[\u4e00-\u9fff]+", compact)
        for run in chinese_runs:
            if len(run) <= 2:
                tokens.add(run)
            else:
                tokens.update(run[index : index + 2] for index in range(len(run) - 1))
        return tokens

    @staticmethod
    def _overlap(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / max(1, min(len(left), len(right)))

    def _score(self, query: str, query_tokens: set[str], text: str) -> float:
        if not text:
            return 0.0
        lowered = text.lower()
        direct = 8.0 if query in lowered else 0.0
        return direct + self._overlap(query_tokens, self._tokens(lowered)) * 10.0

    @staticmethod
    def _source(
        source_type: str,
        sheet: str,
        item: dict,
        title: str,
        excerpt: str,
        score: float,
    ) -> KnowledgeSource:
        return KnowledgeSource(
            type=source_type,
            sheet=sheet,
            record_id=item.get("source_id"),
            title=title,
            excerpt=excerpt[:260],
            score=score,
        )

    def retrieve(
        self,
        query: str,
        phase: str = "",
        limit: int = 8,
    ) -> RetrievedKnowledge:
        normalized = knowledge_parser.normalize(query)
        matched_terms = knowledge_parser.find_matches(query)
        search_text = f"{query} {normalized} {' '.join(matched_terms)}"
        query_tokens = self._tokens(search_text)
        result = RetrievedKnowledge(
            query=query,
            normalized_query=normalized,
            matched_terms=matched_terms,
        )

        scenario_candidates = []
        for item in knowledge_parser.get_scenario_entries():
            text = f"{item['slang']} {item['description']}"
            score = self._score(search_text, query_tokens, text)
            if score > 1.0:
                scenario_candidates.append((score, item))
        scenario_candidates.sort(key=lambda item: item[0], reverse=True)
        result.scenarios = [
            item for _, item in scenario_candidates[:3]
        ]
        scenario_text = " ".join(item["description"] for item in result.scenarios)
        combined_query = f"{search_text} {scenario_text}"
        combined_tokens = self._tokens(combined_query)

        rules = knowledge_parser.get_collection_rule_entries()
        phase_terms = {
            "quoting": ("保费", "报价", "起保", "折扣"),
            "underwriting": ("核保", "证件", "使用性质", "营运"),
            "collecting_info": ("行驶证", "车牌", "车架号", "使用性质"),
        }.get(phase, ())
        rule_scored = []
        for rule in rules:
            score = self._score(combined_query, combined_tokens, rule["text"])
            if any(term in rule["text"] for term in phase_terms):
                score += 2.0
            if score > 1.2:
                rule_scored.append((score, rule))
        rule_scored.sort(key=lambda item: item[0], reverse=True)
        result.rules = [item for _, item in rule_scored[:limit]]

        field_scored = []
        for item in knowledge_parser.get_required_fields():
            text = f"{item.get('group', '')} {item.get('field', '')} {item.get('common_say', '')}"
            score = self._score(combined_query, combined_tokens, text)
            if result.scenarios and str(item.get("required", "")).lower() in {
                "是",
                "true",
                "1",
            }:
                score += 0.8
            if score > 0.45:
                field_scored.append((score, item))
        field_scored.sort(key=lambda item: item[0], reverse=True)
        result.fields = [item for _, item in field_scored[:10]]

        followup_scored = []
        for item in knowledge_parser.get_followups():
            text = (
                f"{item.get('agent_say', '')} {item.get('obtained', '')} "
                f"{item.get('missing', '')} {item.get('followup', '')}"
            )
            score = self._score(search_text, query_tokens, text)
            if score > 1.2:
                followup_scored.append((score, item))
        followup_scored.sort(key=lambda item: item[0], reverse=True)
        result.followups = [item for _, item in followup_scored[:4]]

        template_scored = []
        for item in knowledge_parser.script_templates:
            text = " ".join(
                str(item.get(key) or "")
                for key in (
                    "intent",
                    "category",
                    "agent_questions",
                    "info_needed",
                    "followup",
                    "standard_answer",
                    "customer_version",
                )
            )
            score = self._score(combined_query, combined_tokens, text)
            if score > 1.2:
                template_scored.append((score, item))
        template_scored.sort(key=lambda item: item[0], reverse=True)
        result.templates = [item for _, item in template_scored[:4]]

        sources: list[KnowledgeSource] = []
        for score, item in scenario_candidates[:3]:
            sources.append(
                self._source(
                    "scenario",
                    "场景口语",
                    item,
                    item["slang"],
                    item["description"],
                    score,
                )
            )
        for score, item in rule_scored[:limit]:
            sources.append(
                self._source(
                    "rule",
                    "收集规则",
                    item,
                    f"规则 #{item.get('source_id', '')}",
                    item["text"],
                    score,
                )
            )
        for score, item in field_scored[:10]:
            sources.append(
                self._source(
                    "field",
                    "出单必收字段",
                    item,
                    f"{item.get('group', '')} · {item.get('field', '')}",
                    item.get("description") or item.get("common_say", ""),
                    score,
                )
            )
        for score, item in followup_scored[:4]:
            sources.append(
                self._source(
                    "followup",
                    "典型场景追问",
                    item,
                    item.get("agent_say", ""),
                    item.get("followup", ""),
                    score,
                )
            )
        for score, item in template_scored[:4]:
            sources.append(
                self._source(
                    "template",
                    "话术条目模板",
                    item,
                    item.get("code", ""),
                    item.get("customer_version")
                    or item.get("standard_answer", ""),
                    score,
                )
            )
        result.sources = sources
        return result

    def fallback_answer(self, knowledge: RetrievedKnowledge) -> str:
        if knowledge.templates:
            template = knowledge.templates[0]
            answer = template.get("customer_version") or template.get(
                "standard_answer"
            )
            if answer:
                return str(answer)
        if knowledge.rules:
            return (
                "根据当前知识库规则，需要先确认："
                + "；".join(item["text"] for item in knowledge.rules[:3])
            )
        return ""


knowledge_retrieval = KnowledgeRetrievalService()
