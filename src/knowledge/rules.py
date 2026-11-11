"""
车险出单智能体 - 推荐规则引擎
基于车辆信息生成智能推荐投保方案
"""
import copy
import logging
import re
from datetime import date
from decimal import Decimal
from typing import Optional, Tuple

from src.api.schemas import (
    InsurancePlan, InsuranceItem, InsuranceCompany,
    VehicleInfo, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS,
)
logger = logging.getLogger(__name__)


class RecommendationEngine:
    """智能推荐规则引擎"""

    ITEM_ALIASES = {
        "COMPULSORY": (
            "交强险",
            "交强",
            "强制险",
            "强制性的",
            "强制性",
            "法定强制险",
            "国家强制险",
        ),
        "THIRD_PARTY": ("第三者责任险", "三者险", "三者", "三责险", "三责", "第三者"),
        "VEHICLE_LOSS": ("车辆损失险", "车损险", "车损"),
        "DRIVER_PASSENGER": ("车上人员责任险", "座位险", "乘客险", "座位"),
        "SCRATCH": ("车身划痕险", "划痕险", "划痕"),
        "THEFT": ("盗抢险", "全车盗抢", "盗抢"),
        "GLASS": ("玻璃单独破碎险", "玻璃险", "玻璃"),
        "SPONTANEOUS": ("自燃损失险", "自燃险", "自燃"),
        "WADING": ("发动机涉水险", "涉水险", "涉水"),
    }

    PREFERRED_PRESET_BY_CODE = {
        "COMPULSORY": "compulsory",
        "THIRD_PARTY": "third_party_2m",
        "VEHICLE_LOSS": "vehicle_loss",
        "DRIVER_PASSENGER": "driver_passenger",
        "SCRATCH": "scratch",
        "THEFT": "theft",
        "GLASS": "glass",
        "SPONTANEOUS": "spontaneous",
        "WADING": "wading",
    }

    OPTIONAL_ADDON_CODES = {
        "DRIVER_PASSENGER",
        "SCRATCH",
        "THEFT",
        "GLASS",
        "SPONTANEOUS",
        "WADING",
    }

    REMOVE_KEYWORDS = ("去掉", "删除", "不要", "取消")
    ADD_KEYWORDS = ("加上", "添加", "再加", "增加", "补充")
    CHEAPER_KEYWORDS = (
        "更优惠",
        "优惠点",
        "优惠一些",
        "优惠",
        "更便宜",
        "便宜点",
        "便宜一些",
        "便宜",
        "太贵",
        "保费高",
        "经济",
        "省钱",
        "降低保费",
        "保费低",
        "低预算",
    )

    def generate_recommendation(self, vehicle: VehicleInfo) -> InsurancePlan:
        """
        根据车辆信息，生成推荐的投保方案。

        Args:
            vehicle: 车辆信息

        Returns:
            InsurancePlan 推荐方案
        """
        logger.info("为车辆 %s 生成推荐方案", vehicle.license_plate)

        company = PRESET_COMPANIES["pingan"]
        items = self._select_items(vehicle)

        plan = InsurancePlan(
            company=company,
            items=items,
            discount_rate=company.discount_rate,
        )
        plan.calculate_total()

        logger.info("推荐方案生成完成: total=%s", plan.total_premium)
        return plan

    def _select_items(self, vehicle: VehicleInfo) -> list:
        """根据车辆信息选择险种组合"""
        items = []

        items.append(copy.deepcopy(PRESET_INSURANCE_ITEMS["compulsory"]))

        car_age_years = self._calculate_car_age(vehicle.registration_date)

        if car_age_years <= 6:
            items.append(copy.deepcopy(PRESET_INSURANCE_ITEMS["vehicle_loss"]))

        items.append(copy.deepcopy(PRESET_INSURANCE_ITEMS["third_party_2m"]))

        items.append(copy.deepcopy(PRESET_INSURANCE_ITEMS["driver_passenger"]))

        if car_age_years <= 3:
            items.append(copy.deepcopy(PRESET_INSURANCE_ITEMS["scratch"]))

        return items

    def _calculate_car_age(self, registration_date) -> int:
        """计算车龄（年）"""
        if not registration_date:
            return 3
        today = date.today()
        age = today.year - registration_date.year
        if today.month < registration_date.month or (
            today.month == registration_date.month
            and today.day < registration_date.day
        ):
            age -= 1
        return age

    @classmethod
    def _codes_in_text(cls, text: str) -> set[str]:
        """按最长别名优先识别险种，避免短别名造成误匹配。"""
        matches: list[tuple[int, str]] = []
        for code, aliases in cls.ITEM_ALIASES.items():
            for alias in aliases:
                index = text.find(alias)
                if index >= 0:
                    matches.append((index, code))
        return {code for _, code in matches}

    @staticmethod
    def _split_clauses(text: str) -> list[str]:
        """按标点、项目符号和换行拆分自然语言指令。"""
        return [
            clause.strip()
            for clause in re.split(r"[，,。；;\n•·]+", text)
            if clause.strip()
        ]

    @classmethod
    def _preset_for_code(cls, code: str):
        key = cls.PREFERRED_PRESET_BY_CODE.get(code)
        return PRESET_INSURANCE_ITEMS.get(key) if key else None

    @staticmethod
    def _supported_codes(company: InsuranceCompany) -> set[str]:
        return {
            PRESET_INSURANCE_ITEMS[key].code
            for key in company.available_items
            if key in PRESET_INSURANCE_ITEMS
        }

    @staticmethod
    def _apply_coverage(items: list[InsuranceItem], code: str, amount: Decimal) -> None:
        """修改保额，并在存在对应预设时同步正确保费。"""
        preset = next(
            (
                item
                for item in PRESET_INSURANCE_ITEMS.values()
                if item.code == code and item.coverage_amount == amount
            ),
            None,
        )
        for index, item in enumerate(items):
            if item.code != code:
                continue
            if preset:
                items[index] = copy.deepcopy(preset)
            else:
                item.coverage_amount = amount
            return

    def _apply_cheaper_option(self, plan: InsurancePlan) -> None:
        """生成更经济的组合：移除附加险，并降低一档三者险保额。"""
        optional_codes = {
            item.code for item in plan.items if item.code in self.OPTIONAL_ADDON_CODES
        }
        if optional_codes:
            plan.items = [item for item in plan.items if item.code not in optional_codes]

        third_party = next(
            (item for item in plan.items if item.code == "THIRD_PARTY"),
            None,
        )
        if third_party:
            options = sorted(
                {
                    item.coverage_amount
                    for item in PRESET_INSURANCE_ITEMS.values()
                    if item.code == "THIRD_PARTY"
                    and item.coverage_amount < third_party.coverage_amount
                },
                reverse=True,
            )
            if options:
                self._apply_coverage(plan.items, "THIRD_PARTY", options[0])

    @classmethod
    def is_removing_compulsory(cls, user_input: str) -> bool:
        """识别用户是否在尝试删除法定强制险。"""
        for clause in cls._split_clauses(user_input):
            remove_match = re.search(
                "|".join(re.escape(keyword) for keyword in cls.REMOVE_KEYWORDS),
                clause,
            )
            if not remove_match:
                continue
            prefix = clause[: remove_match.start()]
            target_text = clause[remove_match.end() :]
            target_text = re.sub(r"[（(][^）)]*[）)]", "", target_text)
            target_text = re.split(r"仅保留|只保留|保留|只要|仅要", target_text)[0]
            if "COMPULSORY" in cls._codes_in_text(prefix + target_text):
                return True
        return False

    @classmethod
    def requests_compulsory_only(cls, user_input: str) -> bool:
        """识别“删除全部非强制商业险、只留交强险”的指令。"""
        normalized = re.sub(r"\s+", "", user_input)
        keep_match = re.search(
            r"(?:只|仅)保留([^。；;，,\n）)]+)",
            normalized,
        )
        if keep_match:
            keep_codes = cls._codes_in_text(keep_match.group(1))
            if keep_codes == {"COMPULSORY"}:
                return True

        has_remove_intent = any(
            keyword in normalized for keyword in cls.REMOVE_KEYWORDS
        )
        optional_markers = (
            "法律不强制",
            "非强制",
            "不强制要求",
            "不是强制",
            "不是必须",
            "商业险都不要",
            "不要商业险",
            "全部商业险",
            "所有商业险",
        )
        return has_remove_intent and any(
            marker in normalized for marker in optional_markers
        )

    def parse_modification_intent(self, user_input: str, plan: InsurancePlan) -> InsurancePlan:
        """
        解析用户自然语言中的方案修改意图，并直接修改方案。

        支持的模式示例：
        - "三者改到200万"
        - "把三者换成100万"
        - "三者 200万"
        - "去掉车损险"
        - "加上划痕险"
        - "换成太平洋保险"

        Args:
            user_input: 用户自然语言输入
            plan: 当前方案

        Returns:
            修改后的方案
        """
        logger.info("解析修改意图: %s", user_input)

        modified_plan = copy.deepcopy(plan)
        clauses = self._split_clauses(user_input)

        company_pattern = re.compile(r"(平安|人保|太平洋)")
        company_match = company_pattern.search(user_input)
        if company_match:
            company_name = company_match.group(1)
            company_map = {"平安": "pingan", "人保": "picc", "太平洋": "cpic"}
            if company_name in company_map:
                new_company = PRESET_COMPANIES[company_map[company_name]]
                modified_plan.company = new_company
                modified_plan.discount_rate = new_company.discount_rate
                supported_codes = self._supported_codes(new_company)
                modified_plan.items = [
                    item
                    for item in modified_plan.items
                    if item.code in supported_codes
                ]
                logger.info("切换保险公司: %s", new_company.name)

        if self.requests_compulsory_only(user_input):
            compulsory = self._preset_for_code("COMPULSORY")
            modified_plan.items = (
                [copy.deepcopy(compulsory)] if compulsory else []
            )
            logger.info("仅保留交强险")

        # “仅保留/只保留”是保留清单，其中的险种不能因为同一句包含“去掉”而被删除。
        keep_match = re.search(
            r"(?:仅保留|只保留|只要|仅要)([^。；;，,\n）)]+)",
            user_input,
        )
        keep_codes = (
            {"COMPULSORY"} | self._codes_in_text(keep_match.group(1))
            if keep_match
            else None
        )

        remove_codes = {
            code
            for clause in clauses
            if any(keyword in clause for keyword in self.REMOVE_KEYWORDS)
            for code in self._codes_in_text(clause)
        }
        if keep_codes is not None:
            remove_codes -= keep_codes
            remove_codes |= {
                item.code for item in modified_plan.items if item.code not in keep_codes
            }
        remove_codes.discard("COMPULSORY")
        if remove_codes:
            modified_plan.items = [
                item for item in modified_plan.items if item.code not in remove_codes
            ]
            logger.info("移除险种: %s", ", ".join(sorted(remove_codes)))

        for clause in clauses:
            if not any(keyword in clause for keyword in self.ADD_KEYWORDS):
                continue
            existing_codes = {item.code for item in modified_plan.items}
            for code in self._codes_in_text(clause):
                preset = self._preset_for_code(code)
                if (
                    preset
                    and code not in existing_codes
                    and code in self._supported_codes(modified_plan.company)
                ):
                    modified_plan.items.append(copy.deepcopy(preset))
                    existing_codes.add(code)
                    logger.info("添加险种: %s", preset.name)

        for clause in clauses:
            amount_matches = list(re.finditer(r"(\d+(?:\.\d+)?)\s*万", clause))
            if not amount_matches:
                continue
            codes = self._codes_in_text(clause)
            if len(codes) != 1:
                continue
            # “从200万降到100万”应使用最后一个目标金额。
            amount = Decimal(amount_matches[-1].group(1)) * Decimal("10000")
            code = next(iter(codes))
            self._apply_coverage(modified_plan.items, code, amount)
            logger.info("修改保额: %s -> %s", code, amount)

        if any(keyword in user_input for keyword in self.CHEAPER_KEYWORDS):
            self._apply_cheaper_option(modified_plan)
            logger.info("应用经济型方案")

        # 交强险是必保险种，任何自然语言操作都不能生成空方案或缺少交强险的方案。
        compulsory = self._preset_for_code("COMPULSORY")
        if compulsory and not any(
            item.code == "COMPULSORY" for item in modified_plan.items
        ):
            modified_plan.items.insert(0, copy.deepcopy(compulsory))

        modified_plan.calculate_total()
        return modified_plan

    def get_last_year_plan(self, vehicle: VehicleInfo) -> Optional[InsurancePlan]:
        """
        根据车辆信息获取去年的投保方案。
        当前从内存/数据库中查询，如无记录则返回 None。
        """
        # 生产环境应从 MySQL Session 表查询该车牌号最新的 completed 保单对应的 plan
        # 现阶段无历史数据时返回 None，由调用方 fallback 到智能推荐
        return None


recommendation_engine = RecommendationEngine()
