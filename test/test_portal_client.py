from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.errors import PortalAuthError, PortalNotFound, PortalUnavailable
from app.portal import PortalClient


def test_logs_in_once_and_reuses_the_session(portal, fake_portal):
    portal.read_energy("J100000")
    portal.read_energy("J100000")
    portal.read_energy("J100004")
    assert fake_portal.login_count == 1


def test_a_rejected_login_is_an_auth_error(portal, fake_portal):
    fake_portal.password = "something-else"
    with pytest.raises(PortalAuthError):
        portal.read_energy("J100000")


def test_logs_in_again_shortly_before_the_session_runs_out(portal, fake_portal, clock):
    portal.read_energy("J100000")
    clock.advance(3600 - 299)  # inside the 300 s safety margin
    portal.read_energy("J100000")
    assert fake_portal.login_count == 2


def test_logs_in_again_when_the_portal_ends_the_session_early(portal, fake_portal):
    portal.read_energy("J100000")
    fake_portal.expire_all_sessions()
    assert portal.read_energy("J100000")  # the caller never notices
    assert fake_portal.login_count == 2


def test_session_info_counts_down(portal, clock):
    assert portal.session_info() == {"logged_in": False, "expires_in_seconds": None}
    portal.read_energy("J100000")
    clock.advance(100)
    assert portal.session_info() == {"logged_in": True, "expires_in_seconds": 3500}


def test_export_is_a_single_signed_call_that_returns_every_meter(portal, fake_portal, export_rows):
    rows = portal.export_meters()
    assert len(rows) == len(export_rows) == 403
    assert fake_portal.count("GET /portal/export") == 1  # never loops over `page`
    assert fake_portal.calls.count("GET /portal/export?page=1") == 1  # the signed query is exactly what the portal's own page sends
    assert fake_portal.count("GET /portal/keys") == 1


def test_recovers_when_the_signing_secret_changes(portal, fake_portal):
    portal.export_meters()
    fake_portal.secret = "rotated-secret"
    assert len(portal.export_meters()) == 403
    assert fake_portal.count("GET /portal/export") == 3  # ok, refused, ok again with the new secret


def test_a_badly_skewed_portal_clock_is_corrected_from_the_date_header(portal, fake_portal):
    fake_portal.clock_skew = 3 * 3600  # the portal's clock is 3 hours ahead of ours
    assert len(portal.export_meters()) == 403
    assert fake_portal.count("GET /portal/export") == 1  # no failed attempt was needed


def test_gives_up_instead_of_retrying_forever_when_signatures_are_always_refused(portal, fake_portal):
    fake_portal.reject_all_signatures = True
    with pytest.raises(PortalAuthError):
        portal.export_meters()
    assert fake_portal.count("GET /portal/export") == 3


def test_unknown_meter_is_not_found(portal):
    with pytest.raises(PortalNotFound):
        portal.read_energy("J999999")


def test_meter_ids_cannot_change_the_request_path(portal, fake_portal):
    with pytest.raises(PortalNotFound):
        portal.read_energy("../../login")
    assert "/portal/meters/..%2F..%2Flogin/energy" in fake_portal.raw_targets  # slashes were escaped


def test_portal_down_is_reported_as_unavailable(portal, fake_portal):
    fake_portal.outage = True
    with pytest.raises(PortalUnavailable):
        portal.read_energy("J100000")


def _flaky_client(settings, fake_portal, clock, failures):
    state = {"left": failures}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and state["left"] > 0:
            state["left"] -= 1
            raise httpx.ReadTimeout("too slow", request=request)
        return fake_portal.handle(request)

    return PortalClient(settings, transport=httpx.MockTransport(handler), clock=clock, retry_delay=0)


def test_one_network_blip_is_retried(settings, fake_portal, clock):
    client = _flaky_client(settings, fake_portal, clock, failures=1)
    assert client.read_energy("J100000")


def test_a_network_that_stays_down_is_reported(settings, fake_portal, clock):
    client = _flaky_client(settings, fake_portal, clock, failures=99)
    with pytest.raises(PortalUnavailable):
        client.read_energy("J100000")


def test_transformer_list_reads_every_page_and_then_stops(portal, fake_portal):
    assert len(portal.list_transformers()) == 40
    assert fake_portal.count("GET /portal/dts") == 2


def test_transformer_list_stops_when_the_portal_repeats_a_page(portal, fake_portal, dt_pages):
    fake_portal.dt_pages = [dt_pages[0]] * 5
    assert len(portal.list_transformers()) == 20
    assert fake_portal.count("GET /portal/dts") == 2


def test_many_threads_hitting_an_expired_session_log_in_only_once(portal, fake_portal):
    portal.read_energy("J100000")
    fake_portal.expire_all_sessions()
    fake_portal.latency = 0.01
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: portal.read_energy("J100000"), range(8)))
    assert all(results)
    assert fake_portal.login_count == 2  # the first login plus exactly one more
