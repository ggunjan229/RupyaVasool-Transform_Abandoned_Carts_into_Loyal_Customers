# app/agent/tools.py
"""
Execution layer: simulated delivery channels for the recovery agent.

These functions stand in for real Twilio/SendGrid calls so the whole
pipeline is testable locally with zero API cost. The return contract
{"status": ..., "timestamp": ...} is what run_worker.py will parse to
write the AgentAuditTrail row - so the shape of this dict matters and
should not be changed casually once the worker depends on it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Literal

logger = logging.getLogger("agent.tools")

DeliveryStatus = Literal["delivered", "failed"]

_BORDER_WIDTH = 78


# ---------------------------------------------------------------------------
# Validation helpers - fail loud and early rather than "sending" garbage
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


def _validate_phone(phone: str) -> bool:
    digits_only = re.sub(r"[^\d]", "", phone or "")
    return len(digits_only) >= 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _print_bordered(title: str, lines: list[str]) -> None:
    bar = "=" * _BORDER_WIDTH
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)
    for line in lines:
        print(f"  {line}")
    print(f"{bar}\n")


# ---------------------------------------------------------------------------
# Channel 1: Simulated Email (stand-in for SendGrid)
# ---------------------------------------------------------------------------

def send_recovery_email(email: str, subject: str, body: str) -> dict:
    """
    Simulate sending a recovery email. Prints a bordered payload to the
    terminal instead of hitting a real provider.

    Returns:
        dict: {"status": "delivered" | "failed", "timestamp": ISO-8601 str,
               "channel": "Email", "error": str | None}
    """
    timestamp = _now_iso()

    try:
        if not _validate_email(email):
            raise ValueError(f"Invalid email address: {email!r}")
        if not subject or not subject.strip():
            raise ValueError("Email subject cannot be empty.")
        if not body or not body.strip():
            raise ValueError("Email body cannot be empty.")

        _print_bordered(
            f"📧  SIMULATED EMAIL DISPATCH  -  {timestamp}",
            [
                f"To      : {email}",
                f"Subject : {subject}",
                "-" * (_BORDER_WIDTH - 4),
                *[f"{line}" for line in body.strip().splitlines()],
            ],
        )

        return {
            "status": "delivered",
            "timestamp": timestamp,
            "channel": "Email",
            "error": None,
        }

    except ValueError as e:
        logger.warning(f"[tools] Email send skipped - validation failed: {e}")
        return {
            "status": "failed",
            "timestamp": timestamp,
            "channel": "Email",
            "error": str(e),
        }

    except Exception as e:
        # Defensive catch-all: in a real integration this would be a
        # network/provider error. We never let a delivery-layer exception
        # crash the worker's batch loop.
        logger.error(f"[tools] Unexpected error sending email: {e}")
        return {
            "status": "failed",
            "timestamp": timestamp,
            "channel": "Email",
            "error": f"Unexpected error: {e}",
        }


# ---------------------------------------------------------------------------
# Channel 2: Simulated WhatsApp (stand-in for Twilio)
# ---------------------------------------------------------------------------

def send_recovery_whatsapp(phone: str, body: str) -> dict:
    """
    Simulate sending a recovery WhatsApp message. Prints a bordered payload
    to the terminal instead of hitting a real provider.

    Returns:
        dict: {"status": "delivered" | "failed", "timestamp": ISO-8601 str,
               "channel": "WhatsApp", "error": str | None}
    """
    timestamp = _now_iso()

    try:
        if not _validate_phone(phone):
            raise ValueError(f"Invalid phone number: {phone!r}")
        if not body or not body.strip():
            raise ValueError("WhatsApp message body cannot be empty.")

        _print_bordered(
            f"💬  SIMULATED WHATSAPP DISPATCH  -  {timestamp}",
            [
                f"To : {phone}",
                "-" * (_BORDER_WIDTH - 4),
                *[f"{line}" for line in body.strip().splitlines()],
            ],
        )

        return {
            "status": "delivered",
            "timestamp": timestamp,
            "channel": "WhatsApp",
            "error": None,
        }

    except ValueError as e:
        logger.warning(f"[tools] WhatsApp send skipped - validation failed: {e}")
        return {
            "status": "failed",
            "timestamp": timestamp,
            "channel": "WhatsApp",
            "error": str(e),
        }

    except Exception as e:
        logger.error(f"[tools] Unexpected error sending WhatsApp message: {e}")
        return {
            "status": "failed",
            "timestamp": timestamp,
            "channel": "WhatsApp",
            "error": f"Unexpected error: {e}",
        }
