"""
车险出单智能体 - 知识库测试
"""
import asyncio

import pytest

from src.knowledge.parser import knowledge_parser
from src.knowledge.rules import recommendation_engine


class TestKnowledgeParser:

    @pytest.fixture(autouse=True)
    def setup(self):
        try:
            asyncio.run(knowledge_parser.load_from_db())
        except Exception:
            pytest.skip("MySQL 不可用, 跳过知识库测试")

    def test_load_terminology(self):
        terms = knowledge_parser._terminology
        assert len(terms) > 0
        assert "交强险" in terms
        assert "交强" in terms["交强险"]

    def test_normalize_slang(self):
        assert knowledge_parser.normalize("交强") == "交强险"
        assert knowledge_parser.normalize("三者") == "第三者责任险"

    def test_normalize_unknown(self):
        result = knowledge_parser.normalize("未知术语")
        assert result == "未知术语"

    def test_find_matches(self):
        matches = knowledge_parser.find_matches("我要买交强和三者")
        assert "交强险" in matches
        assert "第三者责任险" in matches

    def test_get_aliases(self):
        aliases = knowledge_parser.get_aliases("交强险")
        assert "交强" in aliases


class TestRecommendationEngine:

    def test_generate_recommendation(self):
        from src.api.schemas import VehicleInfo
        from datetime import date

        vehicle = VehicleInfo(
            license_plate="京A12345",
            brand_model="大众速腾",
            registration_date=date(2022, 6, 15),
        )

        plan = recommendation_engine.generate_recommendation(vehicle)
        assert plan.company.name == "平安保险"
        assert len(plan.items) >= 2
        assert plan.total_premium > 0

        compulsory_found = any(i.code == "COMPULSORY" for i in plan.items)
        third_party_found = any(i.code == "THIRD_PARTY" for i in plan.items)
        assert compulsory_found
        assert third_party_found

    def test_parse_modification_coverage(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS
        from decimal import Decimal

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["third_party_1m"],
                PRESET_INSURANCE_ITEMS["vehicle_loss"],
            ],
        )
        plan.calculate_total()

        modified = recommendation_engine.parse_modification_intent("三者改到200万", plan)
        for item in modified.items:
            if item.code == "THIRD_PARTY":
                assert item.coverage_amount == Decimal("2000000")
                assert item.premium == Decimal("2500")

    def test_parse_modification_uses_last_target_amount(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS
        from decimal import Decimal

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["third_party_2m"],
            ],
        )
        plan.calculate_total()

        modified = recommendation_engine.parse_modification_intent(
            "三者险保额从200万降到100万",
            plan,
        )
        third_party = next(
            item for item in modified.items if item.code == "THIRD_PARTY"
        )
        assert third_party.coverage_amount == Decimal("1000000")
        assert third_party.premium == Decimal("1800")

    def test_cheaper_plan_actually_lowers_premium(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS
        from decimal import Decimal

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["vehicle_loss"],
                PRESET_INSURANCE_ITEMS["third_party_2m"],
                PRESET_INSURANCE_ITEMS["driver_passenger"],
            ],
            discount_rate=PRESET_COMPANIES["pingan"].discount_rate,
        )
        plan.calculate_total()
        original_total = plan.total_premium

        modified = recommendation_engine.parse_modification_intent(
            "更优惠的方案",
            plan,
        )

        assert modified.total_premium < original_total
        assert {item.code for item in modified.items} == {
            "COMPULSORY",
            "VEHICLE_LOSS",
            "THIRD_PARTY",
        }
        third_party = next(
            item for item in modified.items if item.code == "THIRD_PARTY"
        )
        assert third_party.coverage_amount == Decimal("1000000")

    def test_compound_removal_preserves_only_requested_items(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["vehicle_loss"],
                PRESET_INSURANCE_ITEMS["third_party_2m"],
                PRESET_INSURANCE_ITEMS["driver_passenger"],
            ],
        )
        plan.calculate_total()

        modified = recommendation_engine.parse_modification_intent(
            "降低三者险保额（如从200万降到100万） • "
            "去掉车上人员责任险 • "
            "去掉车损险（仅保留交强+三者）",
            plan,
        )

        assert {item.code for item in modified.items} == {
            "COMPULSORY",
            "THIRD_PARTY",
        }
        assert modified.total_premium > 0

    def test_mandatory_insurance_cannot_be_removed(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["third_party_500k"],
            ],
        )
        plan.calculate_total()

        assert recommendation_engine.is_removing_compulsory("去掉强制性的")
        modified = recommendation_engine.parse_modification_intent(
            "去掉强制性的",
            plan,
        )
        assert any(item.code == "COMPULSORY" for item in modified.items)

    def test_remove_all_non_mandatory_coverage(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS
        from decimal import Decimal

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["vehicle_loss"],
                PRESET_INSURANCE_ITEMS["third_party_500k"],
            ],
            discount_rate=PRESET_COMPANIES["pingan"].discount_rate,
        )
        plan.calculate_total()

        modified = recommendation_engine.parse_modification_intent(
            "去掉法律不强制要求的",
            plan,
        )

        assert [item.code for item in modified.items] == ["COMPULSORY"]
        assert modified.total_premium == Decimal("807.50")

    def test_parse_modification_remove_item(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["third_party_1m"],
                PRESET_INSURANCE_ITEMS["vehicle_loss"],
            ],
        )
        plan.calculate_total()

        modified = recommendation_engine.parse_modification_intent("去掉车损险", plan)
        vehicle_loss_items = [i for i in modified.items if i.code == "VEHICLE_LOSS"]
        assert len(vehicle_loss_items) == 0

    def test_parse_modification_change_company(self):
        from src.api.schemas import InsurancePlan, PRESET_COMPANIES, PRESET_INSURANCE_ITEMS

        plan = InsurancePlan(
            company=PRESET_COMPANIES["pingan"],
            items=[
                PRESET_INSURANCE_ITEMS["compulsory"],
                PRESET_INSURANCE_ITEMS["third_party_1m"],
            ],
        )
        plan.calculate_total()

        modified = recommendation_engine.parse_modification_intent("换成太平洋保险", plan)
        assert modified.company.code == "CPIC"
