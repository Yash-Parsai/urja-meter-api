"""Client for the Urja Meter Ops portal.

It hides what the portal makes awkward (details in PROTOCOL.md):
  * you have to log in, and the session cookie only lasts one hour;
  * the bulk export needs a signed request, with a secret fetched from /portal/keys;
  * a successful login is a cookie, not an HTTP status.

The client is safe to share between threads (FastAPI runs our routes in a thread pool).
"""
from __future__ import annotations

import email.utils
import logging
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .config import Settings
from .errors import PortalAuthError, PortalNotFound, PortalProtocolError, PortalUnavailable
from .signing import sign

log = logging.getLogger("urja.portal")

SESSION_COOKIE_HINT = "session_token"
DEFAULT_SESSION_SECONDS = 3600  # what the portal gave in every test; only used if the cookie has no Max-Age
MAX_TRANSFORMER_PAGES = 20  # safety stop for the transformer-list loop
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class PortalClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        retry_delay: float = 0.5,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._retry_delay = retry_delay
        self._http = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )
        url = httpx.URL(settings.base_url)
        self._origin = f"{url.scheme}://{url.netloc.decode('ascii')}"  # the portal rejects logins without it

        self._lock = threading.RLock()
        self._generation = 0  # goes up with every login; lets threads avoid logging in twice
        self._session_expires_at: float | None = None
        self._secret: str | None = None
        self._clock_offset = 0.0  # portal's clock minus ours, in seconds

    # ------------------------------------------------------------------ public API
    def export_meters(self) -> list[dict]:
        """All meters with hierarchy and location, in one signed call.

        The portal ignores the `page` value (page=2 returns the same rows), so we ask exactly once.
        """
        body = self._request("/portal/export", query="page=1", signed=True)
        rows = _list_at(body, "data", "export")
        total = body.get("total") if isinstance(body, dict) else None
        if isinstance(total, int) and total != len(rows):
            log.warning("export says total=%s but contains %s rows", total, len(rows))
        return rows

    def list_transformers(self) -> list[dict]:
        """The distribution-transformer list (a normal paged endpoint)."""
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(1, MAX_TRANSFORMER_PAGES + 1):
            body = self._request("/portal/dts", query=f"page={page}")
            batch = _list_at(body, "data", "transformer list")
            fresh = []
            for row in batch:
                if not isinstance(row, dict) or "code" not in row:
                    raise PortalProtocolError("transformer list contains a row without a 'code'")
                if row["code"] not in seen:
                    fresh.append(row)
                    seen.add(row["code"])
            if not fresh:  # empty page, or the portal repeated itself: stop either way
                break
            rows.extend(fresh)
            total = body.get("total") if isinstance(body, dict) else None
            if isinstance(total, int) and len(rows) >= total:
                break
        return rows

    def read_energy(self, meter_id: str) -> list[dict]:
        """Raw half-hourly readings for one meter (strings, dd/mm/yyyy dates, cumulative kWh)."""
        body = self._request(f"/portal/meters/{quote(meter_id, safe='')}/energy")
        return _list_at(body, "data", "energy readings")

    def session_info(self) -> dict[str, Any]:
        expires = self._session_expires_at
        if expires is None:
            return {"logged_in": False, "expires_in_seconds": None}
        remaining = int(expires - self._clock())
        return {"logged_in": remaining > 0, "expires_in_seconds": max(remaining, 0)}

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ session handling
    def login(self) -> None:
        with self._lock:
            self._http.cookies.clear()
            self._secret = None
            self._session_expires_at = None
            try:
                response = self._http.post(
                    "/login",
                    data={"email": self._settings.email, "password": self._settings.password},
                    headers={"Origin": self._origin},
                )
            except httpx.TransportError as exc:
                raise PortalUnavailable(f"could not reach the portal to log in: {exc}") from exc
            if response.status_code >= 500:
                raise PortalUnavailable(f"portal login failed with HTTP {response.status_code}")
            if not any(SESSION_COOKIE_HINT in cookie.name for cookie in self._http.cookies.jar):
                # A rejected login does not set the session cookie (the HTTP status alone is not reliable).
                raise PortalAuthError(f"login was rejected by the portal (HTTP {response.status_code})")
            self._note_server_time(response)
            lifetime = _cookie_lifetime(response) or DEFAULT_SESSION_SECONDS
            self._session_expires_at = self._clock() + lifetime
            self._generation += 1
            log.info("logged in to the portal; session lasts %s s", lifetime)

    def _session_is_fresh(self) -> bool:
        expires = self._session_expires_at
        return expires is not None and self._clock() < expires - self._settings.session_margin_seconds

    def _ensure_session(self) -> None:
        if self._session_is_fresh():
            return
        with self._lock:
            if not self._session_is_fresh():  # another thread may have logged in while we waited
                self.login()

    def _relogin(self, seen_generation: int) -> None:
        with self._lock:
            if self._generation == seen_generation:  # nobody has logged in since our request started
                self.login()

    def _signing_secret(self) -> str:
        with self._lock:
            if self._secret is None:
                body = self._request("/portal/keys")
                secret = _dig(body, "data", "signingSecret")
                if not isinstance(secret, str) or not secret:
                    raise PortalProtocolError("the portal did not return a signing secret")
                self._secret = secret
            return self._secret

    # ------------------------------------------------------------------ requests
    def _request(self, path: str, *, query: str = "", signed: bool = False) -> Any:
        """GET a JSON endpoint. Logs in first, logs in again if the session died, re-signs if the signature
        was refused. Each of those recoveries is tried once, so this can never loop forever."""
        relogged = resigned = False
        reason = "unauthorized"
        for _ in range(3):
            self._ensure_session()
            generation = self._generation
            response = self._send(path, query, signed)
            reason = _unauthorized_reason(response) or ""
            if not reason:
                return _parse(response, path)
            if reason == "signature_invalid" and signed and not resigned:
                resigned = True
                with self._lock:
                    self._secret = None  # fetch the secret again; this also re-syncs our clock
                continue
            if not relogged:
                relogged = True
                self._relogin(generation)
                continue
            break
        raise PortalAuthError(f"the portal keeps rejecting our request for {path} ({reason})")

    def _send(self, path: str, query: str, signed: bool) -> httpx.Response:
        url = f"{path}?{query}" if query else path
        headers: dict[str, str] = {}
        if signed:
            secret = self._signing_secret()
            timestamp = int(self._clock() + self._clock_offset)
            headers = {"X-Timestamp": str(timestamp), "X-Signature": sign(secret, "GET", path, query, timestamp)}
        error: Exception | None = None
        for attempt in range(2):  # one retry for network blips; every call here is a read-only GET
            try:
                response = self._http.get(url, headers=headers)
            except httpx.TransportError as exc:
                error = exc
                if attempt == 0 and self._retry_delay:
                    time.sleep(self._retry_delay)
                continue
            self._note_server_time(response)
            return response
        raise PortalUnavailable(f"could not reach the portal: {error}") from error

    def _note_server_time(self, response: httpx.Response) -> None:
        """Remember how far the portal's clock is from ours (the signature has to carry a fresh timestamp)."""
        header = response.headers.get("date")
        if not header:
            return
        try:
            server_time = email.utils.parsedate_to_datetime(header).timestamp()
        except (TypeError, ValueError):
            return
        self._clock_offset = server_time - self._clock()


# ---------------------------------------------------------------------- small helpers
def _cookie_lifetime(response: httpx.Response) -> int | None:
    for raw in response.headers.get_list("set-cookie"):
        if SESSION_COOKIE_HINT in raw.split("=", 1)[0]:
            match = re.search(r"max-age=(\d+)", raw, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _unauthorized_reason(response: httpx.Response) -> str | None:
    """None if the response is not an authentication problem, otherwise the portal's error code."""
    if response.status_code == 401:
        return _error_code(response) or "unauthorized"
    # Not seen in testing (the portal answers 401 JSON), but cheap to be safe about.
    if response.status_code in REDIRECT_STATUSES and "/login" in response.headers.get("location", ""):
        return "unauthorized"
    return None


def _parse(response: httpx.Response, path: str) -> Any:
    status = response.status_code
    if status == 404:
        raise PortalNotFound(f"the portal has no {path}")
    if status >= 500:
        raise PortalUnavailable(f"the portal answered HTTP {status} for {path}")
    if status != 200:
        raise PortalProtocolError(f"unexpected HTTP {status} from {path}: {response.text[:120]!r}")
    try:
        return response.json()
    except ValueError:
        raise PortalProtocolError(f"{path} did not return JSON") from None


def _dig(body: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(body, dict):
            return None
        body = body.get(key)
    return body


def _list_at(body: Any, key: str, what: str) -> list:
    value = _dig(body, key)
    if not isinstance(value, list):
        raise PortalProtocolError(f"{what}: expected a list under '{key}'")
    return value
