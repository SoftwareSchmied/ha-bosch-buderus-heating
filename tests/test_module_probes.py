"""Targeted reads (module_probes): the rules R1 to R6 and their limits.

Pure tests with constructed answers: which paths each rule reads, when it
does not fire, the limit of 48 paths, the paths never read, entries known by
their name only and the diagnostics. Not yet confirmed on a real plant: a
hybrid system without a listed heat pump, a hot water circuit without a
list, a cascade, and ventilation, pool or solar found through a place.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from custom_components.bosch_buderus_heating.module_probes import (
    CASCADE_LEAVES,
    HOT_WATER_PATHS,
    HYBRID_PATHS,
    MAX_MODULE_PROBES,
    POOL_PATHS,
    SOLAR_PATHS,
    VENTILATION_PATHS,
    ModuleProbes,
    keep_times,
    result_class,
)
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.resource_catalog import is_never_read
from custom_components.bosch_buderus_heating.system_components import (
    load_table,
    parse_table,
)
from tests.system_components_variants import (
    each_name_entry_alone,
    each_name_entry_removed,
    raw_table,
)


def container(path: str, *children: str) -> Resource:
    return Resource(
        path,
        metadata=ResourceMetadata(resource_type="refEnum"),
        references=tuple(ResourceReference(f"{path}/{child}") for child in children),
    )


def value(path: str, item: object) -> Resource:
    return Resource(
        path,
        value=item,
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )


def info(*rows: tuple[int, int] | tuple[int, int, str]) -> Resource:
    return Resource(
        "/system/info",
        values=tuple(
            {
                "Id": str(row[0]),
                "Tok": str(row[1]),
                **({"Ver": row[2]} if len(row) > 2 else {}),
            }
            for row in rows
        ),
        has_values=True,
        metadata=ResourceMetadata(resource_type="systeminfo"),
    )


# A heating plant with a listed hot water circuit and heat source: no rule.
HEATING = (
    container("/heatingCircuits", "hc1"),
    container("/dhwCircuits", "dhw1"),
    container("/heatSources", "hs1"),
)


def probes(
    *answers: Resource | tuple[str, int],
    table: Any = "shipped",
    never_read: Any = is_never_read,
    limit: int = MAX_MODULE_PROBES,
) -> ModuleProbes:
    """A run that saw these answers; ``(path, status)`` for a failed read."""
    result = ModuleProbes(
        load_table() if table == "shipped" else table,
        never_read=never_read,
        limit=limit,
    )
    for answer in answers:
        if isinstance(answer, Resource):
            result.observe(answer.path, answer, 200)
        else:
            result.observe(answer[0], None, answer[1])
    return result


def planned(run: ModuleProbes, known: Iterable[str] = ()) -> dict[str, str]:
    seen = set(known)
    return {item.path: item.rule for item in run.plan(seen.__contains__)}


# ----------------------------------------------------------------------- R1


@pytest.mark.parametrize(
    ("answers", "evidence"),
    [
        (
            (
                value("/heatSources/hs1/type", "GasBoiler"),
                value("/heatSources/hybrid/outdoorStatus", "CONNECTED"),
            ),
            ["outdoor_status_connected"],
        ),
        ((info((8, 8), (248, 83, "05.00.00")),), ["tok83_id248"]),
        ((info((16, 83)),), ["tok83_id16"]),
        ((value("/system/type", "eHybrid"),), ["system_type_ehybrid"]),
        (
            (
                value("/heatSources/hs1/type", "Boiler"),
                value("/heatSources/hybrid/outdoorStatus", "CONNECTED"),
            ),
            ["outdoor_status_connected"],
        ),
    ],
)
def test_r1_reads_the_heat_pump_of_a_hybrid_system(
    answers: tuple[Resource, ...], evidence: list[str]
) -> None:
    # Reading does not depend on version or firmware (only the plant kind
    # does): an old version at the hybrid place still reads hs2.
    run = probes(*HEATING, *answers)
    assert planned(run) == dict.fromkeys(HYBRID_PATHS, "R1")
    rules = run.snapshot(lambda _path: "404")["rules"]
    assert rules["R1"]["evidence"] == evidence  # type: ignore[index]
    assert rules["R1"]["area"] == "hybrid"  # type: ignore[index]


@pytest.mark.parametrize(
    "answers",
    [
        # A heat pump at hs1 with the hybrid control connected: no boiler.
        (
            value("/heatSources/hs1/type", "Heatpump"),
            value("/heatSources/hybrid/outdoorStatus", "CONNECTED"),
        ),
        # A boiler whose outdoor unit is not connected.
        (
            value("/heatSources/hs1/type", "GasBoiler"),
            value("/heatSources/hybrid/outdoorStatus", "NOT_CONNECTED"),
        ),
        # Another type at the hybrid place, a hybrid manager elsewhere.
        (info((999, 83), (248, 16)),),
        (value("/system/type", "heatpump_single"),),
    ],
)
def test_r1_does_not_fire_without_hybrid_evidence(
    answers: tuple[Resource, ...],
) -> None:
    assert planned(probes(*HEATING, *answers)) == {}


def test_a_not_connected_outdoor_unit_overrules_the_hybrid_place() -> None:
    run = probes(
        *HEATING,
        info((248, 83, "06.02.00")),
        value("/heatSources/hybrid/outdoorStatus", "NOT_CONNECTED"),
    )
    assert planned(run) == {}
    assert run.snapshot(lambda _path: "ok")["conflicts"] == [
        "outdoor_status_not_connected"
    ]


def test_r1_reads_only_what_the_discovery_does_not_know() -> None:
    known = ("/heatSources/hs2/defrostActive", "/heatSources/actualHeatDemand")
    run = probes(*HEATING, value("/system/type", "eHybrid"))
    assert set(planned(run, known)) == set(HYBRID_PATHS) - set(known)


# ------------------------------------------------------------------- R2-R4


@pytest.mark.parametrize(
    ("place", "refused", "paths", "rule"),
    [
        (17, ("/pool", 403), POOL_PATHS, "R2"),
        (17, ("/pool", 404), POOL_PATHS, "R2"),
        (81, ("/ventilation", 500), VENTILATION_PATHS, "R3"),
        (81, container("/ventilation", "operationModes"), VENTILATION_PATHS, "R3"),
        (48, ("/solarCircuits", 403), SOLAR_PATHS, "R4"),
        (48, container("/solarCircuits"), SOLAR_PATHS, "R4"),
    ],
)
def test_a_place_reads_its_area_when_the_gateway_does_not_list_it(
    place: int, refused: Any, paths: tuple[str, ...], rule: str
) -> None:
    run = probes(*HEATING, info((8, 8), (231, place)), refused)
    assert planned(run) == dict.fromkeys(paths, rule)
    assert run.snapshot(lambda _path: "ok")["rules"][rule]["evidence"] == [  # type: ignore[index]
        f"tok{place}"
    ]


@pytest.mark.parametrize(
    ("place", "listed"),
    [
        (17, container("/pool", "enabled")),
        (81, container("/ventilation", "zone1")),
        (48, container("/solarCircuits", "sc1")),
    ],
)
def test_a_listed_area_needs_no_targeted_read(place: int, listed: Resource) -> None:
    assert planned(probes(*HEATING, info((231, place)), listed)) == {}


def test_an_area_without_its_place_is_not_read() -> None:
    run = probes(
        *HEATING,
        info((8, 8), (253, 16)),
        ("/pool", 403),
        ("/ventilation", 500),
        ("/solarCircuits", 403),
    )
    assert planned(run) == {}


def test_remote_controls_and_mixing_modules_lead_to_no_read() -> None:
    rows = [(165, tok) for tok in (56, 57, 58, 59)]
    rows += [(69, 8), (69, 9), (72, 24), (240, 16)]
    run = probes(
        *HEATING,
        info(*rows),
        container("/heatingCircuits", "hc1"),
    )
    assert planned(run) == {}


def test_entries_known_by_their_name_only_change_no_read() -> None:
    """A place whose basis is a name, or a type without a role at the hybrid
    place (roles exist only with another basis), never leads to a read."""
    raw = raw_table()
    for item in raw["places"]:
        if item["place"] in (17, 48, 81, 83):
            item["basis"] = "name"
    table = parse_table(raw)
    assert table is not None
    answers_83 = (*HEATING, info((248, 83, "06.02.00"), (16, 83)))
    assert planned(probes(*answers_83, table=table)) == {}
    raw = raw_table()
    raw["roles"] = [item for item in raw["roles"] if item["place"] != 83]
    assert planned(probes(*answers_83, table=parse_table(raw))) == {}
    raw = raw_table()
    for item in raw["places"]:
        if item["place"] in (17, 48, 81, 83):
            item["basis"] = "name"
    table = parse_table(raw)
    assert table is not None
    answers = (
        *HEATING,
        info((248, 83, "06.02.00"), (16, 83), (231, 17), (231, 48), (231, 81)),
        ("/pool", 403),
        ("/ventilation", 403),
        ("/solarCircuits", 403),
    )
    assert planned(probes(*answers, table=table)) == {}
    shipped = planned(probes(*answers))
    assert {rule for rule in shipped.values()} == {"R1", "R2", "R3", "R4"}


def test_name_entries_removed_or_alone_give_the_same_reads() -> None:
    """I5: the reads never depend on an entry known by its name only."""
    answers = (
        *HEATING,
        info((248, 83, "06.02.00"), (231, 17), (231, 48), (231, 81), (165, 56)),
        ("/pool", 403),
        ("/ventilation", 403),
        ("/solarCircuits", 403),
    )
    expected = planned(probes(*answers))
    for _type_id, table in (*each_name_entry_removed(), *each_name_entry_alone()):
        assert planned(probes(*answers, table=table)) == expected


def test_without_a_table_no_place_leads_to_a_read() -> None:
    run = probes(*HEATING, info((248, 83), (231, 17)), ("/pool", 403), table=None)
    assert planned(run) == {}


# ----------------------------------------------------------------------- R5


@pytest.mark.parametrize(
    "hot_water",
    [container("/dhwCircuits"), ("/dhwCircuits", 403), ("/dhwCircuits", 404)],
)
def test_r5_asks_for_hot_water_when_no_circuit_is_listed(hot_water: Any) -> None:
    run = probes(container("/heatingCircuits", "hc1"), hot_water)
    assert planned(run) == dict.fromkeys(HOT_WATER_PATHS, "R5")
    assert (
        run.circuit_answered("/dhwCircuits/dhw2/operationMode") == "/dhwCircuits/dhw2"
    )
    assert run.circuit_answered("/dhwCircuits/dhw1/name") is None


@pytest.mark.parametrize(
    "answers",
    [
        # Variant B: a listed circuit needs no probe.
        (container("/heatingCircuits", "hc1"), container("/dhwCircuits", "dhw1")),
        (container("/heatSources", "hs1"), container("/dhwCircuits", "dhw2")),
        # No heating at all (an air conditioner, an empty gateway).
        (container("/dhwCircuits"),),
        # The hot water list was never asked for.
        (container("/heatingCircuits", "hc1"),),
    ],
)
def test_r5_does_not_fire_with_a_listed_circuit_or_without_heating(
    answers: tuple[Resource, ...],
) -> None:
    assert planned(probes(*answers)) == {}


# ----------------------------------------------------------------------- R6


def test_r6_reads_the_unlisted_heat_pumps_of_a_cascade() -> None:
    run = probes(
        *HEATING,
        value("/system/type", "heatpump_cascade"),
        value("/heatSources/numberOfRefrigerantCircuitsInstalled", 3),
    )
    assert planned(run) == {
        f"/heatSources/hs{index}/{leaf}": "R6"
        for index in (2, 3)
        for leaf in CASCADE_LEAVES
    }
    assert run.snapshot(lambda _path: "ok")["rules"]["R6"]["evidence"] == [  # type: ignore[index]
        "system_type_cascade:3"
    ]


@pytest.mark.parametrize(
    ("system_type", "circuits"),
    [
        ("heatpump_cascade", 1),
        ("heatpump_cascade", 7),
        ("heatpump_cascade", "two"),
        # A superscript three and an Arabic-Indic three: no ASCII digit.
        ("heatpump_cascade", chr(0xB3)),
        ("heatpump_cascade", chr(0x663)),
        ("heatpump_single", 3),
    ],
)
def test_r6_needs_a_cascade_of_two_to_six(system_type: str, circuits: object) -> None:
    run = probes(
        *HEATING,
        value("/system/type", system_type),
        value("/heatSources/numberOfRefrigerantCircuitsInstalled", circuits),
    )
    assert planned(run) == {}


# ------------------------------------------------------------------ limits


def _everything() -> tuple[Any, ...]:
    """Every rule at once: 5 + 4 + 9 + 5 + 2 + 24 = 49 paths, one shared."""
    return (
        container("/heatingCircuits", "hc1"),
        container("/dhwCircuits"),
        container("/heatSources"),
        info((248, 83, "06.02.00"), (231, 17), (231, 48), (231, 81)),
        ("/pool", 403),
        ("/ventilation", 403),
        ("/solarCircuits", 403),
        value("/system/type", "heatpump_cascade"),
        value("/heatSources/numberOfRefrigerantCircuitsInstalled", 6),
    )


def test_every_rule_at_once_stays_within_48_paths_each_once() -> None:
    run = probes(*_everything())
    first = planned(run)
    # hs2's starts belong to R1 and R6: read once.
    assert len(first) == MAX_MODULE_PROBES
    assert first["/heatSources/hs2/numberOfStarts"] == "R1"
    assert not run.over_limit
    # Asked again (after more answers), nothing is read twice.
    assert planned(run) == {}
    snapshot = run.snapshot(lambda _path: "404")
    assert snapshot["planned"] == 48 and snapshot["over_limit"] == 0
    results = run.results(lambda _path: "404", 7)
    assert len(results) == 48
    assert {item["result"] for item in results} == {"404"}


def test_a_lower_limit_is_kept() -> None:
    run = probes(*_everything(), limit=3)
    assert len(planned(run)) == 3
    assert len(run.over_limit) == 45
    assert planned(run) == {}
    results = run.results(lambda _path: "ok", 1)
    assert [item["result"] for item in results] == ["ok"] * 3


def test_paths_never_read_are_never_planned() -> None:
    blocked = {"/pool/enabled", HOT_WATER_PATHS[0]}
    run = probes(
        container("/heatingCircuits", "hc1"),
        container("/dhwCircuits"),
        info((231, 17)),
        ("/pool", 403),
        never_read=blocked.__contains__,
    )
    found = planned(run)
    assert not blocked & set(found)
    assert set(found) == {*POOL_PATHS, *HOT_WATER_PATHS} - blocked
    assert run.snapshot(lambda _path: "ok")["never_read"] == 2


@pytest.mark.parametrize("reason", ["icom", "room_controller", "water_softener"])
def test_no_reads_where_the_rules_do_not_apply(reason: str) -> None:
    run = probes(*_everything())
    run.deactivate(reason)
    run.deactivate("other")
    assert planned(run) == {}
    snapshot = run.snapshot(lambda _path: "ok")
    assert snapshot["active"] is False and snapshot["inactive_reason"] == reason
    assert not run.evaluated


# -------------------------------------------------------------- diagnostics


@pytest.mark.parametrize(
    ("bulk", "fallback", "discovered", "policy", "expected"),
    [
        ("success", None, True, "normal", "ok"),
        ("http_404", None, False, "normal", "404"),
        ("http_403", None, False, "normal", "403"),
        ("http_406", None, False, "normal", "403"),
        ("http_500", "http_500", False, "optional_5xx_assumed_missing", "5xx"),
        ("http_502", None, False, "normal", "5xx"),
        ("http_500", "service_unavailable", False, "normal", "5xx"),
        ("http_429", None, False, "normal", "429"),
        ("rate_limited", None, False, "normal", "429"),
        ("not_attempted", None, False, "module_probe_limit", "skipped"),
        ("pending", None, False, "normal", "skipped"),
        ("http_428", None, False, "normal", "other"),
        ("timeout", None, False, "normal", "other"),
    ],
)
def test_result_classes(
    bulk: str, fallback: str | None, discovered: bool, policy: str, expected: str
) -> None:
    assert result_class(bulk, fallback, discovered, policy) == expected


def test_listing_refs_and_area_check_show_no_values() -> None:
    run = probes(
        container("/heatingCircuits", "hc1", "hc2", "list"),
        container("/dhwCircuits"),
        ("/pool", 403),
        info((231, 17), (8, 8), (165, 56), (231, 81)),
        container("/ventilation", "zone1"),
    )
    assert run.listing_refs() == {
        "heatingCircuits": ["hc1", "hc2"],
        "dhwCircuits": [],
        "ventilation": ["zone1"],
        "pool": "http_403",
    }
    check = run.area_check(lambda area: area == "/ventilation/zone1")
    assert check == {
        "components": 4,
        "areas": [
            {"token": 17, "id": 231, "place": "pool", "area": "/pool", "found": False},
            {
                "token": 81,
                "id": 231,
                "place": "ventilation",
                "area": "/ventilation/zone1",
                "found": True,
            },
        ],
        "remote_places": 1,
        "heating_circuits_listed": 2,
    }


def test_an_interrupted_run_says_why() -> None:
    run = probes(container("/heatingCircuits", "hc1"), container("/dhwCircuits"))
    planned(run)
    run.interrupt("rate_limited")
    snapshot = run.snapshot(
        lambda path: "429" if path.endswith("dhw1/operationMode") else "skipped"
    )
    assert snapshot["stopped"] == "rate_limited"
    assert snapshot["rules"]["R5"]["results"] == {"429": 1, "skipped": 1}  # type: ignore[index]
    assert snapshot["results"] == [
        {"path": HOT_WATER_PATHS[0], "rule": "R5", "result": "429"},
        {"path": HOT_WATER_PATHS[1], "rule": "R5", "result": "skipped"},
    ]
    assert snapshot["restored"] is False


def test_an_unchanged_result_keeps_its_time() -> None:
    before = [
        {"path": "/a", "rule": "R5", "result": "404", "at": 10},
        {"path": "/b", "rule": "R5", "result": "404", "at": 10},
    ]
    now = [
        {"path": "/a", "rule": "R5", "result": "404", "at": 99},
        {"path": "/b", "rule": "R5", "result": "ok", "at": 99},
    ]
    assert keep_times(before, now) == [
        {"path": "/a", "rule": "R5", "result": "404", "at": 10},
        {"path": "/b", "rule": "R5", "result": "ok", "at": 99},
    ]
