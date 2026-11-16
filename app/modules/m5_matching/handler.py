from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.web import page
from . import ledger_service as service
from .service import demo

router = APIRouter()


class GrIn(BaseModel):
    gr_id: str
    sku: str
    tax_rate: str
    amount: str


class InvoiceIn(BaseModel):
    invoice_id: str
    sku: str
    tax_rate: str
    amount: str


class MatchIn(BaseModel):
    gr_id: str
    invoice_id: str
    amount: str


class AutoMatchIn(BaseModel):
    invoice_id: str


class PaymentIn(BaseModel):
    link_id: str
    amount: str
    idempotency_key: str | None = None


class RevokeIn(BaseModel):
    gr_id: str
    idempotency_key: str | None = None
    expected_version: int | None = None
    failure_point: str | None = None


class CreditNoteIn(BaseModel):
    recovery_id: str
    amount: str
    idempotency_key: str


@router.get("/matching", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m5.html", "多对多匹配 · 撤销补偿", "/api/matching/ledger", {})


@router.post("/api/matches/solve")
def execute():
    """保留原组合匹配样例接口（向后兼容既有运行与测试）。"""
    return demo()


@router.get("/api/matching/ledger")
def get_ledger():
    return service.ledger()


@router.post("/api/matching/seed")
def seed():
    return service.seed_demo()


@router.post("/api/matching/goods-receipts")
def create_gr(payload: GrIn):
    return service.register_gr(
        payload.gr_id, payload.sku, payload.tax_rate, payload.amount
    )


@router.post("/api/matching/invoices")
def create_invoice(payload: InvoiceIn):
    return service.register_invoice(
        payload.invoice_id, payload.sku, payload.tax_rate, payload.amount
    )


@router.post("/api/matching/matches")
def create_match(payload: MatchIn):
    return service.register_match(
        payload.gr_id, payload.invoice_id, payload.amount
    )


@router.post("/api/matching/auto-match")
def create_auto_match(payload: AutoMatchIn):
    return service.auto_match(payload.invoice_id)


@router.post("/api/matching/payments")
def create_payment(payload: PaymentIn):
    return service.register_payment(
        payload.link_id, payload.amount, payload.idempotency_key
    )


@router.post("/api/matching/revocations")
def create_revocation(payload: RevokeIn):
    return service.revoke_gr(
        payload.gr_id,
        payload.idempotency_key,
        payload.expected_version,
        payload.failure_point,
    )


@router.post("/api/matching/revocations/resume")
def resume_revocations():
    return service.resume_revocations()


@router.post("/api/matching/credit-notes")
def create_credit_note(payload: CreditNoteIn):
    return service.register_credit_note(
        payload.recovery_id, payload.amount, payload.idempotency_key
    )
