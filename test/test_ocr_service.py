"""OCR 服务配置与降级逻辑测试。"""
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from src.api.schemas import IDCardInfo, VehicleInfo
from src.services.ocr_service import OCRService


def test_image_data_url_uses_detected_mime_type():
    png = b"\x89PNG\r\n\x1a\n" + b"test"
    jpeg = b"\xff\xd8\xff" + b"test"
    webp = b"RIFF\x00\x00\x00\x00WEBP" + b"test"

    assert OCRService._image_data_url(png).startswith("data:image/png;base64,")
    assert OCRService._image_data_url(jpeg).startswith("data:image/jpeg;base64,")
    assert OCRService._image_data_url(webp).startswith("data:image/webp;base64,")


def test_incomplete_brand_is_not_considered_ready():
    vehicle = VehicleInfo(
        license_plate="京A12345",
        vin="LSVAA49J0N2123456",
        brand_model="号",
    )

    assert not OCRService._vehicle_ready(vehicle)


def test_parse_vehicle_from_dedicated_ocr_lines():
    service = OCRService()
    vehicle = VehicleInfo()
    service._parse_vehicle_ocr_text(
        "\n".join([
            "号牌号码",
            "车辆类型",
            "所有人",
            "使用性质",
            "品牌型号",
            "车辆识别代号",
            "发动机号码",
            "注册日期",
            "核定载人数",
            "京A12345",
            "小型轿车",
            "张三",
            "非营运",
            "大众牌SVW71521DF",
            "LSVAA49J0N2123456",
            "EA21112345",
            "2022-03-18",
            "5人",
        ]),
        vehicle,
    )

    assert vehicle.license_plate == "京A12345"
    assert vehicle.vin == "LSVAA49J0N2123456"
    assert vehicle.brand_model == "大众牌SVW71521DF"
    assert vehicle.owner_name == "张三"
    assert vehicle.registration_date.isoformat() == "2022-03-18"
    assert vehicle.seat_count == 5


def test_parse_id_card_from_dedicated_ocr_text():
    service = OCRService()
    id_card = IDCardInfo()
    service._parse_id_card_ocr_text(
        "\n".join([
            "姓名 张三",
            "性别 男",
            "民族 汉",
            "出生 1990年3月7日",
            "住址 北京市朝阳区示例路1号",
            "公民身份号码 110101199003071233",
            "签发机关 北京市公安局朝阳分局",
            "有效期限 2020.01.01-2040.01.01",
        ]),
        id_card,
    )

    assert id_card.name == "张三"
    assert id_card.id_number == "110101199003071233"
    assert id_card.birth_date.isoformat() == "1990-03-07"
    assert id_card.issuing_authority == "北京市公安局朝阳分局"
    assert id_card.valid_to.isoformat() == "2040-01-01"


@pytest.mark.asyncio
async def test_vehicle_ocr_falls_back_to_second_vision_model():
    service = OCRService()
    expected = VehicleInfo(
        license_plate="京A12345",
        vin="LSVAA49J0N2123456",
        brand_model="大众牌SVW71521DF",
        owner_name="张三",
    )
    recognize = AsyncMock(side_effect=[RuntimeError("Model disabled"), expected])

    with (
        patch.object(
            OCRService,
            "_vision_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch.object(
            OCRService,
            "_vision_models",
            new_callable=PropertyMock,
            return_value=["disabled-model", "working-model"],
        ),
        patch.object(service, "_recognize_vehicle_with_model", new=recognize),
        patch("src.services.ocr_service.AsyncOpenAI"),
    ):
        result = await service.recognize_vehicle_license(b"front", b"back")

    assert result.license_plate == "京A12345"
    assert result.brand_model == "大众牌SVW71521DF"
    assert recognize.await_count == 2


@pytest.mark.asyncio
async def test_vehicle_ocr_uses_dedicated_ocr_after_multimodal_failure():
    service = OCRService()
    recognize = AsyncMock(side_effect=RuntimeError("Model disabled"))
    read_text = AsyncMock(side_effect=[
        "\n".join([
            "号牌号码",
            "车辆类型",
            "所有人",
            "使用性质",
            "品牌型号",
            "车辆识别代号",
            "发动机号码",
            "注册日期",
            "核定载人数",
            "京A12345",
            "小型轿车",
            "张三",
            "非营运",
            "大众牌SVW71521DF",
            "LSVAA49J0N2123456",
            "EA21112345",
            "2022-03-18",
            "5人",
        ]),
        "行驶证副页",
    ])

    with (
        patch.object(
            OCRService,
            "_vision_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch.object(
            OCRService,
            "_vision_models",
            new_callable=PropertyMock,
            return_value=["disabled-model"],
        ),
        patch.object(
            OCRService,
            "_ocr_fallback_model",
            new_callable=PropertyMock,
            return_value="paddle-ocr",
        ),
        patch.object(service, "_recognize_vehicle_with_model", new=recognize),
        patch.object(service, "_ocr_read_text", new=read_text),
        patch("src.services.ocr_service.AsyncOpenAI"),
    ):
        result = await service.recognize_vehicle_license(b"front", b"back")

    assert result.license_plate == "京A12345"
    assert result.vin == "LSVAA49J0N2123456"
    assert result.brand_model == "大众牌SVW71521DF"
    assert read_text.await_count == 2
