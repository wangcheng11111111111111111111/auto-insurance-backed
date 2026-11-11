"""
车险出单智能体 - 车牌号校验工具
"""
import re


CHINA_PLATE_PATTERN = re.compile(
    r"^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁]"
    r"[A-HJ-NP-Z]"
    r"[A-HJ-NP-Z0-9]{4,5}"
    r"[A-HJ-NP-Z0-9挂学警港澳]$"
)

NEW_ENERGY_PLATE_PATTERN = re.compile(
    r"^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁]"
    r"[A-HJ-NP-Z]"
    r"[DF]"
    r"[A-HJ-NP-Z0-9]{4,5}$"
)


def is_valid_plate(plate: str) -> bool:
    """校验中国大陆车牌号是否合法"""
    if not plate or not isinstance(plate, str):
        return False
    plate = plate.strip().upper().replace(" ", "")
    if len(plate) < 7 or len(plate) > 8:
        return False
    return bool(
        CHINA_PLATE_PATTERN.match(plate)
        or NEW_ENERGY_PLATE_PATTERN.match(plate)
    )


def normalize_plate(plate: str) -> str:
    """标准化车牌号：去空格、转大写"""
    if not plate:
        return ""
    return plate.strip().upper().replace(" ", "")


def extract_plate_from_text(text: str) -> str:
    """从OCR识别文本中提取车牌号"""
    if not text:
        return ""
    text = text.upper().replace(" ", "")
    patterns = [
        r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]",
        r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-HJ-NP-Z][DF][A-HJ-NP-Z0-9]{4,5}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group()
    return ""