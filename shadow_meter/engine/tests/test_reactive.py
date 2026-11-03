"""Optional power factor: reactive power of switch-on steps, its use in clustering and typing, setup."""

from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
import pytest

from shadow_meter.detection.classify import classify
from shadow_meter.detection.clusters import Cluster, ClusterSet
from shadow_meter.detection.pipeline import Detector, PhaseConfig
from shadow_meter.detection.reactive import reactive_power
from shadow_meter.engine import Engine
from shadow_meter.models import Cycle, Sample
from shadow_meter.settings import Options, Setup
from shadow_meter.setup import power_factor_sensors, suggest_power_factors
from shadow_meter.store import Store

from .synthetic import make_house
from .test_app import FakeHA, client_for, runtime

DAY = 86400.0
T0 = 1_750_000_000.0


def test_reactive_power_from_power_factor() -> None:
    p = np.array([100.0, 100.0, 100.0, 2000.0])
    pf = np.array([100.0, 60.0, 0.6, -100.0])  # %, %, fraction, negative sign (direction) is ignored
    q = reactive_power(p, pf)
    assert q[0] == pytest.approx(0.0)
    assert q[1] == pytest.approx(100 * math.sqrt(1 / 0.36 - 1))
    assert q[2] == pytest.approx(q[1])
    assert q[3] == pytest.approx(0.0)


def _household(days: int = 4, seed: int = 3) -> tuple[dict[str, list[Sample]], dict[str, float]]:
    """One phase: base load, a fridge (40 W, dQ/dP 0.22) every hour and a heater (2000 W, resistive).

    Power is reported every 5 s, the power factor only every 20 s (like Home Assistant records it)."""
    rnd = random.Random(seed)
    ts = np.arange(T0, T0 + days * DAY, 5.0)
    p = 120.0 + np.array([rnd.gauss(0, 1.5) for _ in ts])
    q = np.full(ts.size, 40.0)  # base load: some reactive power
    for k in range(int(days * 24)):  # fridge: 15 min every hour
        on = (ts >= T0 + k * 3600 + 600) & (ts < T0 + k * 3600 + 1500)
        p[on] += 40.0
        q[on] += 0.22 * 40.0
    for k in range(int(days * 3)):  # heater: 6 min three times a day, never together with the fridge
        start = T0 + k * 8 * 3600 + 1800
        on = (ts >= start) & (ts < start + 360)
        p[on] += 2000.0
    pf = p / np.sqrt(p**2 + q**2) * 100
    power = [Sample(float(t), float(v)) for t, v in zip(ts, p, strict=True)]
    factor = [Sample(float(t), round(float(v), 2)) for t, v in zip(ts[::4], pf[::4], strict=True)]
    return {"sensor.mains": power, "sensor.mains_pf": factor}, {"end": float(ts[-1] + 5)}


def _run(with_pf: bool, chunk_s: float = DAY) -> Detector:
    samples, info = _household()
    cfg = PhaseConfig("A", "sensor.mains", (), "sensor.mains_pf" if with_pf else None)
    det = Detector([cfg])
    t = T0
    while t < info["end"]:
        e = min(t + chunk_s, info["end"])
        chunk = {k: [s for s in v if t <= s.ts < e] for k, v in samples.items() if k in det.entities}
        det.process(chunk, [], t, e)
        det.maintain()
        t = e
    return det


@pytest.mark.parametrize("chunk_s", [DAY, DAY / 4])  # history is analysed day by day
def test_detector_measures_reactive_power_of_cycles(chunk_s: float) -> None:
    det = _run(with_pf=True, chunk_s=chunk_s)
    cands = {c["type"]: c for c in det.candidates() if c["source"] == "pattern"}
    fridge, heater = cands["refrigerator"], cands["heating"]
    assert fridge["features"]["q_ratio"] == pytest.approx(0.22, abs=0.04)
    assert heater["features"]["q_ratio"] == pytest.approx(0.0, abs=0.04)
    measured = fridge["features"]["q_count"] / min(fridge["features"]["count"], 300)
    assert measured > 0.9  # clean data: (almost) every cycle gets its value


def test_detector_without_power_factor_is_unchanged() -> None:
    plain, with_pf = _run(with_pf=False), _run(with_pf=True)
    assert "sensor.mains_pf" not in plain.entities
    a = sorted((c["type"], round(c["power_w"])) for c in plain.candidates())
    b = sorted((c["type"], round(c["power_w"])) for c in with_pf.candidates())
    assert a == b  # same devices; the power factor only adds information here
    assert all(math.isnan(c["features"]["q_ratio"]) for c in plain.candidates() if c["source"] == "pattern")


def _cycles(n: int, watts: float, ratio: float | None, offset: float) -> list[Cycle]:
    return [
        Cycle("A", T0 + i * 3600 + offset, T0 + i * 3600 + offset + 600, watts + 8, watts, ratio)
        for i in range(n)
    ]


def test_same_power_different_kind_of_load_is_kept_apart() -> None:
    """A 45 W resistive heater pad and a 45 W pump: one cluster by power, two with the power factor."""
    mixed = sorted(_cycles(20, 45, 0.0, 0) + _cycles(20, 46, 0.6, 1800), key=lambda c: c.start_ts)
    with_q = ClusterSet()
    for c in mixed:
        with_q.add(c)
    assert sorted(round(cl.q_ratio or 0, 1) for cl in with_q.clusters.values()) == [0.0, 0.6]
    without_q = ClusterSet()
    for c in mixed:
        without_q.add(Cycle(c.phase, c.start_ts, c.end_ts, c.on_w, c.off_w))
    assert len(without_q.clusters) == 1
    # and two such clusters are never merged, even if they take turns
    assert with_q.merge_close() == []


def test_known_ratio_changes_the_type_only_when_it_does_not_fit() -> None:
    heater = {"power_w": 2000, "duration_min": 5, "cycles_per_day": 3, "hour_uniformity": 0.5, "count": 60,
              "power_spread": 0.05}  # fmt: skip
    fridge = {"power_w": 40, "duration_min": 15, "cycles_per_day": 24, "hour_uniformity": 1.0, "count": 300,
              "power_spread": 0.05, "duration_spread": 0.1}  # fmt: skip
    for f, kind in ((heater, "heating"), (fridge, "refrigerator")):
        base = classify(f)
        assert base.type == kind
        assert classify(f | {"q_ratio": math.nan}).confidence == base.confidence  # unknown: no change
        fits = 0.0 if kind == "heating" else 0.22
        wrong = 0.5 if kind == "heating" else 0.0
        assert classify(f | {"q_ratio": fits}).confidence == base.confidence
        assert classify(f | {"q_ratio": wrong}).confidence < base.confidence / 2


def test_cluster_state_with_and_without_ratio_roundtrips() -> None:
    cl = Cluster(id="A-1", phase="A")
    for c in _cycles(6, 40, 0.2, 0):
        cl.add(c, 0)
    d = cl.to_dict()
    assert len(d["recent"][0]) == 5
    again = Cluster.from_dict(d)
    assert again.q_ratio == pytest.approx(0.2)
    old = d | {"recent": [row[:4] for row in d["recent"]]}  # state saved by 0.2.x
    assert Cluster.from_dict(old).q_ratio is None


def test_power_factor_sensors_and_suggestion() -> None:
    def st(eid: str, state: str, unit: str | None, device_class: str | None = None) -> dict[str, object]:
        attrs = {k: v for k, v in (("unit_of_measurement", unit), ("device_class", device_class)) if v}
        return {"entity_id": eid, "state": state, "attributes": attrs}

    states = [
        st("sensor.shellyem3_x_channel_a_power", "100", "W"),
        st("sensor.shellyem3_x_channel_a_power_factor", "62.0", "%", "power_factor"),
        st("sensor.shellypro3em_y_phase_b_active_power", "5", "W"),
        st("sensor.shellypro3em_y_phase_b_power_factor", "0.9", None, "power_factor"),
        st("sensor.battery", "60", "%"),  # a percentage, but not a power factor
    ]
    pf = [s["entity_id"] for s in power_factor_sensors(states)]
    assert pf == ["sensor.shellyem3_x_channel_a_power_factor", "sensor.shellypro3em_y_phase_b_power_factor"]
    power = [
        "sensor.shellyem3_x_channel_a_power",
        "sensor.shellypro3em_y_phase_b_active_power",
        "sensor.other_power",
    ]
    assert suggest_power_factors(power, pf) == {
        "sensor.shellyem3_x_channel_a_power": "sensor.shellyem3_x_channel_a_power_factor",
        "sensor.shellypro3em_y_phase_b_active_power": "sensor.shellypro3em_y_phase_b_power_factor",
    }


def test_setup_validation_of_power_factor() -> None:
    with pytest.raises(ValueError, match="only belong to one phase"):
        Setup.model_validate({"phases": [
            {"phase": "A", "mains": "sensor.a", "power_factor": "sensor.pf"},
            {"phase": "B", "mains": "sensor.b", "power_factor": "sensor.pf"},
        ]})  # fmt: skip
    with pytest.raises(ValueError, match="cannot also be a power meter"):
        Setup.model_validate({"phases": [{"phase": "A", "mains": "sensor.a", "power_factor": "sensor.a"}]})
    plain = Setup.model_validate({"phases": [{"phase": "A", "mains": "sensor.a"}]})
    with_pf = Setup.model_validate(
        {"phases": [{"phase": "A", "mains": "sensor.a", "power_factor": "sensor.pf"}]}
    )
    assert plain.fingerprint() == with_pf.fingerprint()  # adding it does not start the analysis over


async def test_setup_api_offers_and_checks_power_factor_sensors(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    engine = Engine(Options.model_validate({"history_days": 0}), store, FakeHA(make_house(days=1)))  # type: ignore[arg-type]
    await engine.start()
    c = await client_for(engine, runtime("standalone"))
    try:
        data = await (await c.get("/api/setup")).json()
        assert [s["entity_id"] for s in data["pf_sensors"]] == ["sensor.phase_a_power_factor"]
        bad = {"phases": [{"phase": "A", "mains": "sensor.phase_a", "power_factor": "sensor.temperature"}]}
        r = await c.put("/api/setup", json=bad)
        assert r.status == 400 and "power factor" in await r.text()
        ok = {
            "phases": [
                {"phase": "A", "mains": "sensor.phase_a", "power_factor": "sensor.phase_a_power_factor"}
            ]
        }
        assert (await c.put("/api/setup", json=ok)).status == 200
        assert engine.analyzer is not None
        assert "sensor.phase_a_power_factor" in engine.analyzer.detector.entities
        plain = {"phases": [{"phase": "A", "mains": "sensor.phase_a"}]}
        assert (await (await c.put("/api/setup", json=plain)).json())["reset"] is False
    finally:
        await c.close()
        await engine.stop()
        store.close()
