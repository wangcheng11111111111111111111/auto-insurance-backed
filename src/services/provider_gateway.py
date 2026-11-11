"""保险公司报价、核保和支付 HTTP 网关。

所有接口均为失败关闭模式：未配置真实 URL、请求失败或响应格式不合法时，
业务层必须停止，不得使用模拟结果继续出单。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx

from config.settings import settings


class ProviderAPIUnavailable(RuntimeError):
    """三方 API 未配置。"""


class ProviderAPIError(RuntimeError):
    """三方 API 调用失败或响应不规范。"""


class CallbackValidationError(RuntimeError):
    """三方回调签名或业务字段不一致。"""


@dataclass
class ProviderResponse:
    data: dict[str, Any]
    raw_response: str
    status_code: int


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProviderGateway:
    """通用 HTTP 保险公司网关。"""

    @staticmethod
    def _configured(url: str | None) -> bool:
        value = (url or "").strip()
        return bool(value and "example.com" not in value)

    def quote_configured(self) -> bool:
        return self._configured(settings.api.QUOTE_API_URL)

    def underwriting_configured(self) -> bool:
        return self._configured(settings.api.UNDERWRITING_API_URL)

    def payment_configured(self) -> bool:
        return self._configured(settings.api.PAYMENT_API_URL)

    async def _post(
        self,
        label: str,
        env_name: str,
        url: str | None,
        api_key: str | None,
        payload: dict[str, Any],
    ) -> ProviderResponse:
        if not self._configured(url):
            raise ProviderAPIUnavailable(
                f"{label}功能API未完成：未配置有效的 {env_name}_API_URL"
            )

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key

        try:
            async with httpx.AsyncClient(
                timeout=settings.api.PROVIDER_API_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderAPIError(f"{label}API调用失败：{exc}") from exc

        raw = response.text
        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderAPIError(
                f"{label}API返回 HTTP {response.status_code}：{raw[:500]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderAPIError(f"{label}API返回的不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise ProviderAPIError(f"{label}API返回格式必须为 JSON 对象")
        return ProviderResponse(
            data=data,
            raw_response=raw,
            status_code=response.status_code,
        )

    async def request_quote(self, payload: dict[str, Any]) -> ProviderResponse:
        return await self._post(
            "报价",
            "QUOTE",
            settings.api.QUOTE_API_URL,
            settings.api.QUOTE_API_KEY,
            payload,
        )

    async def request_underwriting(
        self, payload: dict[str, Any]
    ) -> ProviderResponse:
        return await self._post(
            "核保",
            "UNDERWRITING",
            settings.api.UNDERWRITING_API_URL,
            settings.api.UNDERWRITING_API_KEY,
            payload,
        )

    async def request_payment(self, payload: dict[str, Any]) -> ProviderResponse:
        return await self._post(
            "支付",
            "PAYMENT",
            settings.api.PAYMENT_API_URL,
            settings.api.PAYMENT_API_KEY,
            payload,
        )


provider_gateway = ProviderGateway()
