"""Settings read from the environment (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
STORE_TZ = ZoneInfo("America/Campo_Grande")

GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str | None
    llm_model: str
    temperature: float
    today_override: datetime | None
    data_dir: Path
    runtime_dir: Path


def _parse_now(value: str | None) -> datetime | None:
    """EMPORIO_TODAY accepts a date (keeps the current time of day) or a full datetime."""
    if not value:
        return None
    if "T" in value or " " in value.strip():
        parsed = datetime.fromisoformat(value.strip())
    else:
        clock = datetime.now(STORE_TZ).time().replace(microsecond=0)
        parsed = datetime.combine(date.fromisoformat(value.strip()), clock)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=STORE_TZ)


def get_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        llm_base_url=os.getenv("LLM_BASE_URL", GEMINI_OPENAI_URL),
        llm_api_key=os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", "gemini-3.5-flash"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        today_override=_parse_now(os.getenv("EMPORIO_TODAY")),
        data_dir=Path(os.getenv("EMPORIO_DATA_DIR", ROOT / "data")),
        runtime_dir=Path(os.getenv("EMPORIO_RUNTIME_DIR", ROOT / "data" / "runtime")),
    )


def make_clock(settings: Settings):
    """Return a zero-arg function giving the store's current time (aware, Campo Grande)."""
    if settings.today_override is None:
        return lambda: datetime.now(STORE_TZ).replace(microsecond=0)
    fixed = settings.today_override
    return lambda: fixed


def at(day: date, hour: int = 10, minute: int = 0) -> datetime:
    """Helper for tests and scripts: an aware datetime in store time."""
    return datetime.combine(day, time(hour, minute), tzinfo=STORE_TZ)
