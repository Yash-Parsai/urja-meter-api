# Reflection

## Assumptions

The one I am least sure about is the timezone. The readings have no timezone and I assumed IST, because the utility is in India. Every readings response says so. If it is wrong, every timestamp is off by hours and nothing in the code would notice.

The others are easier to defend. `kwh` and `kvah` only ever go up, so I treat them as running totals and take consumption as the difference between neighbouring readings. When the transformer list and a few meters disagree on a name (DT-007), I trust the list. All meters on one transformer sit at the same place in the network, which is how I fill the 22 blank hierarchy values. The export is complete because it says 403 and has 403 rows, and a warning is logged if that ever changes. And a fresh timestamp is enough for a signature, which I only tested at two points: fresh works, ten minutes old does not.

## The hardest part

The signed export. My first call to `/portal/export` used only the session cookie, like every other endpoint, and it came back 401. A valid session was clearly not enough, and the response only said "Missing or invalid request signature".

What helped was going back to the browser and treating it as the specification. In the Network tab the page calls `/portal/keys` first, then sends two headers, `X-Timestamp` and `X-Signature`, with the export request. I copied one real request, and a piece of the portal's own JavaScript showed which values go into the signature: the method, the path, the query, the timestamp and the secret. What was left was the exact text that gets signed. Candidate formats were tried against the signature of the captured request until one matched exactly. Then I checked it on the live portal. A fresh timestamp was accepted and a ten-minute-old one was refused.

Working from a real captured request, and not from guesses, is what got me through it.

## With another day

1. Cache readings for a few seconds per meter and merge identical requests, so one busy client cannot hammer the portal.
2. Measure the things listed as "not checked" in PROTOCOL.md: the exact signature window, what a wrong password returns, rate limits.
3. Find out what `build` is for. I saw no difference between `legacy` and `v2`.
4. Ask someone who knows the system about the timezone.
5. Run the service against the real portal for a few hours and watch what happens when sessions expire in normal use. Then add metrics, authentication and a Dockerfile.

## A mistake

I assumed the export worked like the search endpoint. I called it with only the cookie, got a 401, and at first did not understand why. Then I assumed it was paged, and it is not: `page=2` returns the same 403 rows as `page=1`. A client that loops until a page is empty would never stop. The code now asks once, and a test keeps it that way.

## What I would criticise

The tests only prove that the code matches what I understood about the portal. The fake portal is built from my own reading of the real one, so a misunderstanding would be in both, and the tests would still pass. The only real check is `scripts/smoke.py` against the live portal, and that is a script, not an automated test.

I know the portal from one shared account and a limited amount of probing. Rare things, like the secret changing, a long outage or a different response shape, are handled by defensive code that I never saw trigger for real.

Some claims rest on small samples: 12 meters for the readings and the `build` field, and 2 meters read through in full. I never checked the timezone and I never tried a wrong password.

The service has no authentication. That is on purpose for this exercise, but it is a real gap.
