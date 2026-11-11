"""浏览器应用层加密测试。"""
import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.settings import settings
from src.services.client_crypto import ClientCryptoError, ClientCryptoService


def make_envelope(service: ClientCryptoService, payload: dict, nonce: str | None = None):
    public = service.public_key_payload()
    public_key = serialization.load_pem_public_key(public["public_key"].encode())
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = secrets.token_bytes(12)
    protected = {
        **payload,
        "issued_at": int(time.time()),
        "nonce": nonce or secrets.token_urlsafe(18),
    }
    ciphertext = AESGCM(aes_key).encrypt(
        iv, json.dumps(protected).encode(), None
    )
    encrypted_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "key_id": public["key_id"],
        "encrypted_key": base64.b64encode(encrypted_key).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


def test_client_envelope_roundtrip_and_replay_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings.security,
        "CLIENT_KEY_PATH",
        str(tmp_path / "client-key.pem"),
    )
    service = ClientCryptoService()
    nonce = secrets.token_urlsafe(18)
    envelope = make_envelope(
        service,
        {"account": "customer_001", "password": "secret", "role": "customer"},
        nonce,
    )

    decrypted = service.decrypt_json(envelope)
    assert decrypted["account"] == "customer_001"

    with pytest.raises(ClientCryptoError):
        service.decrypt_json(envelope)


def test_client_envelope_rejects_expired_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings.security,
        "CLIENT_KEY_PATH",
        str(tmp_path / "client-key.pem"),
    )
    service = ClientCryptoService()
    envelope = make_envelope(
        service,
        {"message": "hello"},
        secrets.token_urlsafe(18),
    )

    public = service.public_key_payload()
    public_key = serialization.load_pem_public_key(public["public_key"].encode())
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = secrets.token_bytes(12)
    expired = {
        "message": "expired",
        "issued_at": int(time.time()) - 600,
        "nonce": secrets.token_urlsafe(18),
    }
    ciphertext = AESGCM(aes_key).encrypt(iv, json.dumps(expired).encode(), None)
    expired_envelope = {
        "key_id": public["key_id"],
        "encrypted_key": base64.b64encode(
            public_key.encrypt(
                aes_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
        ).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }

    with pytest.raises(ClientCryptoError):
        service.decrypt_json(expired_envelope)
