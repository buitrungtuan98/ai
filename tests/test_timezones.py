"""Unit tests for the curated timezone picker catalog (core/timezones.py)."""
from core import timezones


def test_tz_choices_grouped_with_vietnam_first():
    groups = timezones.tz_choices()
    assert groups and groups[0][0] == "Asia · Pacific"
    first_zone = groups[0][1][0]
    assert first_zone[0] == "Asia/Ho_Chi_Minh"
    assert "Việt Nam" in first_zone[1] and "UTC+07:00" in first_zone[1]
    # Every option is (iana, label) with a live UTC offset baked into the label.
    for _region, zones in groups:
        for iana, label in zones:
            assert timezones.is_valid(iana)
            assert "UTC" in label


def test_offset_label_format():
    assert timezones.offset_label("Asia/Ho_Chi_Minh") == "UTC+07:00"
    assert timezones.offset_label("UTC") == "UTC+00:00"
    assert timezones.offset_label("Not/AZone") == ""  # bad zone → empty, never raises


def test_is_valid():
    assert timezones.is_valid("Asia/Ho_Chi_Minh")
    assert timezones.is_valid("UTC")
    assert not timezones.is_valid("Asia/HoChiMinh")   # common typo
    assert not timezones.is_valid("Middle/Earth")


def test_known_set_matches_catalog():
    catalog = {iana for _r, zones in timezones.tz_choices() for iana, _l in zones}
    assert timezones.KNOWN == catalog
