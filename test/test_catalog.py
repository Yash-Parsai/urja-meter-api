import pytest

from app.catalog import build_snapshot, prune_tree, search_meters
from app.errors import PortalProtocolError, PortalUnavailable


# ---------------------------------------------------------------- building
def test_the_snapshot_holds_everything(catalog):
    snapshot = catalog.snapshot()
    assert len(snapshot.meters) == 403
    assert len(snapshot.transformers) == 40
    assert sum(t.meter_count for t in snapshot.transformers) == 403


def test_a_meter_that_had_blanks_comes_out_complete(catalog):
    meter = catalog.lookup("J100011")  # one of the 22 meters with an empty hierarchy value
    assert meter is not None
    assert all(level.code and level.name for level in meter.hierarchy)
    assert meter.transformer.code == meter.transformer_code == "DT-012"


def test_locations_are_numbers(catalog):
    meter = catalog.lookup("J100000")
    assert (meter.location.latitude, meter.location.longitude) == (26.938961002479868, 75.83095696146852)


def test_the_tree_adds_up_and_has_unique_paths(catalog):
    tree = catalog.snapshot().tree
    assert len(tree) == 3
    assert sum(zone.meter_count for zone in tree) == 403
    paths, leaves = [], 0

    def walk(nodes):
        nonlocal leaves
        for node in nodes:
            paths.append(node.path)
            if not node.children:
                assert node.level == "dt"
                leaves += 1
            walk(node.children)

    walk(tree)
    assert leaves == 40
    assert len(paths) == len(set(paths))


def test_division_codes_reused_under_different_circles_stay_separate_nodes(catalog):
    divisions = []

    def collect(nodes):
        for node in nodes:
            if node.level == "division":
                divisions.append(node)
            collect(node.children)

    collect(catalog.snapshot().tree)
    assert len({d.code for d in divisions}) == 10  # ten codes...
    assert len(divisions) > 10  # ...but more than ten real divisions


def test_prune_keeps_only_the_requested_depth(catalog):
    zones = prune_tree(list(catalog.snapshot().tree), 1)
    assert len(zones) == 3 and all(zone.children == [] for zone in zones)


def test_bad_rows_are_reported_not_fatal(dt_rows):
    rows = [{"meterId": "J1", "dtCode": "DT-001"}, {"meterId": "J1", "dtCode": "DT-001"}, {"dtCode": "DT-001"}]
    snapshot = build_snapshot(rows, dt_rows, loaded_at=0)
    assert len(snapshot.meters) == 1
    assert any("appears twice" in note for note in snapshot.notes)
    assert any("without a meterId" in note for note in snapshot.notes)


# ---------------------------------------------------------------- searching
def test_filters_count_what_the_export_says(catalog):
    snapshot = catalog.snapshot()
    assert len(search_meters(snapshot, status="faulty")) == 90
    assert len(search_meters(snapshot, phase="THREE")) == 126
    assert len(search_meters(snapshot, make="hpl")) == 90
    assert len(search_meters(snapshot, build="v2")) == 165
    assert len(search_meters(snapshot, install_type="ct operated")) == 198
    assert len(search_meters(snapshot, transformer="dt-007")) == 13


def test_text_search_ignores_case_and_matches_part_of_an_id_or_serial(catalog):
    snapshot = catalog.snapshot()
    assert len(search_meters(snapshot, q="j10000")) == 10
    assert len(search_meters(snapshot, q="0000")) == 10
    assert [m.meter_id for m in search_meters(snapshot, q="se33962")] == ["J100000"]


def test_filters_combine_with_and(catalog):
    snapshot = catalog.snapshot()
    both = search_meters(snapshot, status="Faulty", phase="three")
    assert 0 < len(both) < 90
    assert all(m.install_status == "Faulty" and m.phase_type == "three" for m in both)


def test_node_filter_matches_everything_below_a_hierarchy_path(catalog):
    snapshot = catalog.snapshot()
    zone = snapshot.tree[0]
    assert len(search_meters(snapshot, node=zone.path)) == zone.meter_count
    assert len(search_meters(snapshot, node=zone.children[0].path)) == zone.children[0].meter_count
    assert search_meters(snapshot, node=zone.path[:-1]) == []  # half a code is not a node


def test_meters_near_a_point_come_closest_first(catalog):
    snapshot = catalog.snapshot()
    spot = catalog.lookup("J100000").location
    found = search_meters(snapshot, near=(spot.latitude, spot.longitude, 3.0))
    assert found[0].meter_id == "J100000" and found[0].distance_km == 0
    distances = [m.distance_km for m in found]
    assert distances == sorted(distances) and max(distances) <= 3.0
    assert 1 < len(found) < 403


# ---------------------------------------------------------------- the cache
def test_data_is_reused_until_the_ttl_runs_out(catalog, fake_portal, clock):
    catalog.snapshot()
    catalog.snapshot()
    assert fake_portal.count("GET /portal/export") == 1
    clock.advance(601)
    catalog.snapshot()
    assert fake_portal.count("GET /portal/export") == 2


def test_old_data_is_served_when_a_refresh_fails_and_the_portal_is_left_alone_for_a_while(catalog, fake_portal, clock):
    first = catalog.snapshot()
    clock.advance(601)
    fake_portal.outage = True
    assert catalog.snapshot() is first  # stale, but better than an error
    assert catalog.status().stale is True
    calls_after_failure = len(fake_portal.calls)
    assert catalog.snapshot() is first
    assert len(fake_portal.calls) == calls_after_failure  # backing off: no new portal calls
    clock.advance(31)
    fake_portal.outage = False
    assert catalog.snapshot() is not first  # recovered
    assert catalog.status().stale is False


def test_the_very_first_load_failing_is_an_error(catalog, fake_portal):
    fake_portal.outage = True
    with pytest.raises(PortalUnavailable):
        catalog.snapshot()


def test_an_unknown_meter_reloads_the_data_at_most_every_30_seconds(catalog, fake_portal, clock):
    catalog.snapshot()
    assert catalog.lookup("J999999") is None
    assert fake_portal.count("GET /portal/export") == 1  # data is only seconds old: no reload
    clock.advance(31)
    assert catalog.lookup("J999999") is None
    assert fake_portal.count("GET /portal/export") == 2
    assert catalog.lookup("J999999") is None
    assert fake_portal.count("GET /portal/export") == 2  # and not again straight away


def test_ids_are_matched_ignoring_case_and_spaces(catalog):
    assert catalog.lookup(" j100000 ").meter_id == "J100000"


def test_data_in_an_unexpected_shape_is_a_protocol_error(catalog, fake_portal):
    fake_portal.export_rows = [1, 2, 3]
    with pytest.raises(PortalProtocolError):
        catalog.snapshot()
