"""双 Token 登录服务测试。"""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.auth_service import AuthError, AuthIdentity, AuthService


def test_password_hash_roundtrip():
    encoded = AuthService.hash_password("StrongPassword!123")

    assert encoded.startswith("pbkdf2_sha256$")
    assert AuthService.verify_password("StrongPassword!123", encoded)
    assert not AuthService.verify_password("wrong-password", encoded)


@pytest.mark.asyncio
async def test_demo_customer_login_and_token_refresh_rotation():
    service = AuthService()
    with patch.object(
        service,
        "_database_customer",
        new=AsyncMock(return_value=None),
    ):
        identity = await service.authenticate(
            "customer_001", "Customer@123", "customer"
        )

    assert identity.role == "customer"
    assert identity.agent_id == "agent_001"

    pair = await service.issue_token_pair(identity)
    access = service.decode_token(pair["access_token"], "access")
    refresh = service.decode_token(pair["refresh_token"], "refresh")
    assert access["sub"] == "customer_001"
    assert refresh["type"] == "refresh"

    rotated = await service.refresh(pair["refresh_token"])
    assert rotated["access_token"]
    assert rotated["refresh_token"] != pair["refresh_token"]

    with pytest.raises(AuthError):
        await service.refresh(pair["refresh_token"])


def test_identity_from_access_token_preserves_agent_boundary():
    service = AuthService()
    identity = AuthIdentity(
        subject="customer_001",
        role="customer",
        name="演示客户",
        account="customer_001",
        agent_id="agent_001",
    )
    token = service._encode_jwt({
        "sub": identity.subject,
        "role": identity.role,
        "name": identity.name,
        "account": identity.account,
        "agent_id": identity.agent_id,
        "department": "",
        "employee_no": "",
        "type": "access",
        "jti": "test",
        "iat": 1,
        "exp": 9999999999,
        "iss": service.issuer,
        "aud": service.audience,
    })

    restored = service.identity_from_access(token)

    assert restored.subject == "customer_001"
    assert restored.agent_id == "agent_001"
