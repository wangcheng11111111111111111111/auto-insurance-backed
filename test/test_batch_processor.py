"""
车险出单智能体 - 批量处理测试
"""
import pytest

from src.core.engine import engine
from src.core.state import session_store
from src.api.schemas import UserRole


class TestBatchProcessor:

    @pytest.fixture(autouse=True)
    def setup(self):
        self.agent_id = "batch_agent_001"

    @pytest.mark.asyncio
    async def test_batch_query_empty(self):
        result = await engine.batch_query_by_plates(self.agent_id, [])
        assert result["success"]
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_batch_query_not_found(self):
        result = await engine.batch_query_by_plates(self.agent_id, ["京X00000"])
        assert result["success"]
        assert result["total"] == 1
        assert result["results"][0]["phase"] == "not_found"

    @pytest.mark.asyncio
    async def test_batch_query_found(self):
        plate = "京B67890"
        session = await session_store.get_or_create_by_plate(plate, self.agent_id)

        result = await engine.batch_query_by_plates(self.agent_id, [plate])
        assert result["success"]
        assert result["total"] == 1
        assert result["results"][0]["plate"] == plate
        assert result["results"][0]["session_id"] == session.session_id

    @pytest.mark.asyncio
    async def test_batch_create_sessions(self):
        plates = ["京A11111", "京B22222"]
        result = await engine.batch_create_sessions(self.agent_id, plates)
        assert result["success"]
        assert result["total"] == 2
        for r in result["results"]:
            assert r["session_id"]

    @pytest.mark.asyncio
    async def test_batch_create_with_invalid_plate(self):
        plates = ["京A33333", "INVALID"]
        result = await engine.batch_create_sessions(self.agent_id, plates)
        assert result["total"] == 2
        valid_count = sum(1 for r in result["results"] if "session_id" in r)
        assert valid_count == 1

    @pytest.mark.asyncio
    async def test_agent_sessions_list(self):
        sessions = await session_store.get_agent_sessions(self.agent_id)
        assert isinstance(sessions, list)