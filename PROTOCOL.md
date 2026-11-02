# How the Urja Meter Ops portal works

These are my notes on the Urja Meter Ops portal (https://urja-ops.flockenergy.tech), written while building the API in this repo. I used the shared test login. Unless a line says otherwise, I saw it myself on the live portal, either in the browser's Network tab or with `scripts/probe.py`. What I could not check is listed at the end.

The portal is a SvelteKit app, but its pages get all their data from JSON endpoints under `/portal/`, so there is nothing to scrape. You log in and send a cookie. Most endpoints need only the cookie. The bulk export also needs a signed request.

Paths below are relative to the portal's origin. Everything returns JSON. Errors look like `{"error": "<code>", "message": "<text>"}`.

## Logging in

`POST /login` with the form fields `email` and `password`. The request must also carry an `Origin` header equal to the portal's own origin. Without it the portal answers 403 with the text "Cross-site POST form submissions are forbidden". A browser adds `Origin` by itself. A script has to add it.

A successful login returns HTTP 200 and this body:

```json
{"type":"redirect","status":303,"location":"/meters"}
```

The redirect is inside the JSON, and the HTTP status is a plain 200, so the status says little. The cookie in the `Set-Cookie` header is what counts. It is called `__Secure-better-auth.session_token` and is sent with `Max-Age=3600`, `Path=/`, `HttpOnly`, `Secure` and `SameSite=Lax`.

- The session lasts one hour from the login. I looked at 12 later responses and none of them sent a new `Set-Cookie`, so using the session does not seem to extend it.
- A second login does not end the first session. Both cookies kept working.
- A call without a valid session gets 401 and `{"error":"unauthorized","message":"A valid session is required."}`. It is JSON, not a redirect to the login page.

A client therefore logs in once, keeps the cookie, and logs in again when it sees a 401 or when the hour is nearly over.

## Endpoints

| Endpoint | Returns | Notes |
|---|---|---|
| `GET /portal/meters/search?q=&page=N` | `{data, total, page, pageSize}` | 20 rows a page, pages start at 1, 403 meters in all. A page past the end returns 200 with an empty `data` list. `q` matches meter id or serial number as a substring and ignores case. Each row has only `meterId`, `serialNo`, `make`, `phaseType`, `installStatus` and `dtCode`. |
| `GET /portal/export?page=1` | `{data, total}`, 403 rows | Needs a signature (next section). Each row also has `installType`, `build`, `hierarchy` and `geo`. |
| `GET /portal/meters/{id}/energy` | `{data}`, 337 readings | See "Readings". An unknown id gives 404 `{"error":"not_found","message":"Meter not found"}`. |
| `GET /portal/meters/{id}/geo` | `{data: {latitude, longitude}}` | Both values are strings. For the two meters I compared, they match the export's `geo`, so I did not need this endpoint. |
| `GET /portal/dts?page=N` | `{data, total, page, pageSize}` | The 40 transformers, 20 a page: `code`, `name`, `feederCode`, `capacityKva`. |
| `GET /portal/keys` | `{data: {signingSecret}}` | A 32-character secret used to sign export requests. |

The first 20 meters on the search page match the same meters in the export on all six fields they share.

## The signed export

Calling `/portal/export` with only the cookie gives 401 and `{"error":"signature_invalid","message":"Missing or invalid request signature."}`. Two extra headers are needed.

```
secret    = data.signingSecret from GET /portal/keys
timestamp = current unix time, in whole seconds
message   = METHOD, PATH, QUERY and timestamp, joined with newline characters
signature = hex(HMAC-SHA256(secret, message))

headers   = X-Timestamp: <timestamp>
            X-Signature: <signature>
```

PATH has no query string. QUERY has no leading `?`. For the export the message is `GET\n/portal/export\npage=1\n<timestamp>`.

A worked example with a made-up secret:

```
secret    = test-secret
timestamp = 1793500000
message   = "GET\n/portal/export\npage=1\n1793500000"
signature = 90c41b438aa6de353c52d4e3d16a696371436d315520ea3e2087fccb0e33ec2a
```

That value comes from OpenSSL, not from this project's code. The command is in `tests/test_signing.py`, so you can check an implementation against it.

The portal's own page does the same thing. It calls `/portal/keys`, then a helper function with `("GET", "/portal/export", "page=1", secret)`, then sends `x-timestamp` and `x-signature`. The recipe above reproduces the signature of one request captured in the browser exactly, and a script that uses it received the full 231,658-byte export from the live portal. I did not read the body of the portal's signing helper.

What I measured:

- A fresh timestamp is accepted. A timestamp 10 minutes old is refused with 401 `signature_invalid`. The real limit is somewhere under 10 minutes, so the client's clock matters. The `Date` header in the portal's responses can be used to correct drift.
- The secret was the same in two separate logins, and in a browser session about ten minutes earlier. I still fetch it at run time and do not hard-code it.
- The query string is part of what gets signed. A request for `page=2` signed as `page=2` was accepted.
- The `page` value does nothing. `page=2` returns the same 403 rows and the same `total` as `page=1`. A loop that fetches until a page comes back empty would never end. Ask once.

## Readings

```json
{"data": [{"timestamp": "23/06/2026 23:30", "kwh": "48438.74", "kvah": "52313.84", "voltR": "226"}]}
```

- Each meter has 337 readings: seven days of half-hours, plus one. They are exactly 30 minutes apart, oldest first, with no duplicates. All 12 meters I sampled run from `23/06/2026 23:30` to `30/06/2026 23:30`. It is November now, so the data is old.
- `kwh` and `kvah` are meter registers, running totals that only grow. They are not consumption per interval. Consumption is the difference between two neighbouring readings. For the two meters I read through in full, J100000 used about 0.42 kWh per half hour (roughly 20 kWh a day) and J100004 about 0.21 (roughly 10 kWh a day).
- In those two meters `kvah` was never lower than `kwh`, and `voltR` stayed between 220 and 240.
- Every number is a string with trailing zeros dropped: `"48440"`, not `"48440.00"`.
- Timestamps are `dd/mm/yyyy HH:mm` with no timezone.

## Things that behave oddly

1. Decommissioned meters still have readings. 75 of the 403 meters are `Decommissioned`, and the four I tried (J100000, J100007, J100011, J100025) each returned the full seven days. So `installStatus` does not say whether data exists.
2. Three-phase meters report only `voltR`. The six I checked returned the same fields as single-phase meters, with no `voltY` or `voltB`.
3. Serial prefixes do not match the make. Serials starting with `AL` appear on HPL, Secure and Genus meters.
4. Hierarchy codes are only unique inside their parent. `D-01` exists under circles `C-01`, `C-03` and `C-05`. Every division, subdivision and substation code is reused this way, and so are 12 of the 28 feeder codes. To identify a place you need the whole path from the zone down.
5. The feeder codes `F-001` to `F-012` each belong to two transformers under two different substations (`F-001` sits under `SS-01` and `SS-11`). The `feederCode` in the transformer list is therefore not safe to join on. The DT codes (`DT-001` to `DT-040`) are unique, and the list's `feederCode` agrees with the export for all 40.
6. 22 meters have a blank somewhere in the hierarchy: circle (5 blank names, 6 blank codes), substation (3 blank codes) and feeder (3 blank names, 5 blank codes). No node is blank in both its name and its code. Each blank can be filled from other meters on the same transformer, because once blanks are ignored, every transformer has exactly one path.
7. DT-007 has two names. The transformer list says "Sanganer DT 7" and 10 meters agree. Three meters (J100400, J100401, J100402) say "Old Malviya Nagar Xfmr". They are also the only meters outside the J100000 to J100399 run. I treat the transformer list as the authority.
8. `geo` has two shapes. The export gives numbers as `{lat, lng}` and `/geo` gives strings as `{latitude, longitude}`. All 403 meters have coordinates, all around Jaipur (latitude 26.79 to 27.04, longitude 75.66 to 75.91). None is an outlier and no two share a point.
9. `build` is `legacy` for 238 meters and `v2` for 165. It is not a column in the meter list. I compared 12 meters (single and three-phase, legacy and v2, all three statuses) and saw no difference in their `/energy` or `/geo` data. I keep it as plain information.
10. Search returns a slim row, so hierarchy and location only come from the signed export.

## Not checked

- How exact the timestamp limit is. All I know is that it is under 10 minutes.
- The body of the portal's signing helper. I reproduced and tested the recipe but did not read the code.
- What a wrong password returns.
- Whether there are rate limits. None showed up in about 27 requests per run of the probe script.
- Whether `/energy` accepts query parameters such as a date range. I only used the plain call.
- Whether the signing secret ever changes. It stayed the same over about ten minutes and two logins.
- The timezone of the readings. I assume IST because the utility is in Rajasthan.
- What the `build` field is for.

## How I found all this

I logged in with DevTools open on the Network tab and clicked through the meter list, a single meter and the Transformers page. Every data request turned out to be a JSON fetch under `/portal/`. I saved some of the responses and compared them with the file from the "Export all meters" button, which has the same data as the signed export endpoint.

The export button first calls `/portal/keys` and then sends `x-timestamp` and `x-signature`. I captured one of those requests so the signature could be checked against it. After that I used `scripts/probe.py`, a read-only script that asks the portal one thing at a time and prints what comes back, and ran it on the live portal. The numbers above come from those runs and from going through the export file.
