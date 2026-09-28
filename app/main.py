# app/main.py
"""
Core FastAPI server: mock storefront + checkout tracking hooks.
All DB operations are wrapped defensively - no unhandled 500s on bad input.
"""

from decimal import Decimal, InvalidOperation
import logging
from typing import Literal, Optional

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import get_db, Invoice, PaymentStatus, AgentAuditTrail, BatchRun, Channel, ActionTaken

logger = logging.getLogger("revenue_recovery.api")

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
    customer_phone: Optional[str] = None
    item_name: str
    amount: Decimal
    contact_consent: bool = False

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if not v.is_finite() or v <= 0 or v > Decimal("9999999999.99"):
            raise ValueError("amount must be a finite value greater than 0 within the supported range")
        return v

    @field_validator("customer_name", "item_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field cannot be blank")
        return v.strip()

    @field_validator("customer_phone")
    @classmethod
    def normalize_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        return v.strip()[:32]


class CheckoutStatusRequest(BaseModel):
    cart_id: str


class RecoveryReasonRequest(BaseModel):
    reason: Literal["payment_issue", "shipping_cost", "price_comparison", "forgot", "other"]


class SimulateRecoveryRequest(BaseModel):
    cart_id: str
    accepted_offer: bool


class InvoiceResponse(BaseModel):
    id: str
    customer_name: str
    customer_email: str
    item_name: str
    amount: Decimal
    payment_status: str
    conversion_value_recovered: Decimal
    recovered_by_agent: bool

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
    except SQLAlchemyError:
        # Never let a dashboard read crash the storefront - degrade gracefully
        invoices = []
        logger.warning("Failed to load invoices for storefront dashboard")

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
            customer_phone=payload.customer_phone,
            item_name=payload.item_name,
            amount=payload.amount,
            contact_consent=payload.contact_consent,
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

    except SQLAlchemyError:
        db.rollback()
        logger.exception("Database error while creating checkout")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while creating checkout.",
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
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Database error while simulating payment failure")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while updating checkout.",
        )


# ---------------------------------------------------------------------------
# 4. POST /api/checkout/simulate-success - the critical Stopping Rule trigger
# ---------------------------------------------------------------------------

@app.post("/api/checkout/simulate-success", response_model=ApiResponse)
async def simulate_success(payload: CheckoutStatusRequest, db: Session = Depends(get_db)):
    try:
        invoice = _get_invoice_or_404(db, payload.cart_id)

        if invoice.payment_status == PaymentStatus.PAID:
            return ApiResponse(
                success=False,
                message="This checkout is already complete.",
                data=InvoiceResponse.model_validate(invoice),
            )

        previous_status = invoice.payment_status
        invoice.payment_status = PaymentStatus.PAID

        # If the agent had already recovered value on prior attempts, keep it;
        # otherwise this is a fresh, unassisted recovery - worker won't
        # touch this cart again once payment_status == PAID (see
        # Invoice.is_recoverable() in database.py).
        if invoice.recovered_by_agent:
            invoice.conversion_value_recovered = invoice.amount
            action = ActionTaken.RECOVERED_BY_AGENT
            description = "Agent-assisted purchase confirmed in demo."
        else:
            action = ActionTaken.ORGANIC_PURCHASE
            description = "Organic purchase confirmed; not attributed to the agent."
        db.add(AgentAuditTrail(
            cart_id=invoice.id,
            step_number=0,
            channel_used=Channel.ON_SITE,
            content_sent=description,
            action_taken=action,
        ))

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
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Database error while simulating purchase completion")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while updating checkout.",
        )

# ---------------------------------------------------------------------------
# Dashboard data - polled by the frontend for live metrics
# ---------------------------------------------------------------------------

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
        batch_runs = db.query(BatchRun).order_by(BatchRun.started_at.desc()).limit(10).all()

        all_invoices = db.query(Invoice).all()
        total_recovered = sum(
            (inv.amount for inv in all_invoices if inv.recovered_by_agent), Decimal("0.00")
        )
        total_organic = sum(
            (inv.amount for inv in all_invoices
             if inv.payment_status == PaymentStatus.PAID and not inv.recovered_by_agent),
            Decimal("0.00"),
        )
        open_invoices = [inv for inv in all_invoices if inv.payment_status != PaymentStatus.PAID]
        total_at_risk = sum((inv.amount for inv in open_invoices), Decimal("0.00"))

        return {
            "success": True,
            "totals": {
                "total_recovered": str(total_recovered),
                "total_organic": str(total_organic),
                "total_at_risk": str(total_at_risk),
                "open_cases": len(open_invoices),
            },
            "invoices": [
                {
                    "id": inv.id,
                    "customer_name": inv.customer_name,
                    "item_name": inv.item_name,
                    "amount": str(inv.amount),
                    "payment_status": inv.payment_status.value,
                    "conversion_value_recovered": str(inv.conversion_value_recovered),
                    "recovered_by_agent": inv.recovered_by_agent,
                    "recovery_reason": inv.recovery_reason,
                    "contact_consent": inv.contact_consent,
                    "opt_out": inv.opt_out,
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
            "batch_runs": [
                {
                    "id": batch.id,
                    "status": batch.status,
                    "started_at": batch.started_at.isoformat(),
                    "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
                    "cases_scanned": batch.total_invoices_scanned,
                    "at_risk": str(batch.total_at_risk_amount),
                    "recovered": str(batch.total_recovered_amount),
                    "stopped": batch.total_stopped_count,
                    "escalated": batch.total_escalated_count,
                }
                for batch in batch_runs
            ],
        }
    except SQLAlchemyError:
        logger.exception("Dashboard query failed")
        raise HTTPException(status_code=500, detail="Dashboard data is temporarily unavailable.")

# ---------------------------------------------------------------------------
# Health check - useful for confirming the server + DB are both alive
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check(db: Session = Depends(get_db)):
    try:
        db.query(Invoice).limit(1).all()
        return {"status": "ok", "db": "connected"}
    except SQLAlchemyError:
        logger.exception("Database health check failed")
        return {"status": "degraded", "db": "unavailable"}


# ---------------------------------------------------------------------------
# Customer-facing recovery: capture an optional reason and return a bounded,
# deterministic suggestion. No discount or competitor price is invented.
# ---------------------------------------------------------------------------

INTERVENTIONS = {
    "payment_issue": "Try another payment method or retry securely when you’re ready.",
    "shipping_cost": "Review the delivery options and final total before deciding.",
    "price_comparison": "Compare the item and final delivered price at your own pace. We won’t claim a price match unless the store has verified one.",
    "forgot": "Your cart is saved. Continue checkout whenever it suits you.",
    "other": "Would you like help? Contact the store team and they can look into it.",
}


@app.post("/api/carts/{cart_id}/reason")
async def capture_recovery_reason(
    cart_id: str, payload: RecoveryReasonRequest, db: Session = Depends(get_db)
):
    invoice = _get_invoice_or_404(db, cart_id)
    if invoice.payment_status == PaymentStatus.PAID:
        raise HTTPException(status_code=409, detail="This order is already complete.")
    invoice.recovery_reason = payload.reason
    db.add(AgentAuditTrail(
        cart_id=invoice.id,
        step_number=0,
        channel_used=Channel.ON_SITE,
        content_sent=f"Customer selected reason: {payload.reason}. Policy suggestion displayed.",
        action_taken=ActionTaken.CAPTURED_REASON,
    ))
    db.commit()
    return {
        "success": True,
        "reason": payload.reason,
        "suggestion": INTERVENTIONS[payload.reason],
        "item_name": invoice.item_name,
    }


@app.post("/api/carts/{cart_id}/opt-out")
async def opt_out_of_recovery(cart_id: str, db: Session = Depends(get_db)):
    invoice = _get_invoice_or_404(db, cart_id)
    if not invoice.opt_out:
        invoice.opt_out = True
        invoice.is_suppressed = True
        db.add(AgentAuditTrail(
            cart_id=invoice.id,
            step_number=invoice.attempt_count,
            channel_used=Channel.ON_SITE,
            content_sent="Customer opted out. Future recovery messages suppressed.",
            action_taken=ActionTaken.SKIPPED_OPT_OUT,
        ))
        db.commit()
    return {"success": True, "message": "Recovery messages are now suppressed for this checkout."}


@app.post("/api/checkout/simulate-recovery", response_model=ApiResponse)
async def simulate_agent_recovery(payload: SimulateRecoveryRequest, db: Session = Depends(get_db)):
    """Demo-only payment confirmation after accepting an agent intervention."""
    if not payload.accepted_offer:
        raise HTTPException(status_code=400, detail="An accepted recovery action is required.")
    invoice = _get_invoice_or_404(db, payload.cart_id)
    if invoice.payment_status == PaymentStatus.PAID:
        return ApiResponse(success=False, message="This order is already complete.", data=InvoiceResponse.model_validate(invoice))
    if not invoice.recovery_reason:
        raise HTTPException(status_code=409, detail="Choose and record a recovery reason before accepting the demo intervention.")
    invoice.payment_status = PaymentStatus.PAID
    invoice.recovered_by_agent = True
    invoice.conversion_value_recovered = invoice.amount
    db.add(AgentAuditTrail(
        cart_id=invoice.id,
        step_number=0,
        channel_used=Channel.ON_SITE,
        content_sent="Customer accepted a recovery suggestion and completed the demo purchase.",
        action_taken=ActionTaken.RECOVERED_BY_AGENT,
    ))
    db.commit()
    db.refresh(invoice)
    return ApiResponse(
        success=True,
        message="Demo recovery recorded after the customer accepted an intervention.",
        data=InvoiceResponse.model_validate(invoice),
    )
