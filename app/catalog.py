"""The catalog: meters and transformers loaded from the portal's export, cleaned, cached and searchable.

Why cache: the export is the only way to get hierarchy and location, and it is one big call (about
230 KB). Meter and transformer data changes rarely, so we load it once and re-load when it is older
than the TTL. Readings are different (one call per meter) and are fetched live, not cached here.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .errors import PortalError, PortalNotFound, PortalProtocolError
from .models import (
    CatalogStatus,
    HierarchyLevel,
    HierarchyNode,
    Location,
    MeterBase,
    MeterDetail,
    MeterSummary,
    Transformer,
    TransformerRef,
)
from .normalize import resolve_transformer_paths, path_string, text_or_none, to_number
from .portal import PortalClient

log = logging.getLogger("urja.catalog")


@dataclass(frozen=True)
class CatalogSnapshot:
    """One immutable, consistent copy of the data. A refresh builds a new one and swaps it in."""

    meters: tuple[MeterDetail, ...]
    by_id: dict[str, MeterDetail]
    transformers: tuple[Transformer, ...]
    transformers_by_code: dict[str, Transformer]
    tree: tuple[HierarchyNode, ...]
    loaded_at: float
    notes: tuple[str, ...]


# ---------------------------------------------------------------------- building a snapshot
def build_snapshot(export_rows: list[dict], dt_rows: list[dict], loaded_at: float) -> CatalogSnapshot:
    paths, notes = resolve_transformer_paths(export_rows, dt_rows)
    dt_by_code = {row["code"]: row for row in dt_rows}

    rows: dict[str, dict] = {}
    for row in export_rows:
        meter_id = text_or_none(row.get("meterId")) if isinstance(row, dict) else None
        if meter_id is None:
            notes.append("skipped an export row without a meterId")
        elif meter_id in rows:
            notes.append(f"{meter_id}: appears twice in the export; using the first")
        else:
            rows[meter_id] = row

    meter_counts = Counter(text_or_none(row.get("dtCode")) for row in rows.values())
    transformers: dict[str, Transformer] = {}
    for code, levels in paths.items():
        dt_row = dt_by_code.get(code, {})
        transformers[code] = Transformer(
            code=code,
            name=levels[-1].name or code,
            feeder_code=text_or_none(dt_row.get("feederCode")) or levels[5].code,
            capacity_kva=_whole_number(dt_row.get("capacityKva")),
            meter_count=meter_counts.get(code, 0),
            hierarchy=levels,
            hierarchy_path=path_string(levels),
        )

    meters: list[MeterDetail] = []
    for meter_id in sorted(rows):
        row = rows[meter_id]
        transformer = transformers.get(text_or_none(row.get("dtCode")) or "")
        if transformer is None:
            notes.append(f"{meter_id}: has no known transformer, so no hierarchy")
        meters.append(
            MeterDetail(
                meter_id=meter_id,
                serial_no=text_or_none(row.get("serialNo")),
                make=text_or_none(row.get("make")),
                phase_type=text_or_none(row.get("phaseType")),
                install_status=text_or_none(row.get("installStatus")),
                install_type=text_or_none(row.get("installType")),
                build=text_or_none(row.get("build")),
                transformer_code=transformer.code if transformer else None,
                location=_location(row.get("geo")),
                transformer=TransformerRef(**transformer.model_dump(include=set(TransformerRef.model_fields))) if transformer else None,
                hierarchy=transformer.hierarchy if transformer else [],
                hierarchy_path=transformer.hierarchy_path if transformer else "",
            )
        )

    ordered = tuple(sorted(transformers.values(), key=lambda t: t.code))
    return CatalogSnapshot(
        meters=tuple(meters),
        by_id={m.meter_id: m for m in meters},
        transformers=ordered,
        transformers_by_code={t.code: t for t in ordered},
        tree=tuple(_build_tree(ordered, notes)),
        loaded_at=loaded_at,
        notes=tuple(notes),
    )


def _whole_number(value: object) -> int | None:
    number = to_number(value)
    return int(number) if number is not None and number == int(number) else None


def _location(geo: object) -> Location | None:
    if not isinstance(geo, dict):
        return None
    lat = to_number(geo.get("lat", geo.get("latitude")))
    lng = to_number(geo.get("lng", geo.get("longitude")))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return Location(latitude=lat, longitude=lng)


@dataclass
class _TreeNode:
    level: str
    code: str | None
    name: str | None
    path: str
    meter_count: int = 0
    capacity_kva: int | None = None
    children: dict[str, "_TreeNode"] = field(default_factory=dict)


def _build_tree(transformers: tuple[Transformer, ...], notes: list[str]) -> list[HierarchyNode]:
    """Merge every transformer's path into a tree. Nodes are keyed by their full path, because
    codes such as 'D-01' are reused under different parents and would otherwise be merged."""
    roots: dict[str, _TreeNode] = {}
    for transformer in transformers:
        siblings = roots
        for depth, level in enumerate(transformer.hierarchy):
            path = "/".join(item.code or "?" for item in transformer.hierarchy[: depth + 1])
            node = siblings.get(path)
            if node is None:
                node = siblings[path] = _TreeNode(level.level, level.code, level.name, path)
            elif node.name and level.name and node.name != level.name:
                notes.append(f"{path}: two different names for the same place ({node.name!r}, {level.name!r})")
            node.meter_count += transformer.meter_count
            if level.level == "dt":
                node.capacity_kva = transformer.capacity_kva
            siblings = node.children
    return [_freeze(node) for node in sorted(roots.values(), key=lambda n: n.path)]


def _freeze(node: _TreeNode) -> HierarchyNode:
    return HierarchyNode(
        level=node.level,
        code=node.code,
        name=node.name,
        path=node.path,
        meter_count=node.meter_count,
        capacity_kva=node.capacity_kva,
        children=[_freeze(child) for child in sorted(node.children.values(), key=lambda n: n.path)],
    )


def prune_tree(nodes: list[HierarchyNode], depth: int) -> list[HierarchyNode]:
    """Keep only the first `depth` levels of the tree."""
    if depth <= 0:
        return []
    return [node.model_copy(update={"children": prune_tree(node.children, depth - 1)}) for node in nodes]


# ---------------------------------------------------------------------- searching
def search_meters(
    snapshot: CatalogSnapshot,
    *,
    q: str | None = None,
    status: str | None = None,
    make: str | None = None,
    phase: str | None = None,
    build: str | None = None,
    install_type: str | None = None,
    transformer: str | None = None,
    node: str | None = None,
    near: tuple[float, float, float] | None = None,
) -> list[MeterSummary]:
    """Filter the meters. Text filters ignore case. With `near=(lat, lng, radius_km)` only meters inside
    the radius are returned, closest first; otherwise the result is sorted by meter id.

    This is a plain scan over all meters. That is fine for hundreds or thousands of meters; see the
    README for what would change at a much larger scale.
    """
    needle = q.strip().casefold() if q else None
    node_prefix = node.strip().strip("/").casefold() if node else None
    found: list[tuple[float | None, MeterDetail]] = []
    for meter in snapshot.meters:
        if needle and needle not in meter.meter_id.casefold() and needle not in (meter.serial_no or "").casefold():
            continue
        if not (
            _same(meter.install_status, status)
            and _same(meter.make, make)
            and _same(meter.phase_type, phase)
            and _same(meter.build, build)
            and _same(meter.install_type, install_type)
            and _same(meter.transformer_code, transformer)
        ):
            continue
        if node_prefix:
            path = meter.hierarchy_path.casefold()
            if path != node_prefix and not path.startswith(node_prefix + "/"):
                continue
        distance = None
        if near is not None:
            if meter.location is None:
                continue
            distance = _haversine_km(near[0], near[1], meter.location.latitude, meter.location.longitude)
            if distance > near[2]:
                continue
        found.append((distance, meter))

    if near is not None:
        found.sort(key=lambda pair: (pair[0], pair[1].meter_id))
    return [_summary(meter, distance) for distance, meter in found]


def _same(actual: str | None, wanted: str | None) -> bool:
    return wanted is None or (actual is not None and actual.casefold() == wanted.strip().casefold())


def _summary(meter: MeterDetail, distance: float | None) -> MeterSummary:
    data = {name: getattr(meter, name) for name in MeterBase.model_fields}
    return MeterSummary(**data, distance_km=None if distance is None else round(distance, 3))


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points on Earth, in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------- the cache
class CatalogService:
    def __init__(
        self,
        portal: PortalClient,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.time,
        failure_backoff_seconds: float = 30.0,
        miss_refresh_cooldown_seconds: float = 30.0,
    ) -> None:
        self.portal = portal
        self._ttl = ttl_seconds
        self._clock = clock
        self._backoff = failure_backoff_seconds
        self._miss_cooldown = miss_refresh_cooldown_seconds
        self._lock = threading.Lock()
        self._snapshot: CatalogSnapshot | None = None
        self._retry_not_before = 0.0

    def snapshot(self) -> CatalogSnapshot:
        """The current data. Reloads from the portal when it is older than the TTL; if that fails
        and there is old data, the old data is served (and we wait before trying again)."""
        current = self._snapshot
        if current is not None and self._is_fresh(current):
            return current
        with self._lock:
            current = self._snapshot
            if current is not None and self._is_fresh(current):  # someone else just refreshed
                return current
            if current is not None and self._clock() < self._retry_not_before:
                return current  # the portal failed a moment ago; do not hammer it
            return self._refresh()

    def lookup(self, meter_id: str) -> MeterDetail | None:
        """Find one meter. An unknown id triggers one reload (at most every 30 s) in case the meter is new."""
        key = meter_id.strip().upper()
        current = self.snapshot()
        meter = current.by_id.get(key)
        if meter is not None:
            return meter
        with self._lock:
            current = self._snapshot or current
            age = self._clock() - current.loaded_at
            if age >= self._miss_cooldown and self._clock() >= self._retry_not_before:
                current = self._refresh()
        return current.by_id.get(key)

    def status(self) -> CatalogStatus:
        current = self._snapshot
        if current is None:
            return CatalogStatus(loaded=False, meters=0, transformers=0, loaded_at=None, age_seconds=None, stale=False, data_quality_notes=0)
        age = self._clock() - current.loaded_at
        return CatalogStatus(
            loaded=True,
            meters=len(current.meters),
            transformers=len(current.transformers),
            loaded_at=datetime.fromtimestamp(current.loaded_at, timezone.utc),
            age_seconds=round(age, 1),
            stale=age > self._ttl,
            data_quality_notes=len(current.notes),
        )

    def _is_fresh(self, snapshot: CatalogSnapshot) -> bool:
        return self._clock() - snapshot.loaded_at < self._ttl

    def _refresh(self) -> CatalogSnapshot:
        """Load and swap in a new snapshot. Caller holds the lock."""
        try:
            new = self._load()
        except PortalError as exc:
            if self._snapshot is None:
                raise
            self._retry_not_before = self._clock() + self._backoff
            log.warning("could not refresh the catalog (%s); serving the data from %.0f s ago",
                        exc, self._clock() - self._snapshot.loaded_at)
            return self._snapshot
        self._snapshot = new
        self._retry_not_before = 0.0
        log.info("catalog loaded: %d meters, %d transformers, %d data-quality notes",
                 len(new.meters), len(new.transformers), len(new.notes))
        for note in new.notes:
            log.info("data quality: %s", note)
        return new

    def _load(self) -> CatalogSnapshot:
        try:
            export_rows = self.portal.export_meters()
            dt_rows = self.portal.list_transformers()
        except PortalNotFound as exc:  # a 404 here means the portal changed, not that a meter is missing
            raise PortalProtocolError(f"the portal no longer has an expected endpoint: {exc}") from exc
        try:
            return build_snapshot(export_rows, dt_rows, loaded_at=self._clock())
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PortalProtocolError(f"the portal data has an unexpected shape: {exc!r}") from exc
