"""业务员会话报表导出测试。"""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from openpyxl import load_workbook

from src.api.routes_agent import export_agent_sessions_report
from src.api.schemas import (
    IDCardInfo,
    InsurancePlan,
    PRESET_COMPANIES,
    PRESET_INSURANCE_ITEMS,
    Session,
    VehicleInfo,
    WorkflowPhase,
    now_utc,
)
from src.core.state import session_store
from src.storage.repository import TakeoverRepository


class _DbContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_export_agent_sessions_report():
    plan = InsurancePlan(
        company=PRESET_COMPANIES["pingan"],
        items=[
            PRESET_INSURANCE_ITEMS["compulsory"],
            PRESET_INSURANCE_ITEMS["third_party_1m"],
        ],
        discount_rate=PRESET_COMPANIES["pingan"].discount_rate,
    )
    plan.calculate_total()
    session = Session(
        session_id="report-session",
        agent_id="agent_001",
        user_id="customer_001",
        phase=WorkflowPhase.RECOMMENDING,
        license_plate="京A12345",
        vehicle_info=VehicleInfo(
            license_plate="京A12345",
            vin="LSVAA49J0N2123456",
            brand_model="大众牌SVW71521DF",
            owner_name="张三",
        ),
        applicant_id_card=IDCardInfo(
            name="张三",
            id_number="110101199003071234",
        ),
        insurance_plan=plan,
        updated_at=now_utc(),
    )
    session.append_conversation_message("customer", "请帮我调整三者险")
    takeover = SimpleNamespace(
        session_id="report-session",
        handle_status="pending",
        reason="OCR识别失败",
        handler_id="",
        requested_at=now_utc(),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth=SimpleNamespace(
                role="agent",
                agent_id="agent_001",
                name="演示业务员",
                subject="agent_001",
            )
        )
    )

    with (
        patch.object(
            session_store,
            "get_agent_sessions",
            new=AsyncMock(return_value=[session]),
        ),
        patch(
            "src.api.routes_agent.get_db",
            return_value=_DbContext(),
        ),
        patch.object(
            TakeoverRepository,
            "list_by_status",
            new=AsyncMock(return_value=[takeover]),
        ),
    ):
        response = await export_agent_sessions_report(
            request,
            phase="all",
            takeover_status="pending",
            keyword="京A",
            hide_empty=True,
        )

    body = b"".join([chunk async for chunk in response.body_iterator])
    workbook = load_workbook(BytesIO(body))
    sheet = workbook["会话报表"]

    assert response.media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "业务员会话报表" in sheet["A1"].value
    assert sheet["A5"].value == 1
    assert sheet["D5"].value == "京A12345"
    assert sheet["S5"].value == "待认领"
    assert sheet["A4"].value == "序号"
    assert sheet.freeze_panes == "A5"
