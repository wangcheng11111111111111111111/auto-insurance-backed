"""用户登录、Token 刷新与退出接口。"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from config.settings import settings
from src.services.auth_service import AuthError, auth_service
from src.services.client_crypto import ClientCryptoError, client_crypto_service

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/auth", tags=["登录鉴权"])


class LoginRequest(BaseModel):
    account: str = ""
    password: str = ""
    role: str = ""
    encrypted_payload: dict[str, str] | None = None


class RegisterRequest(BaseModel):
    role: str = ""
    account: str = ""
    name: str = ""
    phone: str = ""
    password: str = ""
    agent_id: str = ""
    employee_no: str = ""
    department: str = ""
    invite_code: str = ""
    encrypted_payload: dict[str, str] | None = None


_login_attempts: dict[tuple[str, str, str], tuple[int, float]] = {}


def _check_login_rate(request: Request, account: str, role: str) -> None:
    key = (request.client.host if request.client else "", role, account.lower())
    count, reset_at = _login_attempts.get(key, (0, 0.0))
    now = time.time()
    if reset_at <= now:
        _login_attempts.pop(key, None)
        return
    if count >= 5:
        raise HTTPException(status_code=429, detail="登录尝试过多，请稍后再试")


def _record_login_failure(request: Request, account: str, role: str) -> None:
    key = (request.client.host if request.client else "", role, account.lower())
    count, reset_at = _login_attempts.get(key, (0, 0.0))
    now = time.time()
    if reset_at <= now:
        count, reset_at = 0, now + 300
    _login_attempts[key] = (count + 1, reset_at)


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.security.REFRESH_COOKIE_NAME,
        value=token,
        max_age=settings.security.REFRESH_TOKEN_DAYS * 86400,
        httponly=True,
        secure=settings.security.COOKIE_SECURE,
        samesite="lax",
        path="/api/auth",
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.security.REFRESH_COOKIE_NAME,
        path="/api/auth",
        secure=settings.security.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


@auth_router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    if settings.security.REQUIRE_CLIENT_ENCRYPTION and not req.encrypted_payload:
        raise HTTPException(status_code=400, detail="登录请求必须使用前端加密")
    if req.encrypted_payload:
        try:
            encrypted = client_crypto_service.decrypt_json(req.encrypted_payload)
            account = str(encrypted.get("account") or "")
            password = str(encrypted.get("password") or "")
            role = str(encrypted.get("role") or "")
        except ClientCryptoError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        account, password, role = req.account, req.password, req.role

    _check_login_rate(request, account, role)
    try:
        identity = await auth_service.authenticate(
            account, password, role
        )
        tokens = await auth_service.issue_token_pair(identity)
        _set_refresh_cookie(response, tokens.pop("refresh_token"))
        _login_attempts.pop(
            (
                request.client.host if request.client else "",
                role,
                account.lower(),
            ),
            None,
        )
        return {"success": True, **tokens}
    except AuthError as e:
        _record_login_failure(request, account, role)
        raise HTTPException(status_code=401, detail=str(e))


@auth_router.get("/encryption-key")
async def encryption_key(response: Response):
    """返回浏览器应用层加密公钥。私钥始终只保留在服务端文件。"""
    if not settings.security.CLIENT_ENCRYPTION_ENABLED:
        raise HTTPException(status_code=404, detail="前端加密未启用")
    response.headers["Cache-Control"] = "no-store"
    return {
        "success": True,
        **client_crypto_service.public_key_payload(),
    }


@auth_router.post("/register")
async def register(req: RegisterRequest, request: Request, response: Response):
    if settings.security.REQUIRE_CLIENT_ENCRYPTION and not req.encrypted_payload:
        raise HTTPException(status_code=400, detail="注册请求必须使用前端加密")
    if req.encrypted_payload:
        try:
            values = client_crypto_service.decrypt_json(req.encrypted_payload)
        except ClientCryptoError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        values = req.model_dump()

    role = str(values.get("role") or "")
    account = str(values.get("account") or "")
    _check_login_rate(request, account, role)
    try:
        if role == "customer":
            identity = await auth_service.register_customer(
                user_id=account,
                name=str(values.get("name") or ""),
                phone=str(values.get("phone") or ""),
                password=str(values.get("password") or ""),
                agent_id=str(values.get("agent_id") or ""),
            )
        elif role == "agent":
            identity = await auth_service.register_agent(
                agent_id=account,
                name=str(values.get("name") or ""),
                phone=str(values.get("phone") or ""),
                employee_no=str(values.get("employee_no") or ""),
                department=str(values.get("department") or ""),
                password=str(values.get("password") or ""),
                invite_code=str(values.get("invite_code") or ""),
            )
        else:
            raise AuthError("注册角色无效")
        tokens = await auth_service.issue_token_pair(identity)
        _set_refresh_cookie(response, tokens.pop("refresh_token"))
        _login_attempts.pop(
            (
                request.client.host if request.client else "",
                role,
                account.lower(),
            ),
            None,
        )
        return {"success": True, **tokens}
    except AuthError as e:
        _record_login_failure(request, account, role)
        raise HTTPException(status_code=400, detail=str(e))


@auth_router.post("/refresh")
async def refresh_token(request: Request, response: Response):
    refresh_token = request.cookies.get(settings.security.REFRESH_COOKIE_NAME)
    try:
        tokens = await auth_service.refresh(refresh_token)
        _set_refresh_cookie(response, tokens.pop("refresh_token"))
        return {"success": True, **tokens}
    except AuthError as e:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail=str(e))


@auth_router.post("/logout")
async def logout(request: Request, response: Response):
    await auth_service.logout(
        request.cookies.get(settings.security.REFRESH_COOKIE_NAME)
    )
    _clear_refresh_cookie(response)
    return {"success": True}


@auth_router.get("/me")
async def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        identity = auth_service.identity_from_access(authorization[7:].strip())
        return {"success": True, "user": identity.to_dict()}
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
