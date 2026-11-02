"""Settings, read from environment variables and from the .env file in the project folder (if there is one)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

DEFAULT_BASE_URL = "https://urja-ops.flockenergy.tech"
# Always the project folder, so it does not matter which directory the server is started from.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class Settings:
    email: str
    password: str
    base_url: str = DEFAULT_BASE_URL
    # How long the cached meter/transformer data counts as fresh.
    catalog_ttl_seconds: float = 600.0
    # Log in again this many seconds before the 1-hour session cookie runs out.
    session_margin_seconds: float = 300.0
    request_timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        """Real environment variables win over the .env file."""
        values = _read_environment(ENV_FILE if env_file is None else env_file)
        email = values.get("URJA_EMAIL", "").strip()
        password = values.get("URJA_PASSWORD", "")
        if not email or not password:
            raise RuntimeError(
                "URJA_EMAIL and URJA_PASSWORD are not set. Copy .env.example to .env and fill them in."
            )
        return cls(
            email=email,
            password=password,
            base_url=values.get("URJA_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL,
            catalog_ttl_seconds=_number(values, "CATALOG_TTL_SECONDS", cls.catalog_ttl_seconds),
            session_margin_seconds=_number(values, "SESSION_MARGIN_SECONDS", cls.session_margin_seconds),
            request_timeout_seconds=_number(values, "REQUEST_TIMEOUT_SECONDS", cls.request_timeout_seconds),
        )


def _read_environment(env_file: Path) -> Mapping[str, str]:
    from_file = {k: v for k, v in dotenv_values(env_file).items() if v is not None} if env_file.is_file() else {}
    return {**from_file, **os.environ}


def _number(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        number = float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None
    if number <= 0:
        raise RuntimeError(f"{name} must be greater than zero, got {raw!r}")
    return number
