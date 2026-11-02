"""The shapes our API returns. These are deliberately cleaner than the portal's own JSON."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    error: str = Field(description="Short machine-readable code.", examples=["meter_not_found"])
    message: str = Field(description="Human-readable explanation.")


class Location(BaseModel):
    latitude: float
    longitude: float


class HierarchyLevel(BaseModel):
    level: str = Field(description="One of zone, circle, division, subdivision, substation, feeder, dt.", examples=["circle"])
    code: str | None = Field(description="Null only if the portal never supplies it for this transformer.", examples=["C-01"])
    name: str | None = Field(examples=["Circle 1"])


class TransformerRef(BaseModel):
    code: str = Field(examples=["DT-001"])
    name: str = Field(examples=["Malviya Nagar DT 1"])
    feeder_code: str | None = Field(examples=["F-001"])
    capacity_kva: int | None = Field(examples=[100])


class Transformer(TransformerRef):
    meter_count: int
    hierarchy: list[HierarchyLevel] = Field(description="Zone down to the transformer itself, blanks filled in.")
    hierarchy_path: str = Field(description="Codes joined by '/'. Unique, unlike the codes themselves.", examples=["Z-01/C-01/D-01/SD-01/SS-01/F-001/DT-001"])


class TransformerList(BaseModel):
    items: list[Transformer]
    total: int


class MeterBase(BaseModel):
    meter_id: str = Field(examples=["J100000"])
    serial_no: str | None = Field(examples=["SE33962"])
    make: str | None = Field(examples=["HPL"])
    phase_type: str | None = Field(description="'single' or 'three'.", examples=["single"])
    install_status: str | None = Field(description="Installed, Faulty or Decommissioned.", examples=["Installed"])
    install_type: str | None = Field(examples=["Whole Current"])
    build: str | None = Field(description="The portal's 'legacy' / 'v2' flag, passed through unchanged.", examples=["legacy"])
    transformer_code: str | None = Field(examples=["DT-001"])
    location: Location | None


class MeterSummary(MeterBase):
    """One row of a meter search."""
    distance_km: float | None = Field(default=None, description="Only set when searching near a point.")


class MeterDetail(MeterBase):
    transformer: TransformerRef | None
    hierarchy: list[HierarchyLevel]
    hierarchy_path: str


class MeterPage(BaseModel):
    items: list[MeterSummary]
    total: int = Field(description="Number of meters matching the filters (not just this page).")
    page: int
    page_size: int


class HierarchyNode(BaseModel):
    level: str
    code: str | None
    name: str | None
    path: str = Field(description="Codes from the zone down to this node, joined by '/'. Use it with /meters?node=.")
    meter_count: int
    capacity_kva: int | None = Field(default=None, description="Only for transformer (dt) nodes.")
    children: list[HierarchyNode] = []


class Reading(BaseModel):
    timestamp: datetime = Field(description="ISO 8601 with offset. The portal gives no timezone; IST (+05:30) is assumed.")
    kwh: float | None = Field(description="Cumulative active-energy register, as the meter reports it.")
    kvah: float | None = Field(description="Cumulative apparent-energy register.")
    volt_r: float | None = Field(description="R-phase voltage. Three-phase meters report only this phase.")
    consumption_kwh: float | None = Field(
        description="kWh used since the previous reading (difference of the register). Null for the first reading."
    )


class ReadingsResponse(BaseModel):
    meter_id: str
    timezone: str = Field(description="Offset applied to the portal's timestamps (assumed, see README).")
    latest_reading_at: datetime | None = Field(description="Newest reading the portal holds for this meter, before any from/to filter.")
    count: int
    total_consumption_kwh: float | None = Field(description="Sum of consumption_kwh over the returned readings.")
    readings: list[Reading]


class SessionStatus(BaseModel):
    logged_in: bool
    expires_in_seconds: int | None


class CatalogStatus(BaseModel):
    loaded: bool
    meters: int
    transformers: int
    loaded_at: datetime | None
    age_seconds: float | None
    stale: bool = Field(description="True if a refresh is overdue (the portal could not be reached) and old data is served.")
    data_quality_notes: int = Field(description="How many data problems were found and repaired when loading.")


class Health(BaseModel):
    status: str = Field(description="'ok', 'starting' (nothing loaded yet) or 'degraded' (serving stale data).")
    session: SessionStatus
    catalog: CatalogStatus
