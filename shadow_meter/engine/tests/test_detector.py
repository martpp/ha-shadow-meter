"""End-to-end test of the detection pipeline on a synthetic household."""

from __future__ import annotations

from typing import Any

import pytest

from shadow_meter.detection.pipeline import Detector, PhaseConfig

from .synthetic import T0, House, make_house

PHASES = [
    PhaseConfig("a", "sensor.phase_a", ("sensor.dishwasher",)),
    PhaseConfig("b", "sensor.phase_b"),
    PhaseConfig("c", "sensor.phase_c"),
]


def run(house: House, chunk_s: float = 3600) -> Detector:
    det = Detector(PHASES)
    t = T0
    end = T0 + house.days * 86400
    while t < end:
        part = {e: [s for s in v if t - 1 <= s.ts < t + chunk_s] for e, v in house.samples.items()}
        # like a real source: include the last value before the chunk
        for e, v in house.samples.items():
            before = [s for s in v if s.ts < t]
            if before:
                part[e].insert(0, before[-1])
        det.process(part, [c for c in house.changes if t <= c.ts < t + chunk_s], t, t + chunk_s)
        t += chunk_s
    return det


@pytest.fixture(scope="module")
def result() -> tuple[House, Detector, list[dict[str, Any]]]:
    house = make_house(days=3)
    det = run(house)
    det.maintain()
    return house, det, det.candidates()


def by(cands: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return [c for c in cands if all(c.get(k) == v for k, v in kw.items())]


def test_fridge_found(result: tuple[House, Detector, list[dict[str, Any]]]) -> None:
    house, _, cands = result
    fridge = by(cands, type="refrigerator")
    assert len(fridge) == 1
    f = fridge[0]
    assert f["phase"] == "a"
    assert f["power_w"] == pytest.approx(37, abs=4)
    assert f["confidence"] > 0.6
    assert f["features"]["count"] >= 0.8 * house.fridge_cycles


def test_kettle_found_on_phase_b(result: tuple[House, Detector, list[dict[str, Any]]]) -> None:
    _, _, cands = result
    heating = by(cands, type="heating")
    assert [c["phase"] for c in heating] == ["b"]
    assert heating[0]["power_w"] == pytest.approx(2200, rel=0.05)


def test_submetered_dishwasher_not_detected(result: tuple[House, Detector, list[dict[str, Any]]]) -> None:
    _, _, cands = result
    assert not [c for c in cands if c["phase"] == "a" and c["power_w"] > 1000]


def test_light_matched_to_entity(result: tuple[House, Detector, list[dict[str, Any]]]) -> None:
    _, _, cands = result
    light = by(cands, entity_id="light.hallway")
    assert len(light) == 1
    assert light[0]["phase"] == "c"
    assert light[0]["power_w"] == pytest.approx(21, abs=3)
    assert light[0]["confidence"] > 0.6


def test_state_roundtrip_and_resume(result: tuple[House, Detector, list[dict[str, Any]]]) -> None:
    _, det, cands = result
    restored = Detector(PHASES)
    restored.load(det.to_dict())
    assert [c["id"] for c in restored.candidates()] == [c["id"] for c in cands]
    # already processed time is not processed twice
    assert restored.process({}, [], T0, restored.processed_until) == []


def test_chunk_size_does_not_matter() -> None:
    house = make_house(days=1, seed=3)
    small = {c["id"]: round(c["power_w"]) for c in run(house, chunk_s=600).candidates()}
    large = {c["id"]: round(c["power_w"]) for c in run(house, chunk_s=86400).candidates()}
    assert small == large
