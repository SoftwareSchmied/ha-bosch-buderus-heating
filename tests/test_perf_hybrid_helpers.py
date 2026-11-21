"""Synthetic hybrid plant for the heat pump basis tests."""

from __future__ import annotations

from tests.test_derived_values import CH, DHW, TOTAL, Plant

HS1_TOTAL = "/heatSources/hs1/emon/totalConsumption"
HS2_TOTAL = "/heatSources/hs2/emon/totalConsumption"
HS2_CH = "/heatSources/hs2/emon/chConsumption"
HS2_DHW = "/heatSources/hs2/emon/dhwConsumption"


def boiler_gateway(plant: Plant, *, heat: float, burner: float) -> Plant:
    """The gateway's collections count the boiler: its heat and its gas."""
    for path in (TOTAL, CH, DHW):
        plant.cloud(
            path,
            values=(
                {"outputProduced": heat, "compressor": 300.0, "eheater": 0.0},
                {"burner": burner},
            ),
        )
    return plant


def heat_pump(
    plant: Plant, path: str, *, heat: float, electricity: float, split: bool = True
) -> Plant:
    """The heat pump's own collection (heat and electricity)."""
    parts = (
        {"outputProduced": heat, "compressor": electricity, "eheater": 0.0}
        if split
        else {"outputProduced": heat, "electricity": electricity}
    )
    return plant.cloud(path, values=(parts,))
