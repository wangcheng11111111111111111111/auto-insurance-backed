"""
车险出单智能体 - 全局配置
所有配置项必须从 .env 读取，settings.py 不设硬编码默认值
修改配置只需改 .env，无需改此文件
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _resolve_path(raw: str | None):
    """将 .env 中的路径解析为绝对路径（相对路径基于 BASE_DIR）。"""
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


def _int(name: str, default: int = 0) -> int:
    """读取整数配置，缺失/为空/非法时回退默认值，避免启动崩溃。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float = 0.0) -> float:
    """读取浮点配置，缺失/为空/非法时回退默认值，避免启动崩溃。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    """读取布尔配置，支持 true/false、1/0、yes/no、on/off。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class DatabaseConfig:
    MYSQL_HOST = os.getenv("MYSQL_HOST")
    MYSQL_PORT = _int("MYSQL_PORT", 3306)
    MYSQL_USER = os.getenv("MYSQL_USER")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")

    REDIS_HOST = os.getenv("REDIS_HOST")
    REDIS_PORT = _int("REDIS_PORT", 6379)
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
    REDIS_DB = _int("REDIS_DB", 0)


class KnowledgeConfig:
    QDRANT_HOST = os.getenv("QDRANT_HOST")
    QDRANT_PORT = _int("QDRANT_PORT", 6333)
    QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION_NAME")
    KNOWLEDGE_BASE_DIR = _resolve_path(os.getenv("KNOWLEDGE_BASE_DIR"))


class ExternalAPIConfig:
    OCR_API_KEY = os.getenv("OCR_API_KEY")
    OCR_API_URL = os.getenv("OCR_API_URL")

    QUOTE_API_KEY = os.getenv("QUOTE_API_KEY")
    QUOTE_API_URL = os.getenv("QUOTE_API_URL")

    UNDERWRITING_API_KEY = os.getenv("UNDERWRITING_API_KEY")
    UNDERWRITING_API_URL = os.getenv("UNDERWRITING_API_URL")

    PAYMENT_API_KEY = os.getenv("PAYMENT_API_KEY")
    PAYMENT_API_URL = os.getenv("PAYMENT_API_URL")

    PROVIDER_API_TIMEOUT_SECONDS = _float("PROVIDER_API_TIMEOUT_SECONDS", 20.0)

    HUMAN_SERVICE_WEBHOOK = os.getenv("HUMAN_SERVICE_WEBHOOK_URL")


class SecurityConfig:
    SECRET_KEY = os.getenv("SECRET_KEY")
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
    TOKEN_EXPIRE_MINUTES = _int("TOKEN_EXPIRE_MINUTES", 60)

    # API 鉴权：AUTH_ENABLED=true 时所有接口要求 X-API-Key / Authorization / api_key
    AUTH_ENABLED = _bool("AUTH_ENABLED", False)
    API_KEY = os.getenv("AUTH_API_KEY")

    # 用户双 Token 登录
    USER_AUTH_ENABLED = _bool("USER_AUTH_ENABLED", True)
    ACCESS_TOKEN_MINUTES = _int("AUTH_ACCESS_TOKEN_MINUTES", 15)
    REFRESH_TOKEN_DAYS = _int("AUTH_REFRESH_TOKEN_DAYS", 7)
    REFRESH_COOKIE_NAME = os.getenv("AUTH_REFRESH_COOKIE_NAME", "cx_refresh_token")
    COOKIE_SECURE = _bool("AUTH_COOKIE_SECURE", False)
    DEMO_LOGIN_ENABLED = _bool("AUTH_DEMO_LOGIN_ENABLED", True)
    DEMO_CUSTOMER_ID = os.getenv("AUTH_DEMO_CUSTOMER_ID") or "customer_001"
    DEMO_CUSTOMER_PASSWORD = os.getenv("AUTH_DEMO_CUSTOMER_PASSWORD") or "Customer@123"
    DEMO_AGENT_ID = os.getenv("AUTH_DEMO_AGENT_ID") or "agent_001"
    DEMO_AGENT_PASSWORD = os.getenv("AUTH_DEMO_AGENT_PASSWORD") or "Agent@123"
    CLIENT_ENCRYPTION_ENABLED = _bool("AUTH_CLIENT_ENCRYPTION_ENABLED", True)
    REQUIRE_CLIENT_ENCRYPTION = _bool("AUTH_REQUIRE_CLIENT_ENCRYPTION", True)
    CUSTOMER_REGISTRATION_ENABLED = _bool("AUTH_CUSTOMER_REGISTRATION_ENABLED", True)
    AGENT_REGISTRATION_ENABLED = _bool("AUTH_AGENT_REGISTRATION_ENABLED", True)
    AGENT_REGISTRATION_CODE = os.getenv("AUTH_AGENT_REGISTRATION_CODE") or ""
    CLIENT_KEY_PATH = _resolve_path(
        os.getenv("AUTH_CLIENT_KEY_PATH") or ".secrets/client_encryption_private.pem"
    )


class LLMConfig:
    """大模型配置 - 支持 OpenAI / DeepSeek / Azure / Ollama"""
    PROVIDER = os.getenv("LLM_PROVIDER")

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL")
    OPENAI_TEMPERATURE = _float("OPENAI_TEMPERATURE", 0.3)
    OPENAI_MAX_TOKENS = _int("OPENAI_MAX_TOKENS", 4096)

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL")
    DEEPSEEK_TEMPERATURE = _float("DEEPSEEK_TEMPERATURE", 0.3)
    DEEPSEEK_MAX_TOKENS = _int("DEEPSEEK_MAX_TOKENS", 4096)

    AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
    AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
    AZURE_OPENAI_MAX_TOKENS = _int("AZURE_OPENAI_MAX_TOKENS", 4096)

    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")
    OLLAMA_TEMPERATURE = _float("OLLAMA_TEMPERATURE", 0.3)
    OLLAMA_MAX_TOKENS = _int("OLLAMA_MAX_TOKENS", 4096)


class EmbeddingConfig:
    """Embedding 向量模型配置"""
    PROVIDER = os.getenv("EMBEDDING_PROVIDER")
    API_KEY = os.getenv("EMBEDDING_API_KEY")
    BASE_URL = os.getenv("EMBEDDING_BASE_URL")
    MODEL = os.getenv("EMBEDDING_MODEL")
    DIMENSIONS = _int("EMBEDDING_DIMENSIONS", 1536)
    TIMEOUT = _int("EMBEDDING_TIMEOUT", 30)


class RerankConfig:
    """Rerank 重排序模型配置"""
    ENABLED = _bool("RERANK_ENABLED", False)
    PROVIDER = os.getenv("RERANK_PROVIDER")
    MODEL = os.getenv("RERANK_MODEL")


class VisionConfig:
    """Vision 视觉模型配置 - 用于行驶证/身份证 OCR"""
    PROVIDER = os.getenv("VISION_PROVIDER")
    API_KEY = os.getenv("VISION_API_KEY")
    BASE_URL = os.getenv("VISION_BASE_URL")
    MODEL = os.getenv("VISION_MODEL")
    FALLBACK_MODEL = os.getenv("VISION_FALLBACK_MODEL")
    OCR_FALLBACK_MODEL = os.getenv("VISION_OCR_FALLBACK_MODEL")
    TIMEOUT_SECONDS = _float("VISION_TIMEOUT_SECONDS", 45.0)


class AppConfig:
    APP_ENV = os.getenv("APP_ENV")
    IS_PRODUCTION = (os.getenv("APP_ENV") or "development").lower() in {
        "production",
        "prod",
    }
    LOG_LEVEL = os.getenv("LOG_LEVEL")
    DEBUG = os.getenv("APP_ENV") == "development"
    # CORS 允许来源，逗号分隔；为空时回退 ["*"]（此时自动禁用 credentials）
    CORS_ORIGINS = os.getenv("CORS_ORIGINS")
    # 内存会话 LRU 最大数量；超过后按 last_accessed 淘汰最久未使用的
    MAX_MEMORY_SESSIONS = _int("MAX_MEMORY_SESSIONS", 5000)
    # 会话无访问自动超时秒数（默认 24 小时）
    SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 3600 * 24)


class Settings:
    db = DatabaseConfig
    knowledge = KnowledgeConfig
    api = ExternalAPIConfig
    security = SecurityConfig
    llm = LLMConfig
    embedding = EmbeddingConfig
    rerank = RerankConfig
    vision = VisionConfig
    app = AppConfig


settings = Settings()
