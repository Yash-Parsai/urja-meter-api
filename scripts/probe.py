"""
probe.py - throwaway exploration script for the Urja Meter Ops portal (version 2).

This does NOT build the API. It asks the portal a fixed list of questions and
prints what comes back, so PROTOCOL.md can be written from facts, not guesses.

* Cookie values and the signing secret are never printed (only lengths / a short
  fingerprint), so the console output is safe to paste.
* Raw JSON responses are saved in probe_out/ (git-ignored) - later they become
  test fixtures.

Run from the project root:   python scripts/probe.py
"""
import datetime as dt
import email.utils
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # safe on Windows consoles

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.config import Settings  # noqa: E402

try:  # the login comes from .env or the environment, like everywhere else in the project
    _settings = Settings.from_env()
except RuntimeError as exc:
    sys.exit(f"Settings problem: {exc}")
BASE, EMAIL, PASSWORD = _settings.base_url, _settings.email, _settings.password
OUT = pathlib.Path("probe_out")
OUT.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- signing
def sign(secret: str, method: str, path: str, query: str, timestamp: int) -> str:
    """Signature recipe for the export endpoint.

    Worked out from one request the browser made, then reproduced exactly:
        HMAC-SHA256( key = signingSecret, message = "METHOD\\nPATH\\nQUERY\\nTIMESTAMP" ), hex digest
    PATH has no query string, QUERY has no leading '?', TIMESTAMP is unix seconds.
    """
    message = "\n".join([method.upper(), path, query, str(timestamp)])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- printing helpers
def mask_set_cookie(value: str) -> str:
    """'name=SECRET; Path=/; HttpOnly'  ->  'name=<24 chars>; Path=/; HttpOnly'"""
    first, _, rest = value.partition(";")
    name, _, secret = first.partition("=")
    return f"{name}=<{len(secret)} chars>;{rest}"


def describe(body):
    """Describe JSON without dumping it: list lengths, small values, type names."""
    if isinstance(body, dict):
        out = {}
        for key, value in body.items():
            if isinstance(value, list):
                out[key] = f"list[{len(value)}]"
            elif isinstance(value, (int, float, bool)) or value is None:
                out[key] = value
            else:
                out[key] = type(value).__name__
        return out
    if isinstance(body, list):
        return f"list[{len(body)}]"
    return type(body).__name__


def report(label, r, save_as=None):
    """Print a compact report for one response; return the parsed JSON (or None)."""
    ctype = r.headers.get("content-type", "?").split(";")[0]
    line = f"{label:<42} {r.status_code}  {ctype:<18} {len(r.content):>8} B"
    if r.headers.get("location"):
        line += f"  -> {r.headers['location']}"
    print(line)
    for raw in r.headers.get_list("set-cookie"):  # does the portal refresh the session cookie?
        print("    (this response also sets a cookie):", mask_set_cookie(raw))
    body = None
    if "json" in ctype:
        try:
            body = r.json()
        except ValueError:
            pass
    if body is None:
        print("    body starts:", r.text[:160].replace("\n", " "))
        return None
    if r.status_code >= 400:
        print("    error body:", r.text[:200])
        return body
    print("    shape :", describe(body))
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, list) and value:
                print(f"    first {key!r}:", json.dumps(value[0])[:200])
                break
    if save_as:
        (OUT / save_as).write_bytes(r.content)
    return body


def call(client, label, path, save_as=None):
    r = client.get(path)
    return r, report(label, r, save_as)


def series_report(rows):
    """One line that describes a list of energy readings."""
    if not rows:
        return "EMPTY list"
    keys = sorted({k for r in rows for k in r})
    problems = 0
    for r in rows:
        for k, v in r.items():
            if k != "timestamp":
                try:
                    float(v)
                except (TypeError, ValueError):
                    problems += 1
    try:
        stamps = [dt.datetime.strptime(r["timestamp"], "%d/%m/%Y %H:%M") for r in rows]
    except (KeyError, ValueError) as exc:
        return f"{len(rows)} rows, keys {keys}, TIMESTAMP PROBLEM {exc!r}"
    gaps = sorted({(b - a).total_seconds() / 60 for a, b in zip(stamps, stamps[1:])})
    try:
        kwh = [float(r["kwh"]) for r in rows]
        rising = all(b >= a for a, b in zip(kwh, kwh[1:]))
    except (KeyError, TypeError, ValueError):
        rising = "n/a"
    return (f"{len(rows)} rows, keys {keys}, last {stamps[-1]:%d/%m/%Y %H:%M}, gaps(min) {gaps}, "
            f"kwh never decreases: {rising}, non-numeric values: {problems}")


# --------------------------------------------------------------------------- portal helpers
def new_client():
    return httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)


def do_login(client, with_origin=True, js_style=False):
    headers = {"Origin": BASE} if with_origin else {}
    if js_style:  # copy what the page's own JavaScript sends
        headers.update({"x-sveltekit-action": "true", "Accept": "application/json"})
        return client.post("/login", files={"email": (None, EMAIL), "password": (None, PASSWORD)}, headers=headers)
    return client.post("/login", data={"email": EMAIL, "password": PASSWORD}, headers=headers)


def report_login(client, r):
    print(f"POST /login -> {r.status_code}  location={r.headers.get('location')}")
    print("    body:", r.text[:200].replace("\n", " "))
    for raw in r.headers.get_list("set-cookie"):
        print("    Set-Cookie:", mask_set_cookie(raw))
    for c in client.cookies.jar:
        if c.expires:
            when = dt.datetime.fromtimestamp(c.expires, dt.timezone.utc).isoformat(timespec="seconds")
            lifetime = f"expires {when} (about {(c.expires - time.time()) / 60:.0f} min from now)"
        else:
            lifetime = "session cookie (no expiry set)"
        print(f"    jar: {c.name}  secure={c.secure}  {lifetime}")


def get_secret(client, label):
    """GET /portal/keys and return the signing secret (never printed in full)."""
    r = client.get("/portal/keys")
    body = report(label, r)
    secret = ((body or {}).get("data") or {}).get("signingSecret") if r.status_code == 200 else None
    if secret:
        fingerprint = hashlib.sha256(secret.encode()).hexdigest()[:8]
        print(f"    signingSecret: {len(secret)} chars, fingerprint {fingerprint} (value hidden)")
        server_time = email.utils.parsedate_to_datetime(r.headers["date"]) if r.headers.get("date") else None
        if server_time:
            skew = (dt.datetime.now(dt.timezone.utc) - server_time).total_seconds()
            print(f"    your clock minus the portal's clock: {skew:+.0f} s")
    return secret


def signed_get(client, secret, label, path, query, timestamp=None, save_as=None):
    ts = int(time.time()) if timestamp is None else timestamp
    url = f"{path}?{query}" if query else path
    r = client.get(url, headers={"X-Timestamp": str(ts), "X-Signature": sign(secret, "GET", path, query, ts)})
    return r, report(label, r, save_as)


# --------------------------------------------------------------------------- the questions
def main():
    print(f"Target: {BASE}\n")

    print("== 0. Without logging in ==")
    with new_client() as anon:
        call(anon, "GET search (no cookie)", "/portal/meters/search?q=&page=1")

    client = new_client()

    print("\n== 1. Login: plain form POST (what a browser does without JavaScript) ==")
    r = do_login(client)
    report_login(client, r)
    if not client.cookies:
        print("\n== 1b. Login again, copying what the page's JavaScript sends ==")
        r = do_login(client, js_style=True)
        report_login(client, r)
    if not client.cookies:
        print("\nSTOP: no cookie came back, so we are not logged in. Send me everything above.")
        return

    print("\n== 2. Meter list / search ==")
    call(client, "GET search page 1", "/portal/meters/search?q=&page=1", "search_p1.json")
    call(client, "GET search page 999 (past the end)", "/portal/meters/search?q=&page=999")
    call(client, "GET search q=j10000 (lower case)", "/portal/meters/search?q=j10000&page=1")
    call(client, "GET search q=0000 (middle of an id)", "/portal/meters/search?q=0000&page=1")

    print("\n== 3. Signed export ==")
    secret = get_secret(client, "GET /portal/keys")
    export = None
    if secret:
        _, export = signed_get(client, secret, "GET export page=1 (signed)", "/portal/export", "page=1", save_as="export_p1.json")
        signed_get(client, secret, "GET export page=2 (signed)", "/portal/export", "page=2")
        # Why: tells us how much clock drift our service could survive.
        signed_get(client, secret, "GET export page=1, timestamp 10 min old", "/portal/export", "page=1",
                   timestamp=int(time.time()) - 600)
    else:
        print("    no signing secret, skipping the signed calls")

    print("\n== 4. Energy readings: one meter of each kind (phase / build / status) ==")
    rows = export.get("data", []) if isinstance(export, dict) else []
    samples = {}
    for m in rows:
        samples.setdefault((m.get("phaseType"), m.get("build"), m.get("installStatus")), m["meterId"])
    if not samples:
        samples = {("single", "legacy", "Decommissioned"): "J100000", ("single", "v2", "Faulty"): "J100004",
                   ("three", "legacy", "Faulty"): "J100005"}
    for (phase, build, status), meter_id in list(samples.items())[:16]:
        r = client.get(f"/portal/meters/{meter_id}/energy")
        tag = f"{meter_id} {phase}/{build}/{status}"
        if r.status_code != 200:
            print(f"{tag:<44} HTTP {r.status_code}: {r.text[:120]}")
            continue
        data = r.json().get("data")
        print(f"{tag:<44} {series_report(data)}")
        (OUT / f"energy_{meter_id}.json").write_bytes(r.content)

    print("\n== 5. A meter that does not exist ==")
    call(client, "GET energy J999999", "/portal/meters/J999999/energy")

    print("\n== 6. Session behaviour ==")
    no_origin = new_client()
    r = do_login(no_origin, with_origin=False)
    print(f"login WITHOUT an Origin header -> {r.status_code}, cookie set: {bool(no_origin.cookies)}, body: {r.text[:100]!r}")
    second = new_client()
    do_login(second)
    secret2 = get_secret(second, "second login: GET /portal/keys")
    if secret and secret2:
        print("    same signing secret in both sessions:", secret == secret2)
    call(client, "first session still valid after 2nd login?", "/portal/meters/search?q=&page=1")

    for c in (client, no_origin, second):
        c.close()
    print(f"\nDone. Raw responses saved in {OUT.resolve()}")


if __name__ == "__main__":
    main()
