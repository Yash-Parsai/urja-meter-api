"""A fake Urja portal for tests. It follows the rules written down in PROTOCOL.md, so the real client
can be tested without network access, and so we can make the portal misbehave on purpose
(expire sessions, rotate the signing secret, run a skewed clock, go down)."""
from __future__ import annotations

import email.utils
import hashlib
import hmac
import json
import threading
import time
import urllib.parse

import httpx

PORTAL_ORIGIN = "https://urja-ops.flockenergy.tech"
EMAIL, PASSWORD = "tester@example.test", "correct-password"


class FakeClock:
    """A clock the test can move forward. Shared by the client under test and the fake portal."""

    def __init__(self, start: float = 1_793_500_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePortal:
    def __init__(self, clock: FakeClock, export_rows: list[dict], dt_pages: list[dict], energy: dict[str, dict],
                 *, secret: str = "fake-signing-secret", session_seconds: int = 3600, signature_window: int = 300) -> None:
        self.clock = clock
        self.export_rows = export_rows
        self.dt_pages = dt_pages
        self.energy = energy
        self.secret = secret
        self.session_seconds = session_seconds
        self.signature_window = signature_window
        self.clock_skew = 0.0          # portal clock = shared clock + skew
        self.outage = False            # every request answers 503
        self.reject_all_signatures = False
        self.latency = 0.0
        self.password = PASSWORD
        self.sessions: dict[str, float] = {}
        self.calls: list[str] = []     # "METHOD /path?query" for every request
        self.raw_targets: list[str] = []  # the path exactly as it went over the wire
        self.login_count = 0
        self._lock = threading.Lock()

    # -- controls for tests
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def expire_all_sessions(self) -> None:
        self.sessions.clear()

    def count(self, prefix: str) -> int:
        return sum(1 for call in self.calls if call.startswith(prefix))

    # -- the portal itself
    def portal_now(self) -> float:
        return self.clock() + self.clock_skew

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.latency:
            time.sleep(self.latency)
        target = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        with self._lock:
            self.calls.append(f"{request.method} {target}")
            self.raw_targets.append(request.url.raw_path.decode())
        if self.outage:
            return self._json(503, {"error": "unavailable", "message": "Down for maintenance."})
        path = request.url.path
        if request.method == "POST" and path == "/login":
            return self._login(request)
        if not self._authenticated(request):
            return self._json(401, {"error": "unauthorized", "message": "A valid session is required."})
        if path == "/portal/keys":
            return self._json(200, {"data": {"signingSecret": self.secret}})
        if path == "/portal/export":
            return self._export(request)
        if path == "/portal/dts":
            page = int(request.url.params.get("page", "1"))
            return self._json(200, self.dt_pages[page - 1] if page <= len(self.dt_pages) else
                              {"data": [], "total": 40, "page": page, "pageSize": 20})
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["portal", "meters"] and parts[3] == "energy":
            return self._energy(parts[2])
        return self._json(404, {"error": "not_found", "message": "No such route."})

    def _login(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("origin") != PORTAL_ORIGIN:
            return httpx.Response(403, text="Cross-site POST form submissions are forbidden")
        form = urllib.parse.parse_qs(request.content.decode())
        if form.get("email") != [EMAIL] or form.get("password") != [self.password]:
            return self._json(400, {"type": "failure", "status": 400})  # no cookie, like a rejected login
        with self._lock:
            self.login_count += 1
            token = f"session-{self.login_count}"
            self.sessions[token] = self.portal_now() + self.session_seconds
        cookie = f"__Secure-better-auth.session_token={token}; Max-Age={self.session_seconds}; Path=/; HttpOnly; Secure; SameSite=Lax"
        return self._json(200, {"type": "redirect", "status": 303, "location": "/meters"}, {"set-cookie": cookie})

    def _authenticated(self, request: httpx.Request) -> bool:
        for part in request.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name.endswith("session_token") and self.sessions.get(value, 0) > self.portal_now():
                return True
        return False

    def _export(self, request: httpx.Request) -> httpx.Response:
        stamp = request.headers.get("x-timestamp", "")
        given = request.headers.get("x-signature", "")
        query = request.url.query.decode()
        message = "\n".join(["GET", request.url.path, query, stamp])  # the recipe from PROTOCOL.md section 4
        expected = hmac.new(self.secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        fresh = stamp.isdigit() and abs(self.portal_now() - int(stamp)) <= self.signature_window
        if self.reject_all_signatures or not (fresh and hmac.compare_digest(given, expected)):
            return self._json(401, {"error": "signature_invalid", "message": "Missing or invalid request signature."})
        # Like the real portal: the page number changes nothing, every call returns everything.
        return self._json(200, {"data": self.export_rows, "total": len(self.export_rows)})

    def _energy(self, meter_id: str) -> httpx.Response:
        if meter_id in self.energy:
            return self._json(200, self.energy[meter_id])
        if any(row["meterId"] == meter_id for row in self.export_rows):
            return self._json(200, next(iter(self.energy.values())))
        return self._json(404, {"error": "not_found", "message": "Meter not found"})

    def _json(self, status: int, body: dict, headers: dict | None = None) -> httpx.Response:
        stamp = email.utils.formatdate(self.portal_now(), usegmt=True)
        return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json", "date": stamp, **(headers or {})})
