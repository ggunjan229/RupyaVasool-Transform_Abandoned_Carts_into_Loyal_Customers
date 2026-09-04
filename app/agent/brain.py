# app/agent/brain.py
"""
Agent cognition layer: turns a failed/abandoned invoice into a compliant,
stage-appropriate recovery message using Gemini.

Design principle (carried over from the architecture blueprint):
Gemini decides TONE and CONTENT. It never decides WHETHER or WHEN to act,
or WHAT discount to offer — those are deterministic, code-controlled facts
passed into the prompt. This keeps the compliance story intact: an LLM
hallucination here can produce a badly-worded email, never an unauthorized
discount or an out-of-sequence escalation.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import IntEnum
from typing import Optional

from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError, ClientError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.config import settings

logger = logging.getLogger("agent.brain")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BrainError(Exception):
    """Base class for all recoverable brain-layer failures."""


class MissingAPIKeyError(BrainError):
    """Raised when GEMINI_API_KEY is absent or empty at call time."""


class GenerationFailedError(BrainError):
    """Raised when Gemini can't be reached or returns an unusable response,
    after retries are exhausted."""


# ---------------------------------------------------------------------------
# Recovery stages
# ---------------------------------------------------------------------------

class RecoveryStep(IntEnum):
    IMMEDIATE_REASSURANCE = 1   # Step 1 — Day 0/immediate
    STRATEGIC_NUDGE = 2         # Step 2 — Day 2
    FINAL_NOTICE = 3            # Step 3 — Day 4


# ---------------------------------------------------------------------------
# Client (lazy singleton — avoids crashing on import if key is missing)
# ---------------------------------------------------------------------------

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client

    api_key = settings.GEMINI_API_KEY
    if not api_key or not api_key.strip():
        raise MissingAPIKeyError(
            "GEMINI_API_KEY is missing or empty. Set it in your .env file."
        )

    _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_BASE_SYSTEM_INSTRUCTION = """You are an automated but courteous customer-recovery
assistant for an online store. You write short, human-sounding outbound messages
(email/SMS/WhatsApp style) that help customers complete a purchase they left
unfinished or that failed for a technical reason.

STRICT OUTPUT RULES:
- Return ONLY the raw message body text.
- Do NOT use markdown formatting of any kind (no **, #, backticks, bullet lists).
- Do NOT wrap the output in code blocks or quotation marks.
- Do NOT include a subject line, greeting salutation label, or sign-off block
  unless it is a natural part of the message body itself.
- Keep it under 80 words.
- Never invent a discount, price, or promise that was not explicitly given to
  you in these instructions.
"""

_STEP_INSTRUCTIONS = {
    RecoveryStep.IMMEDIATE_REASSURANCE: """
CONTEXT: This is Step 1 — an immediate, same-day message after a payment attempt
failed. The failure reason is: "{failure_reason}".

TONE: Act as a helpful store clerk clearing a simple transaction roadblock —
calm, reassuring, zero urgency, zero sales pressure.

RULES FOR THIS STEP:
- Do NOT offer any discount, coupon, or incentive of any kind.
- Do NOT imply the item might be lost or unavailable.
- Simply reassure the customer their item is reserved and offer a simple next
  step (e.g., retry payment or check their bank).
- If the failure reason suggests a bank/timeout/technical issue, gently note
  this is often on the bank's side and easily resolved by retrying.
""",
    RecoveryStep.STRATEGIC_NUDGE: """
CONTEXT: This is Step 2 — a follow-up message sent because the order is still
unrecovered after the first reassurance message (Day 2).

TONE: Warm, personal, mildly persuasive — a strategic nudge, not a hard sell.

RULES FOR THIS STEP:
- You MUST offer the exact coupon code SAVE5 for 5% off, and nothing more.
- Do not invent a different discount percentage or code.
- Create light urgency without being pushy (e.g., mention limited-time use).
""",
    RecoveryStep.FINAL_NOTICE: """
CONTEXT: This is Step 3 — a final notice message sent because the order is
still unrecovered after both prior attempts (Day 4).

TONE: Courteous, respectful, final — no pressure, no guilt-tripping.

RULES FOR THIS STEP:
- Clearly and politely state that the reserved item(s) will shortly be
  released back to public stock/inventory.
- Do NOT offer any further discount or incentive.
- Leave the door open: mention they're welcome to reorder anytime.
""",
}


def _build_prompt(
    customer_name: str,
    item_name: str,
    amount: Decimal,
    failure_reason: str,
    step: RecoveryStep,
) -> str:
    step_block = _STEP_INSTRUCTIONS[step].format(failure_reason=failure_reason or "not specified")

    return f"""{step_block}

CUSTOMER DETAILS:
- Name: {customer_name}
- Item: {item_name}
- Amount: {amount}

Write the message now, following all rules above.
"""


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((APIError,)),  # retry only on transient/server errors
)
def _call_gemini(prompt: str) -> str:
    client = _get_client()
    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=_BASE_SYSTEM_INSTRUCTION,
            temperature=0.7,
            max_output_tokens=220,
        ),
    )

    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise GenerationFailedError("Gemini returned an empty response.")

    return text.strip()


def generate_recovery_message(
    customer_name: str,
    item_name: str,
    amount: Decimal,
    failure_reason: str,
    step_number: int,
) -> str:
    """
    Generate a recovery message for the given customer/order at a given
    escalation step (1, 2, or 3).

    Raises:
        MissingAPIKeyError: if GEMINI_API_KEY isn't configured.
        GenerationFailedError: if Gemini is unreachable or fails after retries.
        ValueError: if step_number isn't 1, 2, or 3.
    """
    try:
        step = RecoveryStep(step_number)
    except ValueError:
        raise ValueError(
            f"Invalid step_number={step_number!r}. Must be 1, 2, or 3."
        )

    prompt = _build_prompt(customer_name, item_name, amount, failure_reason, step)

    try:
        return _call_gemini(prompt)

    except MissingAPIKeyError:
        raise  # already the right exception type — surface as-is

    except ClientError as e:
        # 4xx-class errors: bad key, bad request, quota — not worth retrying
        logger.error(f"[brain] Gemini client error (non-retryable): {e}")
        raise GenerationFailedError(f"Gemini rejected the request: {e}") from e

    except APIError as e:
        # Exhausted retries on a transient/server-side error
        logger.error(f"[brain] Gemini API error after retries: {e}")
        raise GenerationFailedError(f"Gemini API unavailable after retries: {e}") from e

    except Exception as e:
        # Catch-all for network drops (ConnectionError, TimeoutError, DNS
        # failures, etc.) so the worker loop can log-and-continue instead of
        # crashing the whole batch on one bad network blip.
        logger.error(f"[brain] Unexpected failure calling Gemini: {e}")
        raise GenerationFailedError(f"Unexpected failure generating message: {e}") from e


# ---------------------------------------------------------------------------
# Fallback templates — used by tools.py/worker if Gemini is down and the
# workflow must not silently skip an escalation step (compliance requirement:
# the cadence must still fire even if the AI text-generation layer fails).
# ---------------------------------------------------------------------------

FALLBACK_MESSAGES = {
    RecoveryStep.IMMEDIATE_REASSURANCE: (
        "Hi {customer_name}, we noticed your payment for {item_name} didn't go "
        "through. No worries — this is often a quick bank-side hiccup. Your item "
        "is still reserved; feel free to simply retry your payment when ready."
    ),
    RecoveryStep.STRATEGIC_NUDGE: (
        "Hi {customer_name}, your {item_name} is still waiting for you! Use code "
        "SAVE5 for 5% off when you complete your order — available for a "
        "limited time."
    ),
    RecoveryStep.FINAL_NOTICE: (
        "Hi {customer_name}, this is a final courtesy note that your reserved "
        "{item_name} will shortly be released back to stock. You're always "
        "welcome to reorder anytime."
    ),
}


def get_fallback_message(item_name: str, customer_name: str, step_number: int) -> str:
    """Deterministic, non-AI fallback so the escalation cadence never silently
    breaks if Gemini is down — call this from tools.py on GenerationFailedError."""
    step = RecoveryStep(step_number)
    return FALLBACK_MESSAGES[step].format(customer_name=customer_name, item_name=item_name)