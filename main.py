"""
车险出单智能体 - 主入口
FastAPI 应用启动
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.logging_config import setup_logging
from config.settings import settings
from scripts.import_knowledge import auto_sync_knowledge
from src.api.middleware import APIKeyAuthMiddleware, JWTAuthMiddleware
from src.api.routes_auth import auth_router
from src.api.routes_chat import chat_router
from src.api.routes_agent import agent_router
from src.api.routes_callbacks import router as callbacks_router
from src.core.state import session_store
from src.knowledge.parser import knowledge_parser
from src.services.auth_service import auth_service
import src.storage.models  # noqa: F401 - 确保所有 ORM 模型注册到 Base.metadata
from src.storage.qdrant_store import qdrant_store

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()

    # CORS 未配置明确白名单时，只在启动时提示一次（避免模块导入阶段重复打印）
    if _cors_fallback:
        logger.warning("CORS 未配置明确白名单，回退 allow_origins=['*']，自动关闭 credentials")

    # 初始化 MySQL + Redis + 会话存储（内部自带优雅降级，返回各服务状态）
    services_status = await session_store.start()
    await auth_service.ensure_demo_accounts()

    # 加载知识库 (依赖 MySQL 表；检测 xlsx 变化，变化则自动重导)
    try:
        imported = await auto_sync_knowledge()
        if imported:
            logger.info("知识库 xlsx 已变化，已自动重新导入")
        await knowledge_parser.load_from_db()
    except Exception as e:
        logger.error("知识库加载失败: %s", e)

    try:
        qdrant_store.connect()
        await qdrant_store.init_collection()
        services_status["qdrant"] = "connected"

        try:
            terms = knowledge_parser.get_all_terms()
            if terms:
                await qdrant_store.upsert_terms(terms)
                logger.info("知识库术语已同步到 Qdrant: %d 条", len(terms))
        except Exception as e:
            logger.warning("知识库同步 Qdrant 失败: %s", e)
    except Exception as e:
        services_status["qdrant"] = f"unavailable: {e}"
        logger.warning("Qdrant 不可用，知识库仅本地查询: %s", e)

    logger.info("车险出单智能体启动完成 | %s", services_status)
    yield

    # 优雅关闭（会话存储会排空 MySQL 落库队列并断开 Redis/MySQL）
    try:
        await session_store.shutdown()
    except Exception:
        pass
    try:
        qdrant_store.close()
    except Exception:
        pass
    logger.info("车险出单智能体已关闭")


app = FastAPI(
    title="车险出单智能体",
    description="Auto Insurance Agent - 车险出单全流程智能体",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(APIKeyAuthMiddleware)
app.add_middleware(JWTAuthMiddleware)

# CORS 配置：白名单优先；未配置时允许所有来源，但自动关闭 allow_credentials，
# 避免 FastAPI "allow_origins=['*'] + allow_credentials=True" 的无效组合
_raw_cors = (settings.app.CORS_ORIGINS or "").strip()
_cors_fallback = False
if _raw_cors and _raw_cors != "*":
    _allowed_origins = [o.strip() for o in _raw_cors.split(",") if o.strip()]
    _allow_credentials = True
else:
    _allowed_origins = ["*"]
    _allow_credentials = False
    _cors_fallback = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(agent_router)
app.include_router(auth_router)
app.include_router(callbacks_router)


@app.get("/")
async def root():
    return {
        "service": "车险出单智能体",
        "version": "1.0.0",
        "status": "running",
        "active_sessions": session_store.active_count,
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "active_sessions": session_store.active_count}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app.DEBUG,
    )
