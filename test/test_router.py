"""
车险出单智能体 - 路由分发器测试
"""
import pytest

from src.api.schemas import InsuranceType, Session, UserRole
from src.core.router import router


class TestInsuranceRouter:

    def test_can_handle_car(self):
        assert router.can_handle(InsuranceType.CAR)

    def test_cannot_handle_property(self):
        assert not router.can_handle(InsuranceType.PROPERTY)

    def test_cannot_handle_life(self):
        assert not router.can_handle(InsuranceType.LIFE)

    def test_get_supported_types(self):
        types = router.get_supported_types()
        assert len(types) == 3
        assert types[0]["available"] is True
        assert types[1]["available"] is False
        assert types[2]["available"] is False

    def test_route_car_session(self):
        session = Session(
            agent_id="a1",
            insurance_type=InsuranceType.CAR,
        )
        engine_id = router.route_session(session)
        assert engine_id == "car_insurance_engine"

    def test_route_property_session(self):
        session = Session(
            agent_id="a1",
            insurance_type=InsuranceType.PROPERTY,
        )
        engine_id = router.route_session(session)
        assert engine_id == "property_insurance_engine"

    def test_route_life_session(self):
        session = Session(
            agent_id="a1",
            insurance_type=InsuranceType.LIFE,
        )
        engine_id = router.route_session(session)
        assert engine_id == "life_insurance_engine"