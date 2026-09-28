# run_worker.py
"""
Primary compliance daemon: scans active cases every 30s, enforces stopping
rules, re-validates payment status immediately before any dispatch, and
executes the escalation ladder via app.agent.brain + app.agent.tools.

Every dispatch and policy stop writes an AgentAuditTrail row; time-based
deferrals stay out of the log to avoid poll noise. Every run writes one
BatchRun row with measured totals.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from sqlalchemy.orm import Session

from app.config import settings
from app.database import (
    SessionLocal,
    Invoice,
    AgentAuditTrail,
    BatchRun,
    PaymentStatus,
    Channel,
    ActionTaken,
)
from app.agent.brain import (
    generate_recovery_message,
    get_fallback_message,
    GenerationFailedError,
    MissingAPIKeyError,
)
from app.agent.tools import send_recovery_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worker")

# Demo-compressed cadence: the local demo uses seconds; a production policy
# should use a customer-local schedule with a measured and approved cadence.
STEP_INTERVAL_SECONDS = settings.STEP_INTERVAL_SECONDS

SUBJECT_BY_STEP = {
    1: "A note about your checkout",
    2: "Need a hand with checkout?",
    3: "Final checkout reminder",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _failure_reason_for(invoice: Invoice) -> str:
    if invoice.recovery_reason:
        return {
            "payment_issue": "customer reported a payment issue",
            "shipping_cost": "customer is reviewing delivery cost",
            "price_comparison": "customer is comparing prices",
            "forgot": "customer got distracted before checkout",
            "other": "customer asked for help with checkout",
        }.get(invoice.recovery_reason, "checkout was not completed")
    if invoice.payment_status == PaymentStatus.FAILED:
        return "payment declined / bank timeout during checkout"
    return "checkout was started but never completed (cart abandoned)"


def _seconds_since_update(invoice: Invoice) -> float:
    now = datetime.now(timezone.utc)
    updated = invoice.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now - updated).total_seconds()


def _in_quiet_hours() -> bool:
    local_now = datetime.now(ZoneInfo(settings.DND_TIMEZONE))
    hour = local_now.hour
    start, end = settings.DND_HOURS_START, settings.DND_HOURS_END
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _write_audit(db: Session, invoice: Invoice, step_number: int,
                  channel: Channel, content: str, action: ActionTaken) -> None:
    db.add(AgentAuditTrail(
        cart_id=invoice.id,
        step_number=step_number,
        channel_used=channel,
        content_sent=content,
        action_taken=action,
    ))


# ---------------------------------------------------------------------------
# Core batch job
# ---------------------------------------------------------------------------

def process_batch() -> None:
    db: Session = SessionLocal()
    batch = BatchRun(status="RUNNING")
    db.add(batch)
    db.commit()
    db.refresh(batch)
    previous_batch = (
        db.query(BatchRun.completed_at)
        .filter(BatchRun.status == "COMPLETED", BatchRun.completed_at.isnot(None))
        .order_by(BatchRun.completed_at.desc())
        .first()
    )
    previous_completed_at = previous_batch[0] if previous_batch else None

    scanned = 0
    at_risk_amount = Decimal("0.00")
    recovered_amount = Decimal("0.00")
    stopped_count = 0
    escalated_count = 0

    try:
        # 1. SCAN: FAILED or PENDING carts, not already suppressed
        candidates = (
            db.query(Invoice)
            .filter(Invoice.payment_status.in_([PaymentStatus.FAILED, PaymentStatus.PENDING]))
            .filter(Invoice.is_suppressed == False)  # noqa: E712
            .order_by(Invoice.created_at.asc())
            .limit(settings.BATCH_SIZE)
            .all()
        )

        logger.info(f"[batch {batch.id[:8]}] scanning {len(candidates)} active case(s)...")

        for invoice in candidates:
            scanned += 1
            at_risk_amount += invoice.amount

            try:
                db.refresh(invoice)  # pick up any change from a concurrent webhook

                # 2. STOPPING RULE - opt-out
                if invoice.opt_out:
                    invoice.is_suppressed = True
                    _write_audit(
                        db, invoice, invoice.attempt_count, Channel.EMAIL,
                        "N/A - customer opted out of communications.",
                        ActionTaken.SKIPPED_OPT_OUT,
                    )
                    db.commit()
                    stopped_count += 1
                    continue

                # Give an unfinished checkout time to complete naturally
                # before classifying a pending case as abandoned.
                if (invoice.payment_status == PaymentStatus.PENDING
                        and invoice.attempt_count == 0
                        and _seconds_since_update(invoice) < settings.ABANDONMENT_GRACE_SECONDS):
                    continue

                # Never send recovery messages without explicit permission.
                # Store one audit event, rather than repeating it every poll.
                if not invoice.contact_consent:
                    already_logged = db.query(AgentAuditTrail.id).filter(
                        AgentAuditTrail.cart_id == invoice.id,
                        AgentAuditTrail.action_taken == ActionTaken.SKIPPED_NO_CONSENT,
                    ).first()
                    if not already_logged:
                        _write_audit(
                            db, invoice, invoice.attempt_count, Channel.EMAIL,
                            "N/A - no recovery-message consent was recorded; no message sent.",
                            ActionTaken.SKIPPED_NO_CONSENT,
                        )
                        db.commit()
                    continue

                # 2. STOPPING RULE - max attempts exceeded
                if invoice.attempt_count >= settings.MAX_RECOVERY_STAGE:
                    invoice.max_attempts_reached = True
                    invoice.is_suppressed = True
                    _write_audit(
                        db, invoice, invoice.attempt_count, Channel.EMAIL,
                        "N/A - max recovery attempts reached; escalated to human review.",
                        ActionTaken.ESCALATED_HUMAN,
                    )
                    db.commit()
                    stopped_count += 1
                    escalated_count += 1
                    continue

                # 3. CONTINUOUS VALIDATION - re-verify status immediately
                #    before any dispatch and stop if a payment arrived.
                if invoice.payment_status == PaymentStatus.PAID:
                    if invoice.recovered_by_agent and invoice.conversion_value_recovered == 0:
                        invoice.conversion_value_recovered = invoice.amount
                    if invoice.recovered_by_agent:
                        recovered_amount += invoice.conversion_value_recovered
                    _write_audit(
                        db, invoice, invoice.attempt_count, Channel.EMAIL,
                        "N/A - payment confirmed PAID prior to dispatch; agent halted.",
                        ActionTaken.HALTED_PAID,
                    )
                    db.commit()
                    stopped_count += 1
                    continue  # move to next invoice; do NOT process further

                # Failed payments can be acted on promptly; steps 2/3 wait
                # for the interval to elapse since the last successful send.
                if invoice.attempt_count > 0 and _seconds_since_update(invoice) < STEP_INTERVAL_SECONDS:
                    continue  # not due yet - skip silently, no audit noise

                if _in_quiet_hours():
                    continue  # retry on the next poll during the local daytime window

                # 4. PROCESS: determine step, generate copy, dispatch, log
                step_number = min(invoice.attempt_count + 1, 3)
                # The storefront collects email consent only. Other channels
                # stay disabled until they have their own consent flow.
                channel = Channel.EMAIL
                failure_reason = _failure_reason_for(invoice)

                try:
                    message_body = generate_recovery_message(
                        customer_name=invoice.customer_name,
                        item_name=invoice.item_name,
                        amount=invoice.amount,
                        failure_reason=failure_reason,
                        step_number=step_number,
                    )
                except (GenerationFailedError, MissingAPIKeyError) as e:
                    logger.warning(f"[worker] Gemini unavailable ({e}) - using fallback template.")
                    message_body = get_fallback_message(
                        invoice.item_name, invoice.customer_name, step_number
                    )

                # Generation can take time. Reload and re-check the payment,
                # consent, suppression, and contact window immediately before
                # dispatch so a concurrent payment or opt-out wins.
                db.refresh(invoice)
                if invoice.payment_status == PaymentStatus.PAID:
                    if invoice.recovered_by_agent and invoice.conversion_value_recovered == 0:
                        invoice.conversion_value_recovered = invoice.amount
                    _write_audit(
                        db, invoice, invoice.attempt_count, channel,
                        "N/A - payment completed during message generation; dispatch cancelled.",
                        ActionTaken.HALTED_PAID,
                    )
                    db.commit()
                    stopped_count += 1
                    continue
                if invoice.opt_out or invoice.is_suppressed:
                    invoice.is_suppressed = True
                    _write_audit(
                        db, invoice, invoice.attempt_count, channel,
                        "N/A - recovery contact was suppressed before dispatch.",
                        ActionTaken.SKIPPED_OPT_OUT if invoice.opt_out else ActionTaken.SKIPPED_SUPPRESSED,
                    )
                    db.commit()
                    stopped_count += 1
                    continue
                if not invoice.contact_consent:
                    already_logged = db.query(AgentAuditTrail.id).filter(
                        AgentAuditTrail.cart_id == invoice.id,
                        AgentAuditTrail.action_taken == ActionTaken.SKIPPED_NO_CONSENT,
                    ).first()
                    if not already_logged:
                        _write_audit(
                            db, invoice, invoice.attempt_count, Channel.EMAIL,
                            "N/A - recovery-message consent was revoked before dispatch.",
                            ActionTaken.SKIPPED_NO_CONSENT,
                        )
                        db.commit()
                    continue
                if _in_quiet_hours():
                    continue

                result = send_recovery_email(
                    email=invoice.customer_email,
                    subject=SUBJECT_BY_STEP[step_number],
                    body=message_body,
                )

                action = ActionTaken.SENT if result["status"] == "delivered" else ActionTaken.DISPATCH_FAILED
                _write_audit(db, invoice, step_number, channel, message_body, action)

                # Only advance the step counter on a successful send -
                # a failed dispatch (e.g. malformed contact) should not
                # silently burn an escalation attempt.
                if result["status"] == "delivered":
                    invoice.attempt_count = step_number

                db.commit()

                logger.info(
                    f"[worker] cart={invoice.id[:8]} step={step_number} "
                    f"channel={channel.value} result={result['status']}"
                )

            except Exception as e:
                db.rollback()
                logger.error(f"[worker] Failed processing invoice {invoice.id[:8]}: {e}")
                continue

        # Close out the batch with measured totals
        batch.completed_at = datetime.now(timezone.utc)
        batch.total_invoices_scanned = scanned
        batch.total_at_risk_amount = at_risk_amount
        # Count explicit recovery-completion events since the previous batch;
        # a normal paid checkout is never treated as agent-recovered.
        recovery_events = db.query(AgentAuditTrail.cart_id).filter(
            AgentAuditTrail.action_taken == ActionTaken.RECOVERED_BY_AGENT,
            AgentAuditTrail.timestamp <= batch.completed_at,
        )
        if previous_completed_at is not None:
            recovery_events = recovery_events.filter(AgentAuditTrail.timestamp > previous_completed_at)
        recovered_ids = [row[0] for row in recovery_events.distinct().all()]
        recovered_amount = sum(
            (inv.amount for inv in db.query(Invoice).filter(Invoice.id.in_(recovered_ids)).all()),
            Decimal("0.00"),
        )
        batch.total_recovered_amount = recovered_amount
        batch.total_stopped_count = stopped_count
        batch.total_escalated_count = escalated_count
        batch.status = "COMPLETED"
        db.commit()

        logger.info(
            f"[batch {batch.id[:8]}] DONE - scanned={scanned} "
            f"recovered=₹{recovered_amount} at_risk=₹{at_risk_amount} "
            f"stopped={stopped_count} escalated={escalated_count}"
        )

    except Exception as e:
        db.rollback()
        batch.status = "FAILED"
        try:
            db.commit()
        except Exception:
            db.rollback()
        logger.error(f"[batch {batch.id[:8]}] Fatal batch error: {e}")

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Daemon entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    scheduler = BlockingScheduler(executors={"default": ThreadPoolExecutor(1)})
    scheduler.add_job(
        process_batch,
        trigger="interval",
        seconds=30,
        id="recovery_batch_job",
        next_run_time=datetime.now(),  # fire immediately on startup, then every 30s
        max_instances=1,               # never let two batches overlap
        coalesce=True,                 # if one run is delayed, don't queue duplicates
    )

    logger.info("AI Revenue Recovery worker started - polling every 30s. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker shutting down gracefully...")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
