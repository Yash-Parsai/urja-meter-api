"""Shared test setup. Everything runs against a fake portal built from real captured responses."""
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.catalog import CatalogService
from app.config import Settings
from app.main import create_app
from app.portal import PortalClient
from tests.fake_portal import EMAIL, PASSWORD, FakeClock, FakePortal

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def export_rows() -> list[dict]:
    return load("export.json")["data"]


@pytest.fixture(scope="session")
def dt_pages() -> list[dict]:
    return [load("dts_page1.json"), load("dts_page2.json")]


@pytest.fixture(scope="session")
def dt_rows(dt_pages) -> list[dict]:
    return [row for page in dt_pages for row in page["data"]]


@pytest.fixture(scope="session")
def energy() -> dict[str, dict]:
    return {"J100000": load("energy_J100000.json"), "J100004": load("energy_J100004.json")}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(email=EMAIL, password=PASSWORD, base_url="https://urja-ops.flockenergy.tech")


@pytest.fixture
def fake_portal(clock, export_rows, dt_pages, energy) -> FakePortal:
    return FakePortal(clock, export_rows, dt_pages, energy)


@pytest.fixture
def portal(settings, fake_portal, clock):
    client = PortalClient(settings, transport=fake_portal.transport(), clock=clock, retry_delay=0)
    yield client
    client.close()


@pytest.fixture
def catalog(portal, clock) -> CatalogService:
    return CatalogService(portal, ttl_seconds=600, clock=clock)


@pytest.fixture
def api(settings, portal):
    with TestClient(create_app(settings, portal)) as client:
        yield client
