import pytest

from app.normalize import (
    LEVELS,
    parse_portal_time,
    parse_readings,
    path_string,
    resolve_transformer_paths,
    to_number,
)


# ---------------------------------------------------------------- times and numbers
def test_portal_times_are_read_as_ist():
    assert parse_portal_time("23/06/2026 23:30").isoformat() == "2026-06-23T23:30:00+05:30"


@pytest.mark.parametrize("raw, expected", [("48438.74", 48438.74), ("48440", 48440.0), (12, 12.0), (" 7.5 ", 7.5)])
def test_numbers_in_strings_become_numbers(raw, expected):
    assert to_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", None, "nan", "inf", True])
def test_things_that_are_not_numbers_become_none(raw):
    assert to_number(raw) is None


# ---------------------------------------------------------------- readings
def test_real_readings_get_consumption_from_the_running_total(energy):
    readings, skipped = parse_readings(energy["J100000"]["data"])
    assert (len(readings), skipped) == (337, 0)
    assert readings[0].consumption_kwh is None  # nothing before it to compare with
    assert readings[1].consumption_kwh == pytest.approx(0.42)
    assert readings[-1].timestamp.isoformat() == "2026-06-30T23:30:00+05:30"
    assert sum(r.consumption_kwh for r in readings[1:]) == pytest.approx(142.05)


def test_unsorted_duplicate_and_unusable_rows_are_handled():
    rows = [
        {"timestamp": "01/07/2026 00:30", "kwh": "10.5", "kvah": "11", "voltR": "230"},
        {"timestamp": "01/07/2026 00:00", "kwh": "10", "kvah": "11", "voltR": "230"},
        {"timestamp": "01/07/2026 00:00", "kwh": "10.1", "kvah": "11", "voltR": "230"},  # repeats a timestamp
        {"timestamp": "garbage", "kwh": "1"},
        {"kwh": "1"},
    ]
    readings, skipped = parse_readings(rows)
    assert skipped == 2
    assert [r.kwh for r in readings] == [10.1, 10.5]  # sorted, and the later duplicate won
    assert readings[1].consumption_kwh == pytest.approx(0.4)


def test_a_register_reset_never_gives_negative_consumption():
    rows = [{"timestamp": f"01/07/2026 0{i}:00", "kwh": kwh} for i, kwh in enumerate(["100", "5", "5.5"])]
    readings, _ = parse_readings(rows)
    assert [r.consumption_kwh for r in readings] == [None, None, 0.5]


def test_a_blank_value_gives_no_consumption_for_it_or_the_next_reading():
    rows = [{"timestamp": f"01/07/2026 0{i}:00", "kwh": kwh} for i, kwh in enumerate(["1", "", "3"])]
    readings, _ = parse_readings(rows)
    assert [r.kwh for r in readings] == [1.0, None, 3.0]
    assert [r.consumption_kwh for r in readings] == [None, None, None]


# ---------------------------------------------------------------- hierarchy
def test_every_blank_hierarchy_value_is_filled_in(export_rows, dt_rows):
    paths, notes = resolve_transformer_paths(export_rows, dt_rows)
    assert len(paths) == 40
    for levels in paths.values():
        assert [level.level for level in levels] == list(LEVELS)
        assert all(level.code and level.name for level in levels)
    assert "22 blank hierarchy values were filled in from other meters on the same transformer" in notes


def test_the_transformer_list_decides_the_name_of_dt_007(export_rows, dt_rows):
    paths, notes = resolve_transformer_paths(export_rows, dt_rows)
    assert paths["DT-007"][-1].name == "Sanganer DT 7"
    assert any("DT-007" in n and "Old Malviya Nagar Xfmr" in n and "3 of 13" in n for n in notes)


def test_a_feeder_code_used_twice_does_not_merge_two_feeders(export_rows, dt_rows):
    paths, _ = resolve_transformer_paths(export_rows, dt_rows)
    first, second = path_string(paths["DT-001"]), path_string(paths["DT-029"])
    assert "F-001" in first and "F-001" in second  # same feeder code...
    assert first != second  # ...but different places in the network


def test_a_missing_feeder_code_falls_back_to_the_transformer_list():
    meter = {"meterId": "J1", "dtCode": "DT-1", "hierarchy": {"feeder": {"code": "", "name": "Feeder 9"}}}
    paths, notes = resolve_transformer_paths([meter], [{"code": "DT-1", "name": "DT One", "feederCode": "F-9"}])
    feeder = paths["DT-1"][5]
    assert (feeder.code, feeder.name) == ("F-9", "Feeder 9")
    assert any("no zone code" in n for n in notes)  # the levels nobody knows are reported, not invented


def test_meters_that_disagree_use_the_most_common_value_and_say_so():
    def meter(i, name):
        return {"meterId": f"J{i}", "dtCode": "DT-1", "hierarchy": {"zone": {"code": "Z-1", "name": name}}}

    paths, notes = resolve_transformer_paths([meter(1, "North"), meter(2, "North"), meter(3, "Nord")], [])
    assert paths["DT-1"][0].name == "North"
    assert any("disagree about the zone name" in n for n in notes)
