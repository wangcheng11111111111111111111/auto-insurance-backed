"""浏览器应用层加密：RSA-OAEP 封装 AES-256-GCM 会话密钥。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.settings import settings

logger = logging.getLogger(__name__)


class ClientCryptoError(Exception):
    """浏览器加密请求无效。"""


class ClientCryptoService:
    algorithm = "RSA-OAEP-256+A256GCM"

    def __init__(self):
        self._private_key = None
        self._public_pem = ""
        self._key_id = ""
        self._used_nonces: dict[str, float] = {}

    def _load_key(self) -> None:
        if self._private_key is not None:
            return
        path = Path(settings.security.CLIENT_KEY_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            key = serialization.load_pem_private_key(
                path.read_bytes(), password=None
            )
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            try:
                with path.open("xb") as handle:
                    handle.write(pem)
            except FileExistsError:
                key = serialization.load_pem_private_key(
                    path.read_bytes(), password=None
                )
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ClientCryptoError("客户端加密密钥类型无效")
        self._private_key = key
        self._public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        self._key_id = hashlib.sha256(
            self._public_pem.encode("utf-8")
        ).hexdigest()[:20]

    def public_key_payload(self) -> dict[str, str]:
        self._load_key()
        return {
            "key_id": self._key_id,
            "algorithm": self.algorithm,
            "public_key": self._public_pem,
        }

    def decrypt_json(
        self,
        envelope: dict[str, Any],
        max_bytes: int = 2 * 1024 * 1024,
    ) -> dict[str, Any]:
        self._load_key()
        if str(envelope.get("key_id") or "") != self._key_id:
            raise ClientCryptoError("加密公钥已过期，请刷新页面重试")
        try:
            encrypted_key = _decode(envelope["encrypted_key"])
            iv = _decode(envelope["iv"])
            ciphertext = _decode(envelope["ciphertext"])
            if len(ciphertext) > max_bytes:
                raise ClientCryptoError("加密请求内容过大")
            if len(iv) != 12:
                raise ClientCryptoError("加密请求参数无效")
            aes_key = self._private_key.decrypt(
                encrypted_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            if len(aes_key) != 32:
                raise ClientCryptoError("加密请求密钥无效")
            plaintext = AESGCM(aes_key).decrypt(iv, ciphertext, None)
            payload = json.loads(plaintext.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ClientCryptoError("加密请求内容无效")
            self._validate_anti_replay(payload)
            return payload
        except ClientCryptoError:
            raise
        except Exception as e:
            logger.warning("前端加密请求解密失败: %s", e)
            raise ClientCryptoError("加密请求无法解密") from e

    def _validate_anti_replay(self, payload: dict[str, Any]) -> None:
        try:
            issued_at = int(payload.get("issued_at", 0))
            nonce = str(payload.get("nonce") or "")
        except (TypeError, ValueError):
            raise ClientCryptoError("加密请求时间戳无效")
        if not nonce or len(nonce) < 16:
            raise ClientCryptoError("加密请求随机数无效")
        if abs(int(time.time()) - issued_at) > 120:
            raise ClientCryptoError("加密请求已过期")
        now = time.time()
        self._used_nonces = {
            key: expires
            for key, expires in self._used_nonces.items()
            if expires > now
        }
        if nonce in self._used_nonces:
            raise ClientCryptoError("加密请求不能重复使用")
        self._used_nonces[nonce] = now + 300


def _decode(value: Any) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except Exception as e:
        raise ClientCryptoError("加密请求编码无效") from e
client_crypto_service = ClientCryptoService()
