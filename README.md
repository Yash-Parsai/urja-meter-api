# Urja Meter API

A small read-only REST API in front of the Urja Meter Ops portal (https://urja-ops.flockenergy.tech).

The portal is a web app for people. A program that wants its data has to log in, log in again every hour, and sign the request for the bulk export, and the data itself has gaps in it. This service does all of that and returns clean JSON for meters, transformers, the network hierarchy and half-hourly readings.

How the portal behaves is written down in [PROTOCOL.md](PROTOCOL.md). This service's own API is described in [openapi.json](openapi.json) and, while it runs, at `/docs`. My answers to the reflection questions are in [REFLECTION.md](REFLECTION.md).

## Running it

You need Python 3.11 or newer.

```powershell
# Windows PowerShell. On macOS or Linux: python3 -m venv .venv, source .venv/bin/activate, cp
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Open `.env` and put in the test login from the assignment. `.env` is git-ignored. Then:

```powershell
python scripts/smoke.py
python -m uvicorn app.main:app --port 8000
```

`smoke.py` is optional. It logs in to the real portal, makes the signed export call, loads the transformers and reads one meter, and prints counts only. If it ends with "All checks passed", the service can talk to the portal.

### Settings

Environment variables, or the `.env` file. Real environment variables win.

| Name | Default | Meaning |
|---|---|---|
| `URJA_EMAIL`, `URJA_PASSWORD` | required | Portal login |
| `URJA_BASE_URL` | `https://urja-ops.flockenergy.tech` | Portal address |
| `CATALOG_TTL_SECONDS` | `600` | How long cached meter and transformer data counts as fresh |
| `SESSION_MARGIN_SECONDS` | `300` | Log in again this long before the one-hour session ends |
| `REQUEST_TIMEOUT_SECONDS` | `20` | Time to wait for the portal on one request |

## Trying it

Open http://127.0.0.1:8000/docs, or use curl. These samples come from the real export data (it is what the tests use), trimmed to fit.

Meters within 1 km of a point, closest first:

```powershell
curl.exe "http://127.0.0.1:8000/meters?lat=26.9&lng=75.8&radius_km=1&page_size=2"
```

```json
{
  "items": [
    {
      "meter_id": "J100350", "serial_no": "AL54158", "make": "HPL", "phase_type": "single",
      "install_status": "Installed", "install_type": "Whole Current", "build": "v2",
      "transformer_code": "DT-031",
      "location": { "latitude": 26.903724103146327, "longitude": 75.80512159105642 },
      "distance_km": 0.655
    },
    { "meter_id": "J100159", "distance_km": 0.9, "...": "..." }
  ],
  "total": 2, "page": 1, "page_size": 2
}
```

One meter, with its transformer and full path in the network:

```powershell
curl.exe "http://127.0.0.1:8000/meters/J100011"
```

```json
{
  "meter_id": "J100011", "serial_no": "SE48422", "make": "HPL", "phase_type": "three",
  "install_status": "Decommissioned", "install_type": "Whole Current", "build": "legacy",
  "transformer_code": "DT-012",
  "location": { "latitude": 26.985815217862932, "longitude": 75.8113866226377 },
  "transformer": { "code": "DT-012", "name": "Mansarovar DT 12", "feeder_code": "F-012", "capacity_kva": 100 },
  "hierarchy": [
    { "level": "zone", "code": "Z-03", "name": "Jaipur Zone 3" },
    { "level": "circle", "code": "C-06", "name": "Circle 6" },
    "... division, subdivision, substation and feeder ...",
    { "level": "dt", "code": "DT-012", "name": "Mansarovar DT 12" }
  ],
  "hierarchy_path": "Z-03/C-06/D-02/SD-12/SS-12/F-012/DT-012"
}
```

Readings from a given time on:

```powershell
curl.exe "http://127.0.0.1:8000/meters/J100000/readings?from=2026-06-30T23:00:00"
```

```json
{
  "meter_id": "J100000",
  "timezone": "+05:30 (IST, assumed)",
  "latest_reading_at": "2026-06-30T23:30:00+05:30",
  "count": 2,
  "total_consumption_kwh": 0.85,
  "readings": [
    { "timestamp": "2026-06-30T23:00:00+05:30", "kwh": 48580.36, "kvah": 52466.79, "volt_r": 230.0, "consumption_kwh": 0.42 },
    { "timestamp": "2026-06-30T23:30:00+05:30", "kwh": 48580.79, "kvah": 52467.25, "volt_r": 220.0, "consumption_kwh": 0.43 }
  ]
}
```

`kwh` and `kvah` are the meter's running totals. `consumption_kwh` is this reading minus the one before it, which is the energy used in that half hour. It is also filled in for the first reading inside a `from` filter, because the reading before it is still known.

## Endpoints

| Endpoint | What it returns |
|---|---|
| `GET /meters` | A page of meters. Filters: `q` (meter id or serial, any case), `status`, `make`, `phase`, `build`, `install_type`, `transformer`, `node` (a hierarchy path), `lat` with `lng` and `radius_km` (closest first), `page`, `page_size` (up to 100) |
| `GET /meters/{meter_id}` | One meter with location, transformer and full hierarchy |
| `GET /meters/{meter_id}/readings` | Half-hourly readings, fetched live. Optional `from` and `to` (ISO 8601) |
| `GET /transformers`, `GET /transformers/{code}` | Transformers with capacity, meter count and hierarchy |
| `GET /hierarchy?depth=` | The network as a tree. Every node has a unique `path` |
| `GET /health` | Login state, age of the cached data, and whether it is stale. It never calls the portal |

Errors always have the same shape: `{"error": "meter_not_found", "message": "No meter with id 'J999999'."}`. A portal that is down gives 503. A portal that refuses our login or answers in an odd way gives 502. A bad parameter gives 422.

## How the code is organised

```
app/
  main.py        routes, error format, start-up
  portal.py      the portal client: login, session renewal, signed requests, retries
  signing.py     the request signature (about ten lines)
  normalize.py   cleaning rules: times, numbers, consumption, blank hierarchy values
  catalog.py     cached meters and transformers, the hierarchy tree, search
  models.py      response shapes (they become openapi.json)
  config.py      settings
  errors.py      the few error types passed between layers
scripts/
  probe.py          the script I used to explore the portal
  smoke.py          end-to-end check against the real portal
  export_openapi.py rewrites openapi.json from the code
tests/              pytest suite, a fake portal and captured responses
```

Calls only go downward: `main` uses `catalog`, `catalog` uses `portal`, `portal` uses `signing`. `normalize` is plain functions with no network, which makes it easy to test.

A request for `/meters` never touches the portal. It reads the cached copy. A request for readings goes to the portal for that one meter.

## Decisions

The meter and transformer data is fetched once and kept in memory for ten minutes. The portal has one bulk export that returns all 403 meters, and that data rarely changes, so search, filters, the hierarchy and nearby search all run on the cached copy. If the portal is down when the copy expires, the old copy is served and `/health` reports `degraded`. The service also starts when the portal is down and tries again on the first request. Readings are different. They are one call per meter and the part most likely to change, so they are fetched live every time.

The export is called once. `page=2` returns the same rows as `page=1`, so a loop that waits for an empty page would never stop.

The signing secret is fetched from `/portal/keys` when needed, not kept in config, because it comes from the portal and might change. If the portal refuses a signature, the client fetches the secret again and retries once. It also corrects its clock from the portal's `Date` header, since the signature carries a timestamp that the portal checks.

A login is judged by the cookie. A successful login answers 200 with a "redirect" inside the JSON body, so the status code says little. The code treats "a session cookie arrived" as success. I did not test what a wrong password returns, so a missing cookie counts as a rejected login whatever the status. The login also needs an `Origin` header.

The client logs in again five minutes before the hour is up, and again after any 401. A counter stops many threads from logging in at the same moment. Each recovery is tried once and then the call fails with a clear error, so nothing can loop forever.

Hierarchy codes are only unique inside their parent, so every node is identified by its full path, for example `Z-01/C-01/D-07/SD-07/SS-07/F-007/DT-007`.

Fields like status and make are plain strings, not fixed lists. If the portal adds a new value, the API keeps working. Times use a fixed +05:30 offset instead of a timezone database, so the service runs on Windows without extra packages.

Routes are normal functions. FastAPI runs them in threads and the portal client is safe to share. That is simpler than async, and the slow part is the portal anyway.

## Assumptions

- The readings are in IST (+05:30). The portal sends no timezone and this is an Indian utility, so I assumed India Standard Time. Every readings response says so.
- `kwh` and `kvah` are running totals, because they only go up. If a total ever goes down (a reset), consumption is `null` and not a negative number.
- The readings end on 30 June 2026 although it is later than that now. The API reports `latest_reading_at` instead of pretending the data is current.
- The export is complete. It says `total: 403` and has 403 rows. A warning is logged if those ever differ.
- When the transformer list and some meters disagree on a name (DT-007), the list wins, and the conflict is reported as a data-quality note.
- All meters on one transformer sit at the same place in the network. That is how the 22 blank hierarchy values are filled in.
- The shared test login is for testing. A real deployment would use its own read-only account.

## Left out

- A web client and a bulk dataset download. Both are optional in the brief.
- Login on this API. It is read-only and meant for a trusted network. I would not put it on the open internet as it is.
- A cache for readings. They are always fresh, and every readings request calls the portal.
- A database. The cached data lives in memory and is rebuilt when the service starts.

## What I would do next

1. Cache readings for a few seconds per meter and merge identical requests that arrive together, so one busy client cannot hammer the portal.
2. Find the exact time window of the signature and write it into PROTOCOL.md.
3. Find out what the `build` field means. I saw no difference between `legacy` and `v2` in the data.
4. Ask someone who knows the system about the timezone instead of assuming it.
5. Add metrics (request counts, portal latency, number of logins), authentication, and a Dockerfile.

Nearby search is a plain scan with the haversine distance. On a laptop it takes about half a millisecond for these 403 meters and about 55 milliseconds for a catalog 100 times bigger. For millions of meters I would move the catalog into a database with a spatial index, such as PostGIS, and sync it in small steps instead of downloading everything again.

## Tests

```powershell
pip install -r requirements-dev.txt
pytest
```

88 tests, about two seconds, no network. They run against a fake portal (`tests/fake_portal.py`) that follows the rules in PROTOCOL.md and is loaded with real responses captured from the portal (`tests/fixtures/`). The fake can end sessions early, change the signing secret, run a wrong clock or go offline, so those cases are tested too. The tests were also checked by breaking the code on purpose in 15 places. 14 were caught at once, and a test was added for the 15th.

They only show that the code matches what I understood about the portal. `scripts/smoke.py` is the check against the real one.
