"""保险公司真实报价服务。"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.api.schemas import (
    InsuranceCompany,
    InsuranceItem,
    InsurancePlan,
    Quotation,
    Session,
    now_utc,
)
from src.services.provider_gateway import (
    ProviderAPIError,
    payload_digest,
    provider_gateway,
)


class QuoteService:
    """调用保险公司报价 API，不接受本地模拟报价。"""

    @staticmethod
    def _parse_decimal(
        value: Any, field: str, allow_zero: bool = False
    ) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ProviderAPIError(f"报价API返回的{field}不是有效金额") from exc
        if amount < 0 or (amount == 0 and not allow_zero):
            raise ProviderAPIError(
                f"报价API返回的{field}必须{'大于等于' if allow_zero else '大于'} 0"
            )
        return amount.quantize(Decimal("0.01"))

    @staticmethod
    def _parse_valid_until(value: Any) -> datetime:
        if not value:
            raise ProviderAPIError("报价API缺少 valid_until")
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderAPIError("报价API返回的 valid_until 格式不正确") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed <= now_utc():
            raise ProviderAPIError("报价API返回的报价单已过期")
        return parsed

    async def create_quote(
        self,
        session: Session,
        effective_date: date,
        expire_date: date,
    ) -> Quotation:
        if not session.insurance_plan or not session.vehicle_info:
            raise ProviderAPIError("缺少投保方案或车辆信息，无法发起报价")

        payload = {
            "session_id": session.session_id,
            "company_code": session.insurance_plan.company.code
            if session.insurance_plan.company
            else "",
            "vehicle": session.vehicle_info.to_dict(),
            "applicant": (
                session.applicant_id_card.to_dict()
                if session.applicant_id_card
                else None
            ),
            "insured": (
                session.insured_id_card.to_dict()
                if session.insured_id_card
                else None
            ),
            "plan": session.insurance_plan.to_dict(),
            "effective_date": effective_date.isoformat(),
            "expire_date": expire_date.isoformat(),
        }
        provider_response = await provider_gateway.request_quote(payload)
        data = provider_response.data

        provider_reference = str(data.get("provider_reference") or "").strip()
        version = str(data.get("version") or "").strip()
        if not provider_reference:
            raise ProviderAPIError("报价API缺少 provider_reference")
        if not version:
            raise ProviderAPIError("报价API缺少 version")
        valid_until = self._parse_valid_until(data.get("valid_until"))

        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ProviderAPIError("报价API返回的险种明细为空")
        items: list[InsuranceItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ProviderAPIError("报价API险种明细格式不正确")
            code = str(raw_item.get("code") or "").strip()
            name = str(raw_item.get("name") or "").strip()
            if not code or not name:
                raise ProviderAPIError("报价API险种缺少 code 或 name")
            items.append(
                InsuranceItem(
                    name=name,
                    code=code,
                    coverage_amount=self._parse_decimal(
                        raw_item.get("coverage_amount"), "coverage_amount"
                    ),
                    premium=self._parse_decimal(
                        raw_item.get("premium"), "premium"
                    ),
                    deductible=self._parse_decimal(
                        raw_item.get("deductible", "0"),
                        "deductible",
                        allow_zero=True,
                    ),
                )
            )

        total_premium = self._parse_decimal(
            data.get("total_premium"), "total_premium"
        )
        company_data = data.get("company")
        if isinstance(company_data, dict) and company_data.get("name"):
            company = InsuranceCompany(
                name=str(company_data["name"]),
                code=str(company_data.get("code") or ""),
                available_items=list(company_data.get("available_items") or []),
                discount_rate=Decimal(
                    str(company_data.get("discount_rate", "1.0"))
                ),
            )
        else:
            company = session.insurance_plan.company

        plan = InsurancePlan(
            company=company,
            items=items,
            total_premium=total_premium,
            discount_rate=Decimal(
                str(data.get("discount_rate", session.insurance_plan.discount_rate))
            ),
        )
        rate_source = str(data.get("rate_source") or "").strip()
        if not rate_source:
            rate_source = f"{company.name if company else '保险公司'}官方报价API"

        return Quotation(
            quotation_id=str(uuid.uuid4()),
            version=version,
            plan=plan,
            vehicle=session.vehicle_info,
            applicant=session.applicant_id_card,
            insured=session.insured_id_card,
            effective_date=effective_date,
            expire_date=expire_date,
            created_at=now_utc(),
            valid_until=valid_until,
            rate_source=rate_source,
            provider_reference=provider_reference,
            request_digest=payload_digest(payload),
            raw_response=provider_response.raw_response,
            is_estimate=False,
            status="quoted",
        )


quote_service = QuoteService()
