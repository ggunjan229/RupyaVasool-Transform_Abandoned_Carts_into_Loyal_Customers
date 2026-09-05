# app/main.py
"""
Core FastAPI server: mock storefront + checkout tracking hooks.
All DB operations are wrapped defensively - no unhandled 500s on bad input.
"""

from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import get_db, Invoice, PaymentStatus, AgentAuditTrail

app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)

templates = Jinja2Templates(directory="app/templates")

# Static files are optional - only mount if the folder exists, so a missing
# app/static dir never crashes startup.
import os
if os.path.isdir("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ---------------------------------------------------------------------------
# Pydantic schemas (request/response contracts)
# ---------------------------------------------------------------------------

class CheckoutInitiateRequest(BaseModel):
    customer_name: str
    customer_email: EmailStr
    item_name: str
    amount: Decimal

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be greater than 0")
        return v

    @field_validator("customer_name", "item_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field cannot be blank")
        return v.strip()


class CheckoutStatusRequest(BaseModel):
    cart_id: str


class InvoiceResponse(BaseModel):
    id: str
    customer_name: str
    customer_email: str
    item_name: str
    amount: Decimal
    payment_status: str
    conversion_value_recovered: Decimal

    class Config:
        from_attributes = True


class ApiResponse(BaseModel):
    success: bool
    message: str
    data: Optional[InvoiceResponse] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_invoice_or_404(db: Session, cart_id: str) -> Invoice:
    invoice = db.get(Invoice, cart_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cart '{cart_id}' not found.",
        )
    return invoice


# ---------------------------------------------------------------------------
# 1. Root - mock storefront
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, db: Session = Depends(get_db)):
    try:
        invoices = (
            db.query(Invoice)
            .order_by(Invoice.created_at.desc())
            .limit(50)
            .all()
        )
    except SQLAlchemyError as e:
        # Never let a dashboard read crash the storefront - degrade gracefully
        invoices = []
        print(f"[WARN] Failed to load invoices for dashboard: {e}")

    return templates.TemplateResponse(
    request,
    "index.html",
    {"invoices": invoices, "app_name": settings.APP_NAME},
    )


# ---------------------------------------------------------------------------
# 2. POST /api/checkout/initiate - create a PENDING invoice
# ---------------------------------------------------------------------------

@app.post("/api/checkout/initiate", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def initiate_checkout(payload: CheckoutInitiateRequest, db: Session = Depends(get_db)):
    try:
        invoice = Invoice(
            customer_name=payload.customer_name,
            customer_email=payload.customer_email,
            item_name=payload.item_name,
            amount=payload.amount,
            payment_status=PaymentStatus.PENDING,
        )
        db.add(invoice)
        db.commit()
        db.refresh(invoice)

        return ApiResponse(
            success=True,
            message=f"Checkout initiated for cart {invoice.id}.",
            data=InvoiceResponse.model_validate(invoice),
        )

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error while creating checkout: {e}",
        )
    except (InvalidOperation, ValueError) as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid checkout payload: {e}",
        )


# ---------------------------------------------------------------------------
# 3. POST /api/checkout/simulate-failure - trigger event for recovery agent
# ---------------------------------------------------------------------------

@app.post("/api/checkout/simulate-failure", response_model=ApiResponse)
async def simulate_failure(payload: CheckoutStatusRequest, db: Session = Depends(get_db)):
    try:
        invoice = _get_invoice_or_404(db, payload.cart_id)

        # Stopping-rule guard: don't overwrite a terminal PAID state
        if invoice.payment_status == PaymentStatus.PAID:
            return ApiResponse(
                success=False,
                message=f"Cart {invoice.id} is already PAID - failure event ignored.",
                data=InvoiceResponse.model_validate(invoice),
            )

        invoice.payment_status = PaymentStatus.FAILED
        db.commit()
        db.refresh(invoice)

        return ApiResponse(
            success=True,
            message=f"Cart {invoice.id} marked FAILED. Recovery agent will pick this up.",
            data=InvoiceResponse.model_validate(invoice),
        )

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error while simulating failure: {e}",
        )


# ---------------------------------------------------------------------------
# 4. POST /api/checkout/simulate-success - the critical Stopping Rule trigger
# ---------------------------------------------------------------------------

@app.post("/api/checkout/simulate-success", response_model=ApiResponse)
async def simulate_success(payload: CheckoutStatusRequest, db: Session = Depends(get_db)):
    try:
        invoice = _get_invoice_or_404(db, payload.cart_id)

        previous_status = invoice.payment_status
        invoice.payment_status = PaymentStatus.PAID

        # If the agent had already recovered value on prior attempts, keep it;
        # otherwise this is a fresh, unassisted recovery - worker won't
        # touch this cart again once payment_status == PAID (see
        # Invoice.is_recoverable() in database.py).
        if invoice.conversion_value_recovered == 0:
            invoice.conversion_value_recovered = invoice.amount

        db.commit()
        db.refresh(invoice)

        return ApiResponse(
            success=True,
            message=(
                f"Cart {invoice.id} marked PAID (was {previous_status}). "
                f"Stopping rule active - agent will halt on next check."
            ),
            data=InvoiceResponse.model_validate(invoice),
        )

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error while simulating success: {e}",
        )

# ---------------------------------------------------------------------------
# Dashboard data - polled by the frontend for live metrics
# ---------------------------------------------------------------------------

from app.database import AgentAuditTrail  # add to your existing database import line instead if you prefer

@app.get("/api/dashboard")
async def dashboard_data(db: Session = Depends(get_db)):
    try:
        invoices = db.query(Invoice).order_by(Invoice.updated_at.desc()).limit(100).all()
        audit_logs = (
            db.query(AgentAuditTrail)
            .order_by(AgentAuditTrail.timestamp.desc())
            .limit(50)
            .all()
        )

        total_recovered = sum((inv.conversion_value_recovered for inv in invoices), Decimal("0.00"))
        total_lost = sum(
            (inv.amount for inv in invoices
             if inv.payment_status == PaymentStatus.FAILED and inv.conversion_value_recovered == 0),
            Decimal("0.00"),
        )

        return {
            "success": True,
            "totals": {
                "total_recovered": str(total_recovered),
                "total_lost": str(total_lost),
            },
            "invoices": [
                {
                    "id": inv.id,
                    "customer_name": inv.customer_name,
                    "item_name": inv.item_name,
                    "amount": str(inv.amount),
                    "payment_status": inv.payment_status.value,
                    "conversion_value_recovered": str(inv.conversion_value_recovered),
                }
                for inv in invoices
            ],
            "audit_logs": [
                {
                    "id": log.id,
                    "cart_id": log.cart_id,
                    "step_number": log.step_number,
                    "channel_used": log.channel_used.value,
                    "content_sent": log.content_sent,
                    "action_taken": log.action_taken.value,
                    "timestamp": log.timestamp.isoformat(),
                }
                for log in audit_logs
            ],
        }
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"Dashboard query failed: {e}")

# ---------------------------------------------------------------------------
# Health check - useful for confirming the server + DB are both alive
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check(db: Session = Depends(get_db)):
    try:
        db.query(Invoice).limit(1).all()
        return {"status": "ok", "db": "connected"}
    except SQLAlchemyError as e:
        return {"status": "degraded", "db": f"error: {e}"}