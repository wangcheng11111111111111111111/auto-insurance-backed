"""
车险出单智能体 - OCR 识别服务
负责行驶证正副页、身份证正反面的 OCR 识别
识别结果由引擎校验，缺失字段自动触发转人工
"""
import base64
import asyncio
import json
import logging
import re
from datetime import date, datetime
from io import BytesIO
from typing import Optional

from openai import AsyncOpenAI

from src.api.schemas import VehicleInfo, IDCardInfo
from src.utils.plate_validator import (
    extract_plate_from_text,
    is_valid_plate,
    normalize_plate,
)
from config.settings import settings

logger = logging.getLogger(__name__)


class OCRService:
    """证件识别服务。

    默认使用多模态大模型直接识别并抽取字段；
    多模态模型失败或字段不完整时，才依次使用专用 OCR 模型和本地 OCR 兜底。
    """

    @property
    def _vision_enabled(self) -> bool:
        return bool(settings.vision.MODEL)

    @property
    def _vision_api_key(self) -> str:
        return settings.vision.API_KEY or ""

    @property
    def _vision_base_url(self) -> str:
        return settings.vision.BASE_URL or ""

    @property
    def _vision_models(self) -> list[str]:
        models = [
            settings.vision.MODEL or "",
            settings.vision.FALLBACK_MODEL or "",
        ]
        return list(dict.fromkeys(model for model in models if model))

    @property
    def _ocr_fallback_model(self) -> str:
        return settings.vision.OCR_FALLBACK_MODEL or ""

    @staticmethod
    def _image_data_url(image_bytes: bytes) -> str:
        """按图片魔数返回正确的 data URL，避免把 PNG 错标成 JPEG。"""
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif image_bytes.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            mime = "image/webp"
        elif image_bytes.startswith(b"BM"):
            mime = "image/bmp"
        else:
            mime = "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"

    async def _vision_chat(
        self,
        client: AsyncOpenAI,
        model: str,
        prompt: str,
        image_bytes: bytes,
        max_tokens: int,
    ) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": self._image_data_url(image_bytes)},
                    },
                ],
            }],
            temperature=0.1,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _vehicle_ready(vehicle: VehicleInfo) -> bool:
        return bool(
            is_valid_plate(vehicle.license_plate)
            and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vehicle.vin or "")
            and len((vehicle.brand_model or "").strip()) >= 2
            and vehicle.brand_model not in {"号", "型号", "品牌型号"}
        )

    @staticmethod
    def _vehicle_score(vehicle: VehicleInfo) -> int:
        return sum(1 for field in (
            vehicle.license_plate,
            vehicle.vin,
            vehicle.engine_number,
            vehicle.brand_model,
            vehicle.owner_name,
            vehicle.registration_date,
        ) if field)

    async def _recognize_vehicle_with_model(
        self,
        client: AsyncOpenAI,
        model: str,
        front_image: bytes,
        back_image: bytes,
    ) -> VehicleInfo:
        vehicle = VehicleInfo()
        prompt = (
            "请识别这张行驶证主页图片。字段值位于标签右侧，"
            "不要把标签、页眉或说明文字当作字段值。"
            "品牌型号必须读取完整的品牌与厂牌型号，不能只返回“号”或“型号”。"
            "以 JSON 格式返回：\n"
            '{"号牌号码":"", "车辆类型":"", "所有人":"", "使用性质":"", '
            '"品牌型号":"", "车辆识别代号":"", "发动机号码":"", '
            '"注册日期":"", "核定载人数":""}\n'
            "只返回 JSON，不要其他内容。"
        )
        front_result, back_result = await asyncio.gather(
            self._vision_chat(
                client, model, prompt, front_image, max_tokens=2048
            ),
            self._vision_chat(
                client,
                model,
                "识别图片中的所有文字，原样输出。",
                back_image,
                max_tokens=1024,
            ),
            return_exceptions=True,
        )
        if isinstance(front_result, Exception):
            raise front_result
        self._parse_vehicle_json(front_result, vehicle)

        if isinstance(back_result, Exception):
            logger.warning("%s 行驶证副页视觉识别失败: %s", model, back_result)
        else:
            self._parse_vehicle_from_text(
                back_result, vehicle, include_main_fields=False
            )
        return vehicle

    async def _ocr_read_text(
        self,
        client: AsyncOpenAI,
        image_bytes: bytes,
        max_tokens: int = 2048,
    ) -> str:
        if not self._ocr_fallback_model:
            raise RuntimeError("未配置专用 OCR 兜底模型")
        return await self._vision_chat(
            client,
            self._ocr_fallback_model,
            "识别图片中的全部文字，原样逐行输出，不要解释。",
            image_bytes,
            max_tokens=max_tokens,
        )

    def _parse_vehicle_ocr_text(self, text: str, vehicle: VehicleInfo) -> None:
        """解析专用 OCR 输出的纯文本，用于多模态模型失败后的兜底。"""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        plate = extract_plate_from_text(text)
        if plate:
            vehicle.license_plate = normalize_plate(plate)

        vin_match = re.search(r"[A-HJ-NPR-Z0-9]{17}", text.upper())
        if vin_match:
            vehicle.vin = vin_match.group(0)

        date_match = re.search(
            r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", text
        )
        if date_match:
            vehicle.registration_date = self._parse_date(
                f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
            )

        seat_match = re.search(r"核定载人数[：:\s]*(\d+)人?", text)
        if not seat_match:
            seat_match = next(
                (re.match(r"^(\d+)人$", line) for line in lines if re.match(r"^\d+人$", line)),
                None,
            )
        if seat_match:
            vehicle.seat_count = int(seat_match.group(1))

        labels = {
            "中华人民共和国机动车行驶证",
            "行驶证正页",
            "行驶证副页",
            "号牌号码",
            "车辆类型",
            "所有人",
            "使用性质",
            "品牌型号",
            "车辆识别代号",
            "发动机号码",
            "注册日期",
            "发证日期",
            "核定载人数",
            "档案编号",
            "总质量",
            "整备质量",
            "核定载质量",
            "外廓尺寸",
            "检验记录",
        }
        values = [
            line for line in lines
            if line not in labels
            and not line.startswith("仅供")
            and not line.startswith("合成测试")
            and not line.startswith("行驶证")
            and "有效期" not in line
        ]

        type_line = next(
            (
                line for line in values
                if any(k in line for k in ("轿车", "客车", "货车", "越野车", "面包车"))
            ),
            "",
        )
        if type_line:
            vehicle.vehicle_type = type_line

        use_line = next((line for line in values if "营运" in line), "")
        if use_line:
            vehicle.use_type = use_line

        brand_line = next(
            (
                line for line in values
                if len(line) >= 4
                and "牌" in line
                and "号牌" not in line
                and line != vehicle.license_plate
            ),
            "",
        )
        if brand_line:
            vehicle.brand_model = brand_line

        owner_line = next(
            (
                line for line in values
                if re.fullmatch(r"[\u4e00-\u9fa5·]{2,8}", line)
                and line not in {"小型轿车", "非营运", "营运", "机动车行驶证"}
            ),
            "",
        )
        if owner_line:
            vehicle.owner_name = owner_line

        engine_candidates = [
            line for line in values
            if re.fullmatch(r"[A-Z0-9-]{5,20}", line)
            and line not in {vehicle.vin, vehicle.license_plate or ""}
            and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", line)
        ]
        if engine_candidates:
            vehicle.engine_number = engine_candidates[0]

    @staticmethod
    def _merge_vehicle(target: VehicleInfo, source: VehicleInfo) -> None:
        for field in (
            "license_plate", "vin", "engine_number", "brand_model",
            "owner_name", "registration_date",
        ):
            if not getattr(target, field) and getattr(source, field):
                setattr(target, field, getattr(source, field))

    async def recognize_vehicle_license(
        self, front_image: bytes, back_image: bytes
    ) -> VehicleInfo:
        """
        识别行驶证正副页。

        默认使用多模态大模型识别并抽取字段；
        字段不完整时使用专用 OCR 模型兜底；
        专用 OCR 也不可用时再尝试本地 tesseract OCR。

        Args:
            front_image: 行驶证主页图片 bytes
            back_image: 行驶证副页图片 bytes

        Returns:
            VehicleInfo 对象（字段可能缺失，引擎自动补位）
        """
        logger.info("开始使用多模态模型识别行驶证正副页 ...")
        vehicle = VehicleInfo()

        if not self._vision_enabled:
            logger.info("VISION_MODEL 未配置，使用本地 OCR 识别 ...")
            return self._local_ocr_vehicle(front_image, back_image)

        # --- 视觉 LLM 识别 ---
        vision_client = AsyncOpenAI(
            api_key=self._vision_api_key,
            base_url=self._vision_base_url,
            timeout=settings.vision.TIMEOUT_SECONDS,
            max_retries=0,
        )

        best_vehicle: Optional[VehicleInfo] = None
        best_model = ""
        best_score = -1
        for model in self._vision_models:
            try:
                candidate = await self._recognize_vehicle_with_model(
                    vision_client, model, front_image, back_image
                )
                score = self._vehicle_score(candidate)
                if score > best_score:
                    best_vehicle = candidate
                    best_model = model
                    best_score = score
                if self._vehicle_ready(candidate):
                    vehicle = candidate
                    break
            except Exception as e:
                logger.warning("%s 行驶证视觉识别失败: %s", model, e)

        if best_vehicle is not None:
            vehicle = best_vehicle
            logger.info("行驶证视觉识别使用模型: %s", best_model)

        if not self._vehicle_ready(vehicle) and self._ocr_fallback_model:
            logger.info(
                "多模态模型字段不完整，使用专用 OCR 兜底: %s",
                self._ocr_fallback_model,
            )
            ocr_vehicle = VehicleInfo()
            try:
                front_result, back_result = await asyncio.gather(
                    self._ocr_read_text(
                        vision_client, front_image, max_tokens=2048
                    ),
                    self._ocr_read_text(
                        vision_client, back_image, max_tokens=1024
                    ),
                    return_exceptions=True,
                )
                if isinstance(front_result, str):
                    self._parse_vehicle_ocr_text(front_result, ocr_vehicle)
                if isinstance(back_result, str):
                    self._parse_vehicle_from_text(
                        back_result, ocr_vehicle, include_main_fields=False
                    )
                if isinstance(front_result, Exception) and isinstance(
                    back_result, Exception
                ):
                    raise front_result
                self._merge_vehicle(vehicle, ocr_vehicle)
            except Exception as e:
                logger.warning("专用 OCR 兜底失败: %s", e)

        if not self._vehicle_ready(vehicle):
            local_vehicle = self._local_ocr_vehicle(front_image, back_image)
            self._merge_vehicle(vehicle, local_vehicle)

        extracted = sum(1 for f in [
            vehicle.license_plate, vehicle.vin, vehicle.engine_number,
            vehicle.brand_model, vehicle.owner_name, vehicle.registration_date,
        ] if f)
        logger.info("行驶证识别完成: plate=%s, 字段提取 %d/6",
                     vehicle.license_plate or "(未识别)", extracted)
        return vehicle

    def _parse_vehicle_json(self, raw_text: str, vehicle: VehicleInfo):
        """从 LLM 返回的 JSON 中解析车辆信息"""
        try:
            json_match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                if data.get("号牌号码"):
                    plate = normalize_plate(str(data["号牌号码"]))
                    if is_valid_plate(plate):
                        vehicle.license_plate = plate
                if data.get("车辆类型"):
                    vehicle.vehicle_type = self._clean_field(data["车辆类型"])
                if data.get("所有人"):
                    owner = self._clean_field(data["所有人"])
                    if owner not in {"所有人", "姓名", "号"}:
                        vehicle.owner_name = owner
                if data.get("使用性质"):
                    vehicle.use_type = self._clean_field(data["使用性质"])
                if data.get("品牌型号"):
                    brand = self._clean_field(data["品牌型号"])
                    if brand not in {"号", "型号", "品牌型号"}:
                        vehicle.brand_model = brand
                if data.get("车辆识别代号"):
                    vin_match = re.search(
                        r"[A-HJ-NPR-Z0-9]{17}",
                        str(data["车辆识别代号"]).upper(),
                    )
                    if vin_match:
                        vehicle.vin = vin_match.group(0)
                if data.get("发动机号码"):
                    vehicle.engine_number = self._clean_field(data["发动机号码"])
                if data.get("注册日期"):
                    raw_date = data["注册日期"]
                    vehicle.registration_date = self._parse_date(raw_date)
                if data.get("核定载人数"):
                    try:
                        vehicle.seat_count = int(re.findall(r'\d+', str(data["核定载人数"]))[0])
                    except (IndexError, ValueError):
                        pass
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("JSON 解析失败: %s", e)

    @staticmethod
    def _clean_field(value: object) -> str:
        return str(value or "").strip().strip("\"'“”")

    def _parse_vehicle_from_text(
        self,
        text: str,
        vehicle: VehicleInfo,
        include_main_fields: bool = True,
    ):
        """从传统 OCR 文本中提取车辆信息（兜底方案）"""
        if include_main_fields:
            plate = extract_plate_from_text(text)
            if plate:
                vehicle.license_plate = normalize_plate(plate)

        vin_match = re.search(r'[A-HJ-NPR-Z0-9]{17}', text)
        if vin_match:
            vehicle.vin = vin_match.group(0)

        eng_match = re.search(r'发动机号[码号]?[:\s]*([A-Z0-9-]+)', text, re.I)
        if eng_match:
            vehicle.engine_number = eng_match.group(1).upper()

        if include_main_fields:
            owner_match = re.search(r'所有人[:\s]*(\S+)', text)
            if owner_match:
                owner = self._clean_field(owner_match.group(1))
                if owner not in {"所有人", "姓名", "号"}:
                    vehicle.owner_name = owner

            model_match = re.search(r'品牌[型号][:\s]*(\S+)', text)
            if model_match:
                model = self._clean_field(model_match.group(1))
                if model not in {"号", "型号", "品牌型号"}:
                    vehicle.brand_model = model

    def validate_vehicle_info(self, vehicle: VehicleInfo) -> tuple[bool, list[str]]:
        """
        校验车辆信息完整性。

        Returns:
            (is_valid, missing_fields)
        """
        missing = []
        if not vehicle.license_plate:
            missing.append("车牌号")
        if not vehicle.vin:
            missing.append("车架号(VIN)")
        if not vehicle.brand_model:
            missing.append("品牌型号")
        if not vehicle.owner_name:
            missing.append("所有人姓名")
        if not vehicle.registration_date:
            missing.append("注册日期")
        return len(missing) == 0, missing

    def _local_ocr_vehicle(self, front_image: bytes, back_image: bytes) -> VehicleInfo:
        """
        本地 OCR 识别行驶证（不依赖 LLM）。

        使用 pytesseract 提取图片文字，再用正则解析关键字段。
        若 tesseract 未安装或识别失败，返回空 VehicleInfo → 转人工。
        """
        vehicle = VehicleInfo()

        try:
            import pytesseract
            from PIL import Image

            front_text = pytesseract.image_to_string(
                Image.open(BytesIO(front_image)), lang="chi_sim+eng"
            )
            back_text = pytesseract.image_to_string(
                Image.open(BytesIO(back_image)), lang="chi_sim+eng"
            )
            combined = front_text + "\n" + back_text
            logger.info("tesseract OCR 输出: %s", combined[:200])

            self._parse_vehicle_from_text(combined, vehicle)
        except ImportError:
            logger.warning(
                "pytesseract 未安装，无法本地 OCR。"
                "安装方法: pip install pytesseract Pillow，并安装 Tesseract-OCR"
            )
        except Exception as e:
            logger.warning("本地 OCR 失败: %s", e)

        extracted = sum(1 for f in [
            vehicle.license_plate, vehicle.vin, vehicle.brand_model,
            vehicle.owner_name,
        ] if f)
        logger.info("本地 OCR 完成: plate=%s, 字段提取 %d/4",
                     vehicle.license_plate or "(未识别)", extracted)
        return vehicle

    def _local_ocr_id_card(self, front_image: bytes, back_image: bytes) -> IDCardInfo:
        """本地 OCR 识别身份证（不依赖 LLM）"""
        id_card = IDCardInfo()

        try:
            import pytesseract
            from PIL import Image

            front_text = pytesseract.image_to_string(
                Image.open(BytesIO(front_image)), lang="chi_sim"
            )
            back_text = pytesseract.image_to_string(
                Image.open(BytesIO(back_image)), lang="chi_sim"
            )

            name_match = re.search(r'姓名[:\s]*(\S+)', front_text)
            if name_match:
                id_card.name = name_match.group(1)

            id_match = re.search(r'\d{17}[\dX]', front_text)
            if id_match:
                id_card.id_number = id_match.group(0)

            if "男" in front_text:
                id_card.gender = "男"
            elif "女" in front_text:
                id_card.gender = "女"

            addr_match = re.search(r'住址[:\s]*(.+)', front_text)
            if addr_match:
                id_card.address = addr_match.group(1).strip()

            birth_match = re.search(r'\d{4}[年.\-/]\d{1,2}[月.\-/]\d{1,2}', front_text)
            if birth_match:
                id_card.birth_date = self._parse_date(birth_match.group(0))

            auth_match = re.search(r'签发机关[:\s]*(.+)', back_text)
            if auth_match:
                id_card.issuing_authority = auth_match.group(1).strip()

        except ImportError:
            logger.warning("pytesseract 未安装，无法本地 OCR")
        except Exception as e:
            logger.warning("身份证本地 OCR 失败: %s", e)

        logger.info("身份证本地 OCR 完成: name=%s", id_card.name or "(未识别)")
        return id_card

    @staticmethod
    def _id_card_ready(id_card: IDCardInfo) -> bool:
        return bool(
            id_card.name
            and id_card.name not in {"姓名", "民族", "住址"}
            and re.fullmatch(r"\d{17}[\dX]", id_card.id_number or "")
        )

    @staticmethod
    def _id_card_score(id_card: IDCardInfo) -> int:
        return sum(1 for field in (
            id_card.name,
            id_card.id_number,
            id_card.gender,
            id_card.birth_date,
            id_card.address,
            id_card.issuing_authority,
        ) if field)

    async def _recognize_id_card_with_model(
        self,
        client: AsyncOpenAI,
        model: str,
        front_image: bytes,
        back_image: bytes,
    ) -> IDCardInfo:
        id_card = IDCardInfo()
        front_prompt = (
            "请识别这张身份证正面图片，提取以下字段并以 JSON 格式返回：\n"
            '{"姓名":"", "性别":"", "民族":"", "出生日期":"", '
            '"住址":"", "公民身份号码":""}\n'
            "只返回 JSON，不要其他内容。"
        )
        back_prompt = (
            "请识别这张身份证背面图片，提取以下字段并以 JSON 格式返回：\n"
            '{"签发机关":"", "有效期限":""}\n'
            "只返回 JSON，不要其他内容。"
        )
        front_result, back_result = await asyncio.gather(
            self._vision_chat(
                client, model, front_prompt, front_image, max_tokens=1024
            ),
            self._vision_chat(
                client, model, back_prompt, back_image, max_tokens=512
            ),
            return_exceptions=True,
        )
        if isinstance(front_result, Exception):
            raise front_result
        self._parse_idcard_json(front_result, id_card)

        if isinstance(back_result, Exception):
            logger.warning("%s 身份证背面视觉识别失败: %s", model, back_result)
        else:
            self._parse_idcard_back_json(back_result, id_card)
        return id_card

    def _parse_id_card_ocr_text(self, text: str, id_card: IDCardInfo) -> None:
        name_match = re.search(r"姓名[：:\s]*([\u4e00-\u9fa5·]{2,8})", text)
        if name_match:
            id_card.name = name_match.group(1)

        id_match = re.search(r"\d{17}[\dX]", text.upper())
        if id_match:
            id_card.id_number = id_match.group(0)

        gender_match = re.search(r"性别[：:\s]*(男|女)", text)
        if gender_match:
            id_card.gender = gender_match.group(1)

        ethnicity_match = re.search(r"民族[：:\s]*([\u4e00-\u9fa5]{1,8})", text)
        if ethnicity_match:
            id_card.ethnicity = ethnicity_match.group(1)

        birth_match = re.search(
            r"出生[：:\s]*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", text
        )
        if birth_match:
            id_card.birth_date = self._parse_date(
                f"{birth_match.group(1)}-{birth_match.group(2)}-{birth_match.group(3)}"
            )

        address_match = re.search(r"住址[：:\s]*([^\n]+)", text)
        if address_match:
            id_card.address = self._clean_field(address_match.group(1))

        authority_match = re.search(r"签发机关[：:\s]*([^\n]+)", text)
        if authority_match:
            id_card.issuing_authority = self._clean_field(authority_match.group(1))

        validity_match = re.search(
            r"有效期限[：:\s]*([0-9.\-/]+)\s*[-–—~至]\s*([0-9.\-/]+|长期)",
            text,
        )
        if validity_match:
            self._parse_validity_period(
                f"{validity_match.group(1)}-{validity_match.group(2)}",
                id_card,
            )

    @staticmethod
    def _merge_id_card(target: IDCardInfo, source: IDCardInfo) -> None:
        for field in (
            "name", "id_number", "gender", "ethnicity", "birth_date",
            "address", "issuing_authority", "valid_from", "valid_to",
        ):
            if not getattr(target, field) and getattr(source, field):
                setattr(target, field, getattr(source, field))

    async def recognize_id_card(
        self, front_image: bytes, back_image: bytes
    ) -> IDCardInfo:
        """
        识别身份证正反面。

        默认使用多模态大模型识别图片并抽取字段；
        字段不完整时使用专用 OCR 模型和本地 OCR 依次兜底。
        """
        logger.info("开始使用多模态模型识别身份证正反面 ...")
        id_card = IDCardInfo()

        if not self._vision_enabled:
            logger.info("VISION_MODEL 未配置，使用本地 OCR 识别身份证 ...")
            return self._local_ocr_id_card(front_image, back_image)

        # --- 视觉 LLM 识别 ---
        vision_client = AsyncOpenAI(
            api_key=self._vision_api_key,
            base_url=self._vision_base_url,
            timeout=settings.vision.TIMEOUT_SECONDS,
            max_retries=0,
        )

        best_id_card: Optional[IDCardInfo] = None
        best_model = ""
        best_score = -1
        for model in self._vision_models:
            try:
                candidate = await self._recognize_id_card_with_model(
                    vision_client, model, front_image, back_image
                )
                score = self._id_card_score(candidate)
                if score > best_score:
                    best_id_card = candidate
                    best_model = model
                    best_score = score
                if self._id_card_ready(candidate):
                    id_card = candidate
                    break
            except Exception as e:
                logger.warning("%s 身份证视觉识别失败: %s", model, e)

        if best_id_card is not None:
            id_card = best_id_card
            logger.info("身份证视觉识别使用模型: %s", best_model)

        if not self._id_card_ready(id_card) and self._ocr_fallback_model:
            logger.info(
                "多模态模型字段不完整，使用专用 OCR 兜底: %s",
                self._ocr_fallback_model,
            )
            ocr_id_card = IDCardInfo()
            try:
                front_result, back_result = await asyncio.gather(
                    self._ocr_read_text(
                        vision_client, front_image, max_tokens=1024
                    ),
                    self._ocr_read_text(
                        vision_client, back_image, max_tokens=512
                    ),
                    return_exceptions=True,
                )
                if isinstance(front_result, Exception) and isinstance(
                    back_result, Exception
                ):
                    raise front_result
                self._parse_id_card_ocr_text(
                    "\n".join(
                        text
                        for text in (front_result, back_result)
                        if isinstance(text, str)
                    ),
                    ocr_id_card,
                )
                self._merge_id_card(id_card, ocr_id_card)
            except Exception as e:
                logger.warning("身份证专用 OCR 兜底失败: %s", e)

        if not self._id_card_ready(id_card):
            local_id_card = self._local_ocr_id_card(front_image, back_image)
            self._merge_id_card(id_card, local_id_card)

        extracted = sum(1 for f in [
            id_card.name, id_card.id_number, id_card.gender,
        ] if f)
        logger.info("身份证识别完成: name=%s, 字段提取 %d/3",
                     id_card.name or "(未识别)", extracted)
        return id_card

    def _parse_idcard_json(self, raw_text: str, id_card: IDCardInfo):
        """从 LLM 返回的 JSON 中解析身份证正面信息"""
        try:
            json_match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                name = self._clean_field(data.get("姓名", ""))
                if name not in {"姓名", "民族", "住址"}:
                    id_card.name = name
                id_card.gender = self._clean_field(data.get("性别", ""))
                id_card.ethnicity = self._clean_field(data.get("民族", ""))
                if data.get("出生日期"):
                    id_card.birth_date = self._parse_date(data["出生日期"])
                id_card.address = self._clean_field(data.get("住址", ""))
                if data.get("公民身份号码"):
                    id_match = re.search(
                        r"\d{17}[\dX]",
                        str(data["公民身份号码"]).upper(),
                    )
                    if id_match:
                        id_card.id_number = id_match.group(0)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("身份证 JSON 解析失败: %s", e)

    def _parse_idcard_back_json(self, raw_text: str, id_card: IDCardInfo):
        """从 LLM 返回的 JSON 中解析身份证背面信息"""
        try:
            json_match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                id_card.issuing_authority = data.get("签发机关", "")
                if data.get("有效期限"):
                    self._parse_validity_period(data["有效期限"], id_card)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("身份证背面 JSON 解析失败: %s", e)

    def _parse_validity_period(self, period_str: str, id_card: IDCardInfo):
        """解析有效期限，格式：2020.01.01-2040.01.01 或 2020.01.01-长期"""
        parts = re.split(r'[-–—~至]', period_str)
        if len(parts) >= 1:
            id_card.valid_from = self._parse_date(parts[0].strip())
        if len(parts) >= 2:
            if "长期" in parts[1]:
                id_card.valid_to = None  # 长期有效
            else:
                id_card.valid_to = self._parse_date(parts[1].strip())

    def validate_id_card(self, id_card: IDCardInfo) -> tuple[bool, list[str]]:
        """
        校验身份证信息完整性。

        仅校验 OCR 可提取的核心字段（姓名 + 身份证号码），
        图片 URL（front_image_url / back_image_url）由上传流程负责回填，
        不作为 OCR 结果的有效性判据，避免「未回填图片地址导致永远转人工」。

        Returns:
            (is_valid, missing_fields)
        """
        missing = []
        if not id_card.name:
            missing.append("姓名")
        if not id_card.id_number:
            missing.append("身份证号码")
        return len(missing) == 0, missing

    @staticmethod
    def _parse_date(date_str: str) -> Optional[date]:
        """解析日期字符串，支持多种格式"""
        if not date_str:
            return None
        date_str = date_str.strip().replace(".", "-").replace("/", "-")
        formats = ["%Y-%m-%d", "%Y%m%d", "%Y年%m月%d日"]
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            logger.warning("无法解析日期: %s", date_str)
            return None


ocr_service = OCRService()
