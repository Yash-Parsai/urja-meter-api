"""Cleaning rules: turn the portal's awkward data into clean values. Each rule is explained in PROTOCOL.md."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import HierarchyLevel, Reading

# The portal sends no timezone. This is an Indian utility, so we assume India Standard Time.
# A fixed offset avoids needing the tz database (which Windows does not ship).
IST = timezone(timedelta(hours=5, minutes=30), "IST")
PORTAL_TIME_FORMAT = "%d/%m/%Y %H:%M"

LEVELS = ("zone", "circle", "division", "subdivision", "substation", "feeder", "dt")


# ---------------------------------------------------------------------- numbers and times
def parse_portal_time(text: str) -> datetime:
    """'23/06/2026 23:30' -> 2026-06-23 23:30 +05:30."""
    return datetime.strptime(text.strip(), PORTAL_TIME_FORMAT).replace(tzinfo=IST)


def to_number(value: Any) -> float | None:
    """The portal sends numbers as strings. Anything that is not a finite number becomes None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


# ---------------------------------------------------------------------- readings
def parse_readings(rows: list[dict]) -> tuple[list[Reading], int]:
    """Portal rows -> readings, oldest first, with consumption worked out.

    kwh is a running total, so consumption is the difference to the previous reading. A drop in the
    total (the register was reset) gives no consumption instead of a negative number.
    Returns the readings and how many rows had to be skipped because their timestamp was unusable.
    """
    by_time: dict[datetime, dict] = {}
    skipped = 0
    for row in rows:
        try:
            stamp = parse_portal_time(row["timestamp"])
        except (KeyError, TypeError, AttributeError, ValueError):
            skipped += 1
            continue
        by_time[stamp] = row  # if a timestamp repeats, the later row wins

    readings: list[Reading] = []
    previous: Reading | None = None
    for stamp in sorted(by_time):
        row = by_time[stamp]
        kwh = to_number(row.get("kwh"))
        consumption = None
        if previous is not None and previous.kwh is not None and kwh is not None:
            delta = round(kwh - previous.kwh, 4)
            consumption = delta if delta >= 0 else None
        reading = Reading(
            timestamp=stamp,
            kwh=kwh,
            kvah=to_number(row.get("kvah")),
            volt_r=to_number(row.get("voltR")),
            consumption_kwh=consumption,
        )
        readings.append(reading)
        previous = reading
    return readings, skipped


# ---------------------------------------------------------------------- hierarchy
def resolve_transformer_paths(
    export_rows: list[dict], dt_rows: list[dict]
) -> tuple[dict[str, list[HierarchyLevel]], list[str]]:
    """Work out the full hierarchy of every transformer.

    All meters on one transformer sit at the same place in the network, so a blank value on one
    meter is filled in from the other meters on that transformer. The transformer list is the
    authority for transformer names (one transformer is named differently on a few meters).

    Returns {transformer code: [zone ... dt]} and a list of human-readable data-quality notes.
    """
    notes: list[str] = []
    dt_by_code = {row["code"]: row for row in dt_rows}
    meters_by_dt: dict[str, list[dict]] = defaultdict(list)
    for row in export_rows:
        code = text_or_none(row.get("dtCode"))
        if code:
            meters_by_dt[code].append(row)

    filled = 0
    paths: dict[str, list[HierarchyLevel]] = {}
    for dt_code in sorted(set(meters_by_dt) | set(dt_by_code)):
        meters = meters_by_dt.get(dt_code, [])
        dt_row = dt_by_code.get(dt_code)
        if dt_row is None:
            notes.append(f"{dt_code}: appears on meters but not in the transformer list")

        levels: list[HierarchyLevel] = []
        for level in LEVELS[:-1]:
            code, blank_codes = _consensus(meters, level, "code", dt_code, notes)
            name, blank_names = _consensus(meters, level, "name", dt_code, notes)
            if code is None and level == "feeder" and dt_row:
                code = text_or_none(dt_row.get("feederCode"))  # the transformer list knows its feeder
            if code is None or name is None:
                notes.append(f"{dt_code}: no {level} {'code' if code is None else 'name'} available anywhere")
            filled += (blank_codes if code else 0) + (blank_names if name else 0)
            levels.append(HierarchyLevel(level=level, code=code, name=name))

        levels.append(HierarchyLevel(level="dt", code=dt_code, name=_transformer_name(dt_code, dt_row, meters, notes)))
        paths[dt_code] = levels

    if filled:
        notes.append(f"{filled} blank hierarchy values were filled in from other meters on the same transformer")
    return paths, notes


def path_string(levels: list[HierarchyLevel]) -> str:
    return "/".join(level.code or "?" for level in levels)


def _node(row: dict, level: str) -> dict:
    hierarchy = row.get("hierarchy")
    node = hierarchy.get(level) if isinstance(hierarchy, dict) else None
    return node if isinstance(node, dict) else {}


def _consensus(meters: list[dict], level: str, field: str, dt_code: str, notes: list[str]) -> tuple[str | None, int]:
    """Most common non-blank value of one hierarchy field across a transformer's meters, plus how many were blank."""
    values = [text_or_none(_node(m, level).get(field)) for m in meters]
    present = [v for v in values if v]
    if not present:
        return None, len(values)
    ranked = [value for value, _ in Counter(present).most_common()]
    if len(ranked) > 1:
        notes.append(f"{dt_code}: meters disagree about the {level} {field} {ranked}; using {ranked[0]!r}")
    return ranked[0], len(values) - len(present)


def _transformer_name(dt_code: str, dt_row: dict | None, meters: list[dict], notes: list[str]) -> str | None:
    export_names = Counter(text_or_none(_node(m, "dt").get("name")) for m in meters)
    export_names.pop(None, None)
    official = text_or_none(dt_row.get("name")) if dt_row else None
    if official is None:
        return export_names.most_common(1)[0][0] if export_names else None
    for other, count in export_names.items():
        if other != official:
            notes.append(
                f"{dt_code}: {count} of {len(meters)} meters call this transformer {other!r}; "
                f"using {official!r} from the transformer list"
            )
    return official
