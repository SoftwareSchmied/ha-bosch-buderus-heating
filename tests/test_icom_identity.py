"""Recognising a gateway by values it shares with the cloud (no usable own ID)."""

import pytest

from custom_components.bosch_buderus_heating import icom_identity as ident
from custom_components.bosch_buderus_heating.icom_access import IDENTITY_UUID_PATH
from custom_components.bosch_buderus_heating.icom_identity import (
    CloudReading,
    Verdict,
    compare,
    fingerprint,
    text_of,
)
from custom_components.bosch_buderus_heating.icom_plan import UUID_PATH
from custom_components.bosch_buderus_heating.observations import PAIR_CATALOG

FIRMWARE, HARDWARE = ident.FIRMWARE_PATH, ident.HARDWARE_PATH
OUTDOOR, PRESSURE, STARTS = ident.OUTDOOR_PATH, ident.PRESSURE_PATH, ident.STARTS_PATH


def reading(value, age=60.0):
    return CloudReading(value, age)


def verdict(local, cloud):
    return compare(local, cloud).verdict


def test_every_compared_path_is_one_the_pair_catalog_has_on_both_sides():
    templates = {pair.template for pair in PAIR_CATALOG}
    assert set(ident.FALLBACK_PATHS) <= templates
    assert UUID_PATH == IDENTITY_UUID_PATH
    assert not any("{" in path for path in ident.FALLBACK_PATHS)


def test_two_matches_with_a_live_one_confirm():
    local = {FIRMWARE: "01.02.03", OUTDOOR: 4.5}
    cloud = {FIRMWARE: reading("01.02.03"), OUTDOOR: reading(5.0)}
    outcome = compare(local, cloud)
    assert (outcome.verdict, outcome.matches) == (Verdict.CONFIRMED, 2)


def test_static_values_alone_never_confirm():
    local = {FIRMWARE: "1", HARDWARE: "hw", ident.BRAND_PATH: "b"}
    cloud = {
        FIRMWARE: reading("1"),
        HARDWARE: reading("HW "),
        ident.BRAND_PATH: reading("b"),
    }
    outcome = compare(local, cloud)
    assert (outcome.verdict, outcome.matches) == (Verdict.INSUFFICIENT, 3)


def test_one_live_match_alone_is_not_enough():
    assert verdict({OUTDOOR: 4.5}, {OUTDOOR: reading(4.0)}) is Verdict.INSUFFICIENT


def test_the_counter_of_starts_counts_as_a_live_match():
    local = {HARDWARE: "hw", STARTS: 1002}
    assert verdict(local, {HARDWARE: reading("hw"), STARTS: reading(1000)}) is (
        Verdict.CONFIRMED
    )


@pytest.mark.parametrize(
    ("local", "cloud"),
    [
        ({FIRMWARE: "1.0"}, {FIRMWARE: reading("2.0")}),
        ({HARDWARE: "a"}, {HARDWARE: reading("b")}),
        ({OUTDOOR: 4.5}, {OUTDOOR: reading(10.0)}),
        ({OUTDOOR: 4.5}, {OUTDOOR: reading(-1.0)}),
        ({PRESSURE: 1.6}, {PRESSURE: reading(3.0)}),
        ({STARTS: 5}, {STARTS: reading(9)}),  # a counter never falls
        ({STARTS: 500}, {STARTS: reading(1)}),
    ],
)
def test_a_clear_contradiction_wins_over_matches(local, cloud):
    local = {FIRMWARE: "1.0", HARDWARE: "a", OUTDOOR: 4.5, **local}
    cloud = {
        FIRMWARE: reading("1.0"),
        HARDWARE: reading("a"),
        OUTDOOR: reading(4.5),
        **cloud,
    }
    assert verdict(local, cloud) is Verdict.CONTRADICTION


def test_the_tolerance_has_a_gap_between_agreeing_and_contradicting():
    base = {FIRMWARE: "1"}, {FIRMWARE: reading("1")}
    for cloud_value, expected in (
        (6.4, Verdict.CONFIRMED),  # 1.9 apart
        (7.5, Verdict.INSUFFICIENT),  # 3.0 apart: neither agree nor contradict
        (9.6, Verdict.CONTRADICTION),  # 5.1 apart
    ):
        local = {**base[0], OUTDOOR: 4.5}
        cloud = {**base[1], OUTDOOR: reading(cloud_value)}
        assert verdict(local, cloud) is expected, cloud_value


@pytest.mark.parametrize("age", [None, 901.0, 5000.0, -3600.0])
def test_a_cloud_reading_that_is_not_recent_says_nothing(age):
    local = {FIRMWARE: "1", OUTDOOR: 4.5}
    cloud = {FIRMWARE: reading("1"), OUTDOOR: reading(40.0, age)}
    # Not a contradiction either: it is only left out.
    assert compare(local, cloud).verdict is Verdict.INSUFFICIENT


@pytest.mark.parametrize("bad", [None, True, "4.5", float("nan"), float("inf"), []])
def test_unusable_numbers_are_left_out(bad):
    local = {FIRMWARE: "1", OUTDOOR: bad}
    cloud = {FIRMWARE: reading("1"), OUTDOOR: reading(4.5)}
    assert verdict(local, cloud) is Verdict.INSUFFICIENT


def test_texts_are_compared_without_case_and_spacing():
    assert text_of("  V01.02 \n") == "v01.02"
    assert text_of("") is None and text_of(5) is None and text_of("x" * 500) is None


def test_the_fingerprint_prefers_the_hardware_over_the_firmware():
    assert fingerprint({FIRMWARE: "1", HARDWARE: "Hw"}) == (HARDWARE, "hw")
    assert fingerprint({FIRMWARE: "1", HARDWARE: -1}) == (FIRMWARE, "1")
    assert fingerprint({OUTDOOR: 1}) is None
