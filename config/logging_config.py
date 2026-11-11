"""
车险出单智能体 - 日志配置
"""
import logging
import sys
from config.settings import settings


def setup_logging():
    level = getattr(logging, settings.app.LOG_LEVEL.upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logging.getLogger(__name__).info("日志系统初始化完成, level=%s", settings.app.LOG_LEVEL)