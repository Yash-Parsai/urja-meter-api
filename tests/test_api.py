import json
import pathlib

import pytest

from app.main import create_app

ROOT = pathlib.Path(__file__).resolve().parent.parent


def assert_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == code


# ---------------------------------------------------------------- health
def test_health_reports_what_was_loaded_at_startup(api):
    body = api.get("/health").json()
    assert body["status"] == "ok"
    assert body["session"]["logged_in"] is True
    assert body["catalog"]["meters"] == 403 and body["catalog"]["transformers"] == 40
    assert body["catalog"]["data_quality_notes"] > 0


def test_health_never_calls_the_portal(api, fake_portal):
    before = len(fake_portal.calls)
    api.get("/health")
    assert len(fake_portal.calls) == before


# ---------------------------------------------------------------- meters
def test_default_listing_is_the_first_page_by_meter_id(api):
    body = api.get("/meters").json()
    assert (body["total"], body["page"], body["page_size"], len(body["items"])) == (403, 1, 20, 20)
    first = body["items"][0]
    assert first["meter_id"] == "J100000" and first["transformer_code"] == "DT-001"
    assert first["distance_km"] is None


def test_filters_and_paging_work_together(api):
    body = api.get("/meters", params={"status": "faulty", "page_size": 5, "page": 2}).json()
    assert body["total"] == 90 and len(body["items"]) == 5
    assert all(item["install_status"] == "Faulty" for item in body["items"])


def test_search_by_part_of_a_serial_ignores_case(api):
    items = api.get("/meters", params={"q": "se33962"}).json()["items"]
    assert [item["meter_id"] for item in items] == ["J100000"]


def test_near_search_returns_distances(api):
    spot = api.get("/meters/J100000").json()["location"]
    body = api.get("/meters", params={"lat": spot["latitude"], "lng": spot["longitude"], "radius_km": 2}).json()
    assert body["items"][0]["meter_id"] == "J100000"
    assert all(item["distance_km"] <= 2 for item in body["items"])


def test_bad_parameters_get_the_standard_error_body(api):
    assert_error(api.get("/meters", params={"page_size": 1000}), 422, "invalid_request")
    assert_error(api.get("/meters", params={"page": 0}), 422, "invalid_request")
    assert_error(api.get("/meters", params={"lat": 26.9}), 422, "invalid_request")


def test_meter_detail_has_hierarchy_transformer_and_location(api):
    body = api.get("/meters/j100011").json()  # lower case works too
    assert body["meter_id"] == "J100011"
    assert [level["level"] for level in body["hierarchy"]] == ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]
    assert body["hierarchy_path"].endswith("/DT-012")
    assert body["transformer"]["capacity_kva"] == 100
    assert isinstance(body["location"]["latitude"], float)
    assert "distance_km" not in body  # that field belongs to search results only


def test_unknown_meter_is_a_404_with_the_standard_body(api):
    assert_error(api.get("/meters/J999999"), 404, "meter_not_found")


# ---------------------------------------------------------------- readings
def test_readings_are_clean_and_include_consumption(api):
    body = api.get("/meters/J100000/readings").json()
    assert body["count"] == 337
    assert body["latest_reading_at"] == "2026-06-30T23:30:00+05:30"
    assert body["total_consumption_kwh"] == pytest.approx(142.05)
    first, second = body["readings"][:2]
    assert first["timestamp"] == "2026-06-23T23:30:00+05:30" and first["consumption_kwh"] is None
    assert second["kwh"] == 48439.16 and second["consumption_kwh"] == pytest.approx(0.42)


def test_a_from_filter_keeps_consumption_correct_for_its_first_reading(api):
    body = api.get("/meters/J100000/readings", params={"from": "2026-06-30T00:00:00"}).json()  # no offset: read as IST
    assert body["count"] == 48
    assert body["readings"][0]["timestamp"] == "2026-06-30T00:00:00+05:30"
    assert body["readings"][0]["consumption_kwh"] == pytest.approx(0.43)  # measured against 29 June 23:30
    assert body["latest_reading_at"] == "2026-06-30T23:30:00+05:30"  # not affected by the filter


def test_times_with_an_offset_or_z_mean_the_same_moment(api):
    ist = api.get("/meters/J100000/readings", params={"to": "2026-06-24T00:00:00+05:30"}).json()
    utc = api.get("/meters/J100000/readings", params={"to": "2026-06-23T18:30:00Z"}).json()
    assert ist["count"] == utc["count"] == 2


def test_from_after_to_is_rejected(api):
    response = api.get("/meters/J100000/readings", params={"from": "2026-06-30T00:00:00", "to": "2026-06-29T00:00:00"})
    assert_error(response, 422, "invalid_request")


def test_an_unknown_meter_never_reaches_the_portal(api, fake_portal):
    assert_error(api.get("/meters/J999999/readings"), 404, "meter_not_found")
    assert fake_portal.count("GET /portal/meters/") == 0


def test_a_portal_outage_gives_503_for_readings_but_cached_data_still_works(api, fake_portal):
    fake_portal.outage = True
    assert_error(api.get("/meters/J100000/readings"), 503, "portal_unavailable")
    assert api.get("/meters/J100000").status_code == 200
    assert api.get("/meters").status_code == 200


def test_an_expired_portal_session_is_invisible_to_api_users(api, fake_portal):
    fake_portal.expire_all_sessions()
    assert api.get("/meters/J100000/readings").status_code == 200
    assert fake_portal.login_count == 2


# ---------------------------------------------------------------- network
def test_transformer_list_uses_the_official_name_for_dt_007(api):
    body = api.get("/transformers").json()
    assert body["total"] == 40
    dt7 = next(item for item in body["items"] if item["code"] == "DT-007")
    assert dt7["name"] == "Sanganer DT 7" and dt7["meter_count"] == 13


def test_one_transformer(api):
    body = api.get("/transformers/dt-001").json()
    assert body["capacity_kva"] == 100 and body["feeder_code"] == "F-001"
    assert_error(api.get("/transformers/DT-999"), 404, "transformer_not_found")


def test_hierarchy_tree_and_depth(api):
    zones = api.get("/hierarchy", params={"depth": 1}).json()
    assert len(zones) == 3 and all(zone["children"] == [] for zone in zones)
    assert sum(zone["meter_count"] for zone in zones) == 403
    full = api.get("/hierarchy").json()
    assert full[0]["children"][0]["level"] == "circle"
    assert_error(api.get("/hierarchy", params={"depth": 9}), 422, "invalid_request")


def test_unknown_routes_use_the_standard_error_body(api):
    assert_error(api.get("/nope"), 404, "not_found")


# ---------------------------------------------------------------- documentation
def test_openapi_json_in_the_repo_describes_the_current_app():
    committed = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    current = create_app().openapi()
    assert set(committed["paths"]) == set(current["paths"])
    assert set(committed["components"]["schemas"]) == set(current["components"]["schemas"])
    assert committed["info"]["title"] == current["info"]["title"]
