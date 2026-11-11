"""
PII 加解密工具 - 敏感个人信息字段加密存储

采用 Fernet 对称加密（cryptography 库），密钥由 .env 的 ENCRYPTION_KEY 派生。
仅对白名单中的敏感字段（姓名/身份证/住址/电话/车牌/车架号等）加密，
其余字段保持明文，便于结构化检索。

优雅降级：
- 未配置 ENCRYPTION_KEY 或未安装 cryptography 时，明文透传（开发环境）；
- 解密遇到非加密字段（历史明文数据）时原样返回，保证向后兼容。
"""
import base64
import hashlib
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

# 需要加密存储的敏感字段名（作用于序列化后的字典 key，含嵌套 dataclass 字段）
PII_FIELDS = frozenset({
    "name", "id_number", "address", "phone", "contact_phone", "email",
    "license_plate", "vin", "engine_number", "owner_name",
})

# 密文前缀，用于区分「已加密」与「历史明文」
_PREFIX = "enc:"

_fernet_instance = None
_fernet_initialized = False


def _get_fernet():
    """由 ENCRYPTION_KEY 派生 Fernet 实例；不可用时返回 None。"""
    key = settings.security.ENCRYPTION_KEY
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning("cryptography 未安装，PII 字段将降级为明文存储")
        return None
    # 将任意长度密钥派生为 32 字节 urlsafe base64 Fernet key
    derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
    return Fernet(derived)


def _fernet():
    global _fernet_instance, _fernet_initialized
    if not _fernet_initialized:
        _fernet_instance = _get_fernet()
        _fernet_initialized = True
    return _fernet_instance


def encrypt_pii(value: str) -> str:
    """加密单个敏感字段值；非字符串、空值、已加密或加密失败时原样返回。"""
    if not isinstance(value, str) or value == "":
        return value
    if value.startswith(_PREFIX):  # 已是密文，避免重复加密
        return value
    f = _fernet()
    if f is None:
        return value
    try:
        return _PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")
    except Exception as e:  # 加密失败降级为明文，不阻断落库
        logger.warning("PII 加密失败，回退明文: %s", e)
        return value


def decrypt_pii(value: str) -> str:
    """解密单个敏感字段值；非密文或解密失败时保留密文，避免静默丢数据。"""
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value
    f = _fernet()
    if f is None:
        logger.error("检测到已加密 PII 但无可用密钥，无法解密，保留密文")
        return value
    try:
        return f.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as e:
        logger.error("PII 解密失败（密钥可能已变更），保留密文待恢复: %s", e)
        return value


def encrypt_pii_fields(data):
    """递归加密字典/列表中所有命中的敏感字段。"""
    if isinstance(data, dict):
        return {
            k: encrypt_pii(v) if (k in PII_FIELDS and isinstance(v, str)) else encrypt_pii_fields(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [encrypt_pii_fields(v) for v in data]
    return data


def decrypt_pii_fields(data):
    """递归解密字典/列表中所有命中的敏感字段。"""
    if isinstance(data, dict):
        return {
            k: decrypt_pii(v) if (k in PII_FIELDS and isinstance(v, str)) else decrypt_pii_fields(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [decrypt_pii_fields(v) for v in data]
    return data