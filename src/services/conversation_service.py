"""用户端车险对话智能体。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.api.schemas import InsurancePlan, InsuranceType, Session, WorkflowPhase
from src.core.engine import engine
from src.services.llm_service import llm_service
from src.utils.plate_validator import extract_plate_from_text
from src.knowledge.rules import recommendation_engine
from src.knowledge.retrieval import RetrievedKnowledge, knowledge_retrieval

logger = logging.getLogger(__name__)


class ConversationService:
    """理解用户问题，并区分咨询问答与流程操作。"""

    ACTIONS = {
        "answer",
        "select_car",
        "query_vehicle",
        "modify_plan",
        "submit_underwriting",
        "initiate_payment",
        "verify_payment",
    }

    def _system_prompt(
        self,
        session: Session,
        knowledge_context: str = "",
    ) -> str:
        phase = session.phase.value
        context = {
            "phase": phase,
            "customer_type": session.customer_type.value,
            "license_plate": session.license_plate,
            "has_vehicle": bool(session.vehicle_info),
            "brand_model": session.vehicle_info.brand_model if session.vehicle_info else "",
            "has_plan": bool(session.insurance_plan),
            "plan_total": str(session.insurance_plan.total_premium)
            if session.insurance_plan
            else "",
            "has_quotation": bool(session.quotation),
            "has_underwriting": bool(session.underwriting_result),
            "has_payment": bool(session.payment_result),
        }
        return f"""
你是中文车险出单智能体。你要回答用户问题，并在用户明确要求时推进业务。

当前会话：
{json.dumps(context, ensure_ascii=False)}

知识库检索结果：
{knowledge_context or "未检索到直接相关知识；不得编造具体规则、折扣或保司政策。"}

业务原则：
1. 目前只支持车险。车险通常由交强险和商业险组成，商业险可包含三者险、车损险、车上人员责任险等。
2. 保费受车辆、保额、出险记录、保司定价等影响，不能编造报价或承诺一定能承保。
3. 涉及使用性质、贷款车、过户车、营运车等问题时，应提醒按行驶证如实投保。
4. 用户只是咨询时，直接回答问题，绝对不要推进流程。
5. “车险介绍”“车险是什么”“三者险保什么”等是咨询，不得当作选择车险或确认方案。
6. 只有用户明确说“开始办理”“选择车险”“我要投保”等行动意图时，才能使用 select_car。
7. 车险选型阶段可以介绍产品，然后询问用户是否开始办理。
8. collecting_info 阶段，只有用户明确给出车牌号或车架号并要求查询时才使用 query_vehicle。
9. recommending 阶段，只有用户明确要求修改方案或确认方案时才使用 modify_plan。
10. 流程操作必须符合当前阶段，否则按普通问题回答。
11. 涉及知识库场景、必收字段或业务规则时，必须优先使用上面的检索结果。
12. 检索结果不足以回答时明确说明，不得擅自补充未提供的费率、政策或承诺。

返回严格 JSON，不要 Markdown：
{{"action":"answer|select_car|query_vehicle|modify_plan|submit_underwriting|initiate_payment|verify_payment",
  "reply":"给用户的中文回复",
  "plate":"用户提供的车牌，没有则为空",
  "vin":"用户提供的车架号，没有则为空"}}
""".strip()

    @staticmethod
    def _rule_based_decision(
        session: Session, message: str
    ) -> dict[str, str] | None:
        """对明确、低歧义的流程指令走确定性分流。"""
        if session.phase != WorkflowPhase.RECOMMENDING:
            return None

        normalized = message.strip()
        if not normalized:
            return None
        if any(word in normalized for word in ("确认", "确定", "没问题")):
            return {"action": "modify_plan", "reply": "", "plate": "", "vin": ""}

        modification_pattern = (
            r"更优惠|优惠|更便宜|便宜|太贵|保费高|"
            r"经济|省钱|降低保费|保费低|下调|降低|"
            r"去掉|删除|不要|取消|加上|添加|再加|增加|补充|"
            r"换成|更换|换一个|改到|调到|降到|调整为|"
            r"调整方案|优化方案|修改|仅保留|只保留"
        )
        if re.search(modification_pattern, normalized):
            return {"action": "modify_plan", "reply": "", "plate": "", "vin": ""}
        return None

    @staticmethod
    def _explicit_plan_codes(
        message: str, plan: InsurancePlan | None
    ) -> set[str]:
        if not plan:
            return set()
        available_codes = {item.code for item in plan.items}
        return {
            code
            for code, aliases in recommendation_engine.ITEM_ALIASES.items()
            if code in available_codes
            and any(alias in message for alias in aliases)
        }

    @classmethod
    def _last_discussed_plan_code(
        cls,
        history: list[dict[str, str]],
        plan: InsurancePlan | None,
    ) -> str:
        """从最近的对话中找出用户正在讨论的、当前方案内的险种。"""
        if not plan:
            return ""
        available_codes = {item.code for item in plan.items}
        for item in reversed(history[-10:]):
            content = str(item.get("content") or "")
            scores: list[tuple[int, int, str]] = []
            for code, aliases in recommendation_engine.ITEM_ALIASES.items():
                if code not in available_codes:
                    continue
                occurrences = [
                    (content.find(alias), alias)
                    for alias in aliases
                    if alias in content
                ]
                if occurrences:
                    scores.append(
                        (
                            max(index for index, _ in occurrences),
                            len(occurrences),
                            code,
                        )
                    )
            if scores:
                return max(scores)[2]
        return ""

    @classmethod
    def _resolve_plan_followup(
        cls,
        session: Session,
        message: str,
        history: list[dict[str, str]],
    ) -> tuple[str, str]:
        """把“去掉”“改到100万”等省略指令补全为明确险种。"""
        plan = session.insurance_plan
        if cls._explicit_plan_codes(message, plan):
            return message, ""

        is_remove_followup = bool(
            re.fullmatch(
                r"(?:把)?(?:那就|这个|它|该险种)?"
                r"(?:去掉|删除|取消|不要了?|删掉)"
                r"(?:吧|呢|啊|了|呗|[，。！!])*",
                message.strip(),
            )
        )
        has_amount_followup = bool(re.search(r"\d+(?:\.\d+)?\s*万", message))
        is_add_back_followup = bool(
            re.fullmatch(r"(?:加上|加回来|恢复|要这个|这个也要)[吧呢啊，。！!]*", message.strip())
        )
        if not (is_remove_followup or has_amount_followup or is_add_back_followup):
            return message, ""

        code = cls._last_discussed_plan_code(history, plan)
        if not code or not plan:
            return message, ""
        item = next((item for item in plan.items if item.code == code), None)
        if not item:
            return message, ""

        if is_remove_followup:
            return f"去掉{item.name}", item.name
        if has_amount_followup:
            return f"{item.name}{message}", item.name
        return f"加上{item.name}", item.name

    async def respond(
        self,
        session: Session,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        cleaned_history = self._clean_history(history or [])
        trace = ["理解用户问题"]
        knowledge: RetrievedKnowledge = knowledge_retrieval.retrieve(
            message,
            session.phase.value,
        )
        knowledge_sources = [
            source.to_dict() for source in knowledge.sources
        ]
        if not knowledge.is_empty:
            trace.append(
                f"检索知识库：{len(knowledge.scenarios)} 个场景、"
                f"{len(knowledge.rules)} 条规则、{len(knowledge.fields)} 个字段、"
                f"{len(knowledge.followups)} 个追问、{len(knowledge.templates)} 个话术"
            )
        decision = self._rule_based_decision(session, message)
        if decision:
            trace.append("命中明确流程指令，使用规则分流")
        else:
            try:
                raw = await llm_service.chat(
                    self._system_prompt(
                        session,
                        knowledge.prompt_context(),
                    ),
                    message,
                    history=cleaned_history,
                    temperature=0.2,
                    max_tokens=900,
                )
                decision = self._parse_decision(raw)
                trace.append("结合当前会话判断用户意图")
            except Exception as e:
                logger.warning("对话智能体调用失败: %s", e)
                trace.append("模型调用失败，使用安全兜底回复")
                decision = {
                    "action": "answer",
                    "reply": (
                        knowledge_retrieval.fallback_answer(knowledge)
                        or self._fallback_answer(message, session.phase)
                    ),
                }

        action = str(decision.get("action") or "answer").strip()
        reply = str(decision.get("reply") or "").strip()
        if action not in self.ACTIONS or not self._action_allowed(action, session.phase):
            if action != "answer":
                trace.append("该操作不符合当前阶段，转为普通问答")
            action = "answer"

        if action == "answer":
            trace.extend(["当前阶段不执行流程操作", "整理车险知识和会话信息"])
            return self._result(
                session,
                action,
                reply
                or knowledge_retrieval.fallback_answer(knowledge)
                or self._fallback_answer(message, session.phase),
                extra={"knowledge_sources": knowledge_sources},
                trace=trace,
            )

        trace.extend([
            f"识别为流程操作：{self._action_label(action)}",
            f"检查当前阶段：{self._phase_label(session.phase)}",
        ])
        if action == "select_car":
            result = await engine.select_insurance_type(
                session.session_id, InsuranceType.CAR
            )
        elif action == "query_vehicle":
            plate = extract_plate_from_text(message) or str(decision.get("plate") or "")
            vin = self._extract_vin(message) or str(decision.get("vin") or "")
            if not plate and not vin:
                trace.append("缺少车牌号或车架号，提示用户补充")
                return self._result(
                    session,
                    "answer",
                    "请把车牌号或 17 位车架号发给我，我再帮您查询历史车辆。",
                    trace=trace,
                )
            result = await engine.collect_info_returning_customer(
                session.session_id, plate=plate, vin=vin
            )
        elif action == "modify_plan":
            resolved_message, contextual_item = self._resolve_plan_followup(
                session,
                message,
                cleaned_history,
            )
            if contextual_item:
                trace.append(f"结合最近对话，当前指令指向{contextual_item}")
            result = await engine.modify_plan(session.session_id, resolved_message)
        elif action == "submit_underwriting":
            result = await engine.submit_underwriting(session.session_id)
        elif action == "initiate_payment":
            result = await engine.initiate_payment(session.session_id)
        else:
            result = await engine.verify_payment(session.session_id)

        updated = await self._get_session(session.session_id) or session
        trace.append("执行操作并同步会话状态")
        merged_sources = self._merge_sources(
            knowledge_sources,
            result.get("knowledge_sources") or [],
        )
        return self._result(
            updated,
            action,
            str(result.get("message") or reply or "操作已完成。"),
            extra={**result, "knowledge_sources": merged_sources},
            trace=trace,
        )

    @staticmethod
    def _merge_sources(*groups: list[dict]) -> list[dict]:
        merged = []
        seen = set()
        for group in groups:
            for source in group:
                key = (
                    source.get("type"),
                    source.get("sheet"),
                    source.get("record_id"),
                    source.get("title"),
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(source)
        return merged

    async def _get_session(self, session_id: str) -> Session | None:
        from src.core.state import session_store

        return await session_store.get_session(session_id)

    @staticmethod
    def _clean_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
        cleaned = []
        for item in history[-12:]:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                cleaned.append({"role": role, "content": content[:2000]})
        return cleaned

    @staticmethod
    def _parse_decision(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```")
            text = text.removesuffix("```").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return {"action": "answer", "reply": text}
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else {"action": "answer", "reply": text}
        except json.JSONDecodeError:
            return {"action": "answer", "reply": text}

    @staticmethod
    def _extract_vin(text: str) -> str:
        import re

        match = re.search(r"[A-HJ-NPR-Z0-9]{17}", text.upper())
        return match.group(0) if match else ""

    @staticmethod
    def _action_allowed(action: str, phase: WorkflowPhase) -> bool:
        allowed = {
            "select_car": {WorkflowPhase.INSURANCE_TYPE_SELECTION},
            "query_vehicle": {WorkflowPhase.COLLECTING_INFO},
            "modify_plan": {WorkflowPhase.RECOMMENDING},
            "submit_underwriting": {
                WorkflowPhase.QUOTING,
                WorkflowPhase.UNDERWRITING,
            },
            "initiate_payment": {WorkflowPhase.PAYMENT},
            "verify_payment": {WorkflowPhase.PAYMENT},
        }
        return phase in allowed.get(action, set())

    @staticmethod
    def _fallback_answer(message: str, phase: WorkflowPhase) -> str:
        if phase == WorkflowPhase.INSURANCE_TYPE_SELECTION:
            if "介绍" in message or "什么" in message:
                return (
                    "车险主要由交强险和商业险组成。交强险必须投保，"
                    "主要保障事故中第三方的人身和财产损失；商业险可按需选择三者险、"
                    "车损险、车上人员责任险等。如果准备办理，请告诉我“开始办理车险”。"
                )
            return "目前可以先办理车险。您可以直接提问，或告诉我“开始办理车险”。"
        if phase == WorkflowPhase.COLLECTING_INFO:
            return (
                "可以继续咨询车险问题。准备出单时，请直接发送车牌号，"
                "或点击输入框左侧的附件按钮上传行驶证正副页。"
            )
        return "智能体暂时没有理解这个问题，请换一种说法。"

    @staticmethod
    def _result(
        session: Session,
        action: str,
        message: str,
        extra: dict[str, Any] | None = None,
        trace: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "success": True,
            "action": action,
            "message": message,
            "trace": trace or [],
            "phase": session.phase.value,
            "vehicle": session.vehicle_info.to_dict() if session.vehicle_info else None,
            "plan": session.insurance_plan.to_dict() if session.insurance_plan else None,
            "quotation": session.quotation.to_dict() if session.quotation else None,
            "underwriting": session.underwriting_result.to_dict()
            if session.underwriting_result
            else None,
            "payment": session.payment_result.to_dict() if session.payment_result else None,
            "policy": session.policy.to_dict() if session.policy else None,
            "customer_type": session.customer_type.value,
        }
        if extra:
            for key in (
                "last_year_plan",
                "needs_verification",
                "suggest_next",
                "skip_recommendation",
                "knowledge_sources",
            ):
                if key in extra:
                    payload[key] = extra[key]
        return payload

    @staticmethod
    def _action_label(action: str) -> str:
        return {
            "select_car": "开始办理车险",
            "query_vehicle": "查询车辆",
            "modify_plan": "修改或确认方案",
            "submit_underwriting": "提交核保",
            "initiate_payment": "发起支付",
            "verify_payment": "核验支付",
        }.get(action, action)

    @staticmethod
    def _phase_label(phase: WorkflowPhase) -> str:
        return {
            WorkflowPhase.INSURANCE_TYPE_SELECTION: "选择保险类型",
            WorkflowPhase.COLLECTING_INFO: "收集车辆信息",
            WorkflowPhase.RECOMMENDING: "智能推荐方案",
            WorkflowPhase.IDENTITY_VERIFICATION: "身份验证",
            WorkflowPhase.QUOTING: "生成报价",
            WorkflowPhase.UNDERWRITING: "核保",
            WorkflowPhase.PAYMENT: "支付",
            WorkflowPhase.COMPLETED: "投保完成",
            WorkflowPhase.TRANSFERRED_TO_HUMAN: "人工接管",
        }.get(phase, phase.value)


conversation_service = ConversationService()
