"""
API 鉴权中间件 - 全局轻量 API Key 校验

逻辑：
- AUTH_ENABLED=false 时完全跳过校验，兼容开发/测试环境；
- 启用后读取以下任意一处的 API Key 做比对：
  1. Header X-API-Key
  2. Header Authorization (Bearer <key>)
  3. Query 参数 api_key
- 未通过返回 401 Unauthorized。
"""
import logging
import secrets
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config.settings import settings
from src.services.auth_service import AuthError, auth_service

logger = logging.getLogger(__name__)


def _extract_api_key(request: Request) -> Optional[str]:
    """从请求中提取 API Key，返回 None 表示未提供。"""
    if "x-api-key" in request.headers:
        return request.headers["x-api-key"].strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("api_key")


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 鉴权关闭（开发/测试环境）或 CORS 预检请求时直接放行
        if not settings.security.AUTH_ENABLED or request.method == "OPTIONS":
            return await call_next(request)

        expected = settings.security.API_KEY
        if not expected:
            logger.warning("AUTH_ENABLED=true 但 AUTH_API_KEY 未配置，拒绝所有请求")
            return _unauthorized()

        provided = _extract_api_key(request)
        if provided is None or not secrets.compare_digest(provided, expected):
            logger.warning("API Key 校验失败: method=%s path=%s", request.method, request.url.path)
            return _unauthorized()

        return await call_next(request)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """保护用户端和业务员端接口，并校验角色边界。"""

    PUBLIC_EXACT = {
        "/",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
    PUBLIC_PREFIXES = (
        "/api/auth/",
        "/api/payment/",
        "/api/underwriting/",
    )

    async def dispatch(self, request: Request, call_next):
        if (
            not settings.security.USER_AUTH_ENABLED
            or request.method == "OPTIONS"
            or request.url.path in self.PUBLIC_EXACT
            or request.url.path.startswith(self.PUBLIC_PREFIXES)
        ):
            return await call_next(request)

        path = request.url.path
        if not (path.startswith("/api/chat/") or path.startswith("/api/agent/")):
            return await call_next(request)

        token = _extract_bearer_token(request)
        if not token:
            return _jwt_unauthorized("请先登录")
        try:
            identity = auth_service.identity_from_access(token)
        except AuthError as e:
            logger.warning("访问 Token 校验失败: path=%s reason=%s", path, e)
            return _jwt_unauthorized("登录状态已失效")

        if path.startswith("/api/agent/") and identity.role != "agent":
            return JSONResponse(
                status_code=403, content={"detail": "该接口仅限业务员访问"}
            )
        if path.startswith("/api/chat/") and identity.role != "customer":
            return JSONResponse(
                status_code=403, content={"detail": "该接口仅限客户访问"}
            )
        request.state.auth = identity
        return await call_next(request)


def _extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _jwt_unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )
