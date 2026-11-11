"""用户与业务员双 Token 登录服务。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select

from config.settings import settings
from src.storage.models import AgentModel, UserModel
from src.storage.mysql import get_db

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """认证失败。"""


@dataclass
class AuthIdentity:
    subject: str
    role: str
    name: str
    account: str
    agent_id: str
    department: str = ""
    employee_no: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.subject,
            "role": self.role,
            "name": self.name,
            "account": self.account,
            "agent_id": self.agent_id,
            "department": self.department,
            "employee_no": self.employee_no,
        }


class AuthService:
    issuer = "auto-insurance-agent"
    audience = "auto-insurance-web"
    _refresh_memory: dict[str, float] = {}

    @property
    def secret(self) -> bytes:
        raw = settings.security.SECRET_KEY or ""
        if len(raw) < 16:
            raise AuthError("登录密钥未正确配置")
        return raw.encode("utf-8")

    @staticmethod
    def hash_password(password: str, iterations: int = 260_000) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return (
            f"pbkdf2_sha256${iterations}$"
            f"{base64.urlsafe_b64encode(salt).decode()}$"
            f"{base64.urlsafe_b64encode(digest).decode()}"
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            iterations = int(raw_iterations)
            salt = base64.urlsafe_b64decode(raw_salt.encode())
            expected = base64.urlsafe_b64decode(raw_digest.encode())
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, iterations
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    async def ensure_demo_accounts(self) -> None:
        """在开发环境为演示账号补齐密码哈希。"""
        if not settings.security.DEMO_LOGIN_ENABLED:
            return
        try:
            async with get_db() as db:
                agent = (
                    await db.execute(
                        select(AgentModel).where(
                            AgentModel.agent_id == settings.security.DEMO_AGENT_ID
                        )
                    )
                ).scalar_one_or_none()
                if agent is None:
                    agent = AgentModel(
                        agent_id=settings.security.DEMO_AGENT_ID,
                        name="演示业务员",
                        employee_no="A0001",
                        department="车险业务部",
                        status=1,
                        password_hash=self.hash_password(
                            settings.security.DEMO_AGENT_PASSWORD
                        ),
                    )
                    db.add(agent)
                elif not agent.password_hash:
                    agent.password_hash = self.hash_password(
                        settings.security.DEMO_AGENT_PASSWORD
                    )

                user = (
                    await db.execute(
                        select(UserModel).where(
                            UserModel.user_id == settings.security.DEMO_CUSTOMER_ID
                        )
                    )
                ).scalar_one_or_none()
                if user is None:
                    user = UserModel(
                        user_id=settings.security.DEMO_CUSTOMER_ID,
                        name="演示客户",
                        phone="13800000000",
                        agent_id=settings.security.DEMO_AGENT_ID,
                        password_hash=self.hash_password(
                            settings.security.DEMO_CUSTOMER_PASSWORD
                        ),
                    )
                    db.add(user)
                elif not user.password_hash:
                    user.password_hash = self.hash_password(
                        settings.security.DEMO_CUSTOMER_PASSWORD
                    )
        except Exception as e:
            logger.warning("演示登录账号初始化失败，将使用内存演示账号: %s", e)

    async def authenticate(
        self,
        account: str,
        password: str,
        role: str,
    ) -> AuthIdentity:
        account = account.strip()
        if not account or not password:
            raise AuthError("账号或密码错误")
        if role not in {"customer", "agent"}:
            raise AuthError("登录角色无效")

        try:
            if role == "agent":
                identity = await self._database_agent(account, password)
            else:
                identity = await self._database_customer(account, password)
            if identity:
                return identity
        except Exception as e:
            logger.warning("数据库登录查询失败，尝试演示账号: %s", e)

        demo = self._demo_identity(account, password, role)
        if demo:
            return demo
        raise AuthError("账号或密码错误")

    async def register_customer(
        self,
        *,
        user_id: str,
        name: str,
        phone: str,
        password: str,
        agent_id: str,
    ) -> AuthIdentity:
        if not settings.security.CUSTOMER_REGISTRATION_ENABLED:
            raise AuthError("客户注册暂未开放")
        user_id = self._validate_account(user_id, "客户ID")
        name = self._validate_name(name)
        phone = self._validate_phone(phone)
        self._validate_password(password)
        agent_id = (agent_id or settings.security.DEMO_AGENT_ID).strip()

        async with get_db() as db:
            duplicate = (
                await db.execute(
                    select(UserModel).where(
                        or_(
                            UserModel.user_id == user_id,
                            UserModel.phone == phone,
                        )
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if duplicate:
                raise AuthError("客户ID或手机号已注册")
            agent = (
                await db.execute(
                    select(AgentModel).where(
                        AgentModel.agent_id == agent_id,
                        AgentModel.status == 1,
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if not agent:
                raise AuthError("绑定的业务员不存在或已停用")
            db.add(UserModel(
                user_id=user_id,
                name=name,
                phone=phone,
                agent_id=agent_id,
                password_hash=self.hash_password(password),
            ))
        return AuthIdentity(
            subject=user_id,
            role="customer",
            name=name,
            account=user_id,
            agent_id=agent_id,
        )

    async def register_agent(
        self,
        *,
        agent_id: str,
        name: str,
        phone: str,
        employee_no: str,
        department: str,
        password: str,
        invite_code: str,
    ) -> AuthIdentity:
        if not settings.security.AGENT_REGISTRATION_ENABLED:
            raise AuthError("业务员注册暂未开放")
        expected_code = settings.security.AGENT_REGISTRATION_CODE or ""
        if not expected_code or not secrets.compare_digest(
            invite_code.strip(), expected_code
        ):
            raise AuthError("业务员邀请码无效")
        agent_id = self._validate_account(agent_id, "业务员ID")
        name = self._validate_name(name)
        phone = self._validate_phone(phone)
        self._validate_password(password)

        async with get_db() as db:
            duplicate = (
                await db.execute(
                    select(AgentModel).where(
                        or_(
                            AgentModel.agent_id == agent_id,
                            AgentModel.phone == phone,
                        )
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if duplicate:
                raise AuthError("业务员ID或手机号已注册")
            db.add(AgentModel(
                agent_id=agent_id,
                name=name,
                phone=phone,
                employee_no=employee_no.strip()[:50],
                department=department.strip()[:100],
                password_hash=self.hash_password(password),
                status=1,
            ))
        return AuthIdentity(
            subject=agent_id,
            role="agent",
            name=name,
            account=agent_id,
            agent_id=agent_id,
            department=department.strip()[:100],
            employee_no=employee_no.strip()[:50],
        )

    async def _database_agent(
        self, account: str, password: str
    ) -> AuthIdentity | None:
        async with get_db() as db:
            row = (
                await db.execute(
                    select(AgentModel).where(
                        AgentModel.status == 1,
                        or_(
                            AgentModel.agent_id == account,
                            AgentModel.employee_no == account,
                            AgentModel.phone == account,
                        ),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if row and row.password_hash and self.verify_password(
                password, row.password_hash
            ):
                return AuthIdentity(
                    subject=row.agent_id,
                    role="agent",
                    name=row.name,
                    account=row.agent_id,
                    agent_id=row.agent_id,
                    department=row.department or "",
                    employee_no=row.employee_no or "",
                )
        return None

    async def _database_customer(
        self, account: str, password: str
    ) -> AuthIdentity | None:
        async with get_db() as db:
            row = (
                await db.execute(
                    select(UserModel).where(
                        or_(
                            UserModel.user_id == account,
                            UserModel.phone == account,
                        )
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if row and row.password_hash and self.verify_password(
                password, row.password_hash
            ):
                return AuthIdentity(
                    subject=row.user_id,
                    role="customer",
                    name=row.name or "客户",
                    account=row.user_id,
                    agent_id=row.agent_id or settings.security.DEMO_AGENT_ID,
                )
        return None

    def _demo_identity(
        self, account: str, password: str, role: str
    ) -> AuthIdentity | None:
        if not settings.security.DEMO_LOGIN_ENABLED:
            return None
        if (
            role == "agent"
            and account == settings.security.DEMO_AGENT_ID
            and secrets.compare_digest(
                password, settings.security.DEMO_AGENT_PASSWORD
            )
        ):
            return AuthIdentity(
                subject=settings.security.DEMO_AGENT_ID,
                role="agent",
                name="演示业务员",
                account=settings.security.DEMO_AGENT_ID,
                agent_id=settings.security.DEMO_AGENT_ID,
                department="车险业务部",
                employee_no="A0001",
            )
        if (
            role == "customer"
            and account == settings.security.DEMO_CUSTOMER_ID
            and secrets.compare_digest(
                password, settings.security.DEMO_CUSTOMER_PASSWORD
            )
        ):
            return AuthIdentity(
                subject=settings.security.DEMO_CUSTOMER_ID,
                role="customer",
                name="演示客户",
                account=settings.security.DEMO_CUSTOMER_ID,
                agent_id=settings.security.DEMO_AGENT_ID,
            )
        return None

    @staticmethod
    def _validate_account(value: str, label: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", value):
            raise AuthError(f"{label}需为 4-64 位字母、数字或下划线")
        return value

    @staticmethod
    def _validate_name(value: str) -> str:
        value = value.strip()
        if len(value) < 2 or len(value) > 100:
            raise AuthError("姓名长度需为 2-100 个字符")
        return value

    @staticmethod
    def _validate_phone(value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"1\d{10}", value):
            raise AuthError("请输入正确的 11 位手机号")
        return value

    @staticmethod
    def _validate_password(value: str) -> None:
        if (
            len(value) < 8
            or len(value) > 128
            or not re.search(r"[a-z]", value)
            or not re.search(r"[A-Z]", value)
            or not re.search(r"\d", value)
        ):
            raise AuthError("密码需为 8-128 位，并包含大小写字母和数字")

    async def issue_token_pair(self, identity: AuthIdentity) -> dict[str, Any]:
        now = int(time.time())
        access_exp = now + settings.security.ACCESS_TOKEN_MINUTES * 60
        refresh_exp = now + settings.security.REFRESH_TOKEN_DAYS * 86400
        refresh_jti = secrets.token_urlsafe(32)

        access_token = self._encode_jwt({
            "sub": identity.subject,
            "role": identity.role,
            "name": identity.name,
            "account": identity.account,
            "agent_id": identity.agent_id,
            "department": identity.department,
            "employee_no": identity.employee_no,
            "type": "access",
            "jti": secrets.token_urlsafe(24),
            "iat": now,
            "exp": access_exp,
            "iss": self.issuer,
            "aud": self.audience,
        })
        refresh_token = self._encode_jwt({
            "sub": identity.subject,
            "role": identity.role,
            "name": identity.name,
            "account": identity.account,
            "agent_id": identity.agent_id,
            "department": identity.department,
            "employee_no": identity.employee_no,
            "type": "refresh",
            "jti": refresh_jti,
            "iat": now,
            "exp": refresh_exp,
            "iss": self.issuer,
            "aud": self.audience,
        })
        await self._store_refresh(refresh_jti, refresh_exp)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": settings.security.ACCESS_TOKEN_MINUTES * 60,
            "refresh_expires_in": settings.security.REFRESH_TOKEN_DAYS * 86400,
            "user": identity.to_dict(),
        }

    async def refresh(self, refresh_token: str | None) -> dict[str, Any]:
        if not refresh_token:
            raise AuthError("登录状态已失效")
        payload = self.decode_token(refresh_token, expected_type="refresh")
        jti = str(payload.get("jti") or "")
        if not jti or not await self._consume_refresh(jti):
            raise AuthError("登录状态已失效，请重新登录")
        identity = self._identity_from_claims(payload)
        return await self.issue_token_pair(identity)

    async def logout(self, refresh_token: str | None) -> None:
        if not refresh_token:
            return
        try:
            payload = self.decode_token(refresh_token, expected_type="refresh")
            await self._consume_refresh(str(payload.get("jti") or ""))
        except AuthError:
            return

    def decode_token(self, token: str, expected_type: str = "access") -> dict[str, Any]:
        try:
            header_raw, payload_raw, signature_raw = token.split(".", 2)
            signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
            expected_signature = hmac.new(
                self.secret, signing_input, hashlib.sha256
            ).digest()
            provided_signature = self._b64decode(signature_raw)
            if not hmac.compare_digest(expected_signature, provided_signature):
                raise AuthError("Token 签名无效")
            payload = json.loads(self._b64decode(payload_raw))
            now = int(time.time())
            if (
                payload.get("iss") != self.issuer
                or payload.get("aud") != self.audience
                or payload.get("type") != expected_type
                or int(payload.get("exp", 0)) <= now
            ):
                raise AuthError("Token 已失效")
            return payload
        except (ValueError, TypeError, json.JSONDecodeError, AuthError) as e:
            raise AuthError(str(e) if isinstance(e, AuthError) else "Token 无效")

    def identity_from_access(self, token: str) -> AuthIdentity:
        return self._identity_from_claims(
            self.decode_token(token, expected_type="access")
        )

    @staticmethod
    def _identity_from_claims(payload: dict[str, Any]) -> AuthIdentity:
        return AuthIdentity(
            subject=str(payload.get("sub") or ""),
            role=str(payload.get("role") or ""),
            name=str(payload.get("name") or ""),
            account=str(payload.get("account") or ""),
            agent_id=str(payload.get("agent_id") or ""),
            department=str(payload.get("department") or ""),
            employee_no=str(payload.get("employee_no") or ""),
        )

    def _encode_jwt(self, payload: dict[str, Any]) -> str:
        header = self._b64encode(
            json.dumps(
                {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")
            ).encode()
        )
        body = self._b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        )
        signing_input = f"{header}.{body}".encode("ascii")
        signature = self._b64encode(
            hmac.new(self.secret, signing_input, hashlib.sha256).digest()
        )
        return f"{header}.{body}.{signature}"

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    async def _store_refresh(self, jti: str, expires_at: int) -> None:
        ttl = max(1, expires_at - int(time.time()))
        try:
            from src.storage.redis_store import redis_store

            await redis_store.redis.set(
                f"auth:refresh:{jti}", "1", ex=ttl
            )
            return
        except Exception:
            pass
        self._refresh_memory[jti] = time.time() + ttl

    async def _consume_refresh(self, jti: str) -> bool:
        if not jti:
            return False
        try:
            from src.storage.redis_store import redis_store

            deleted = await redis_store.redis.getdel(f"auth:refresh:{jti}")
            return bool(deleted)
        except Exception:
            pass
        expires_at = self._refresh_memory.pop(jti, 0)
        return expires_at > time.time()


auth_service = AuthService()
