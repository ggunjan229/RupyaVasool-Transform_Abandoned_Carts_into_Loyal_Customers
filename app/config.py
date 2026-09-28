# app/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from decimal import Decimal


class Settings(BaseSettings):
    # --- App ---
    APP_NAME: str = "RupyaVasool"
    ENV: str = "development"
    DEBUG: bool = True

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./revenue_recovery.db"

    # --- Gemini ---
    # Optional: deterministic fallback copy keeps the demo usable without a key.
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.6-flash"  # fast + cheap for batch diagnosis

    # --- Escalation cadence (days since last action) ---
    STAGE_1_DAY: int = 1
    STAGE_2_DAY: int = 3
    STAGE_3_DAY: int = 5
    MAX_RECOVERY_STAGE: int = 3

    # --- Stopping rules ---
    BATCH_STOP_THRESHOLD_PCT: float = 0.40  # circuit breaker: pause batch if >40% stopped
    MIN_AMOUNT_DUE: Decimal = Decimal("1.00")  # ignore dust amounts

    # --- Worker ---
    WORKER_POLL_INTERVAL_SECONDS: int = 30
    BATCH_SIZE: int = 50
    ABANDONMENT_GRACE_SECONDS: int = 60
    STEP_INTERVAL_SECONDS: int = 60

    # --- Compliance ---
    ENABLE_OPT_OUT_CHECK: bool = True
    DND_HOURS_START: int = 21  # no outbound contact 9pm–8am
    DND_HOURS_END: int = 8
    DND_TIMEZONE: str = "Asia/Kolkata"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
