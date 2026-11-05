"""Finding the phase of devices with their own meter from the history."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import numpy as np

from shadow_meter.engine import Engine
from shadow_meter.models import Sample
from shadow_meter.phase_assign import PhaseFinder
from shadow_meter.settings import Options
from shadow_meter.store import Store

from .synthetic import make_house
from .test_app import FakeHA, client_for, runtime

DAY = 86400.0
T0 = 1_750_000_000.0


def _house(days: int = 3, seed: int = 5) -> dict[str, list[Sample]]:
    """Three phases with noise and other devices; a kettle plug on B, a printer behind a UPS (not on the
    meter), a plug that never switches, and a small fan on C."""
    rnd = random.Random(seed)
    ts = np.arange(T0, T0 + days * DAY, 5.0)
    ph = {p: 100 + np.array([rnd.gauss(0, 3) for _ in ts]) for p in "ABC"}
    kettle, printer, fan = np.zeros(ts.size), np.zeros(ts.size), np.zeros(ts.size)
    for _ in range(40):  # foreign devices switching on all phases
        p, a = rnd.choice("ABC"), rnd.randrange(ts.size - 400)
        ph[p][a : a + rnd.randrange(20, 300)] += rnd.choice([60, 150, 400])
    for _ in range(25):
        a = rnd.randrange(ts.size - 100)
        kettle[a : a + 36] = 2000  # 3 min
        a = rnd.randrange(ts.size - 400)
        printer[a : a + 300] = 250  # behind the UPS: never on the phases
        a = rnd.randrange(ts.size - 200)
        fan[a : a + 120] = 40
    ph["B"] += kettle
    ph["C"] += fan
    out = {f"sensor.phase_{p.lower()}": v for p, v in ph.items()}
    out |= {
        "sensor.kettle": kettle,
        "sensor.printer": printer,
        "sensor.fan": fan,
        "sensor.idle": np.full(ts.size, 3.0),
    }
    # Home Assistant reports a value only when it changes
    samples = {}
    for e, v in out.items():
        keep = np.concatenate(([True], np.abs(np.diff(v)) > 0.5))
        samples[e] = [Sample(float(t), float(x)) for t, x in zip(ts[keep], v[keep], strict=True)]
    return samples


def test_phases_of_plugs_are_found() -> None:
    samples = _house()
    finder = PhaseFinder(
        {"A": "sensor.phase_a", "B": "sensor.phase_b", "C": "sensor.phase_c"},
        ["sensor.kettle", "sensor.printer", "sensor.fan", "sensor.idle"],
    )
    for d in range(3):  # day by day, like the app
        s, e = T0 + d * DAY, T0 + (d + 1) * DAY
        finder.add({k: [x for x in v if s - 3600 <= x.ts < e] for k, v in samples.items()}, s, e)
    r = finder.results()
    assert (r["sensor.kettle"]["verdict"], r["sensor.kettle"]["phase"]) == ("phase", "B")
    assert (r["sensor.fan"]["verdict"], r["sensor.fan"]["phase"]) == ("phase", "C")
    assert r["sensor.printer"]["verdict"] == "not_behind"
    assert r["sensor.idle"]["verdict"] == "unsure" and r["sensor.idle"]["steps"] == 0


async def test_find_phases_api(tmp_path: Path) -> None:
    house = make_house(days=1)
    ha = FakeHA(house)
    store = Store(tmp_path / "db.sqlite")
    engine = Engine(
        Options.model_validate({"history_days": 0}),
        store,
        ha,  # type: ignore[arg-type]
        power_history=ha.power,
        clock=lambda: house.samples["sensor.phase_a"][-1].ts,
    )
    await engine.start()
    c = await client_for(engine, runtime("standalone"))
    try:
        assert (await (await c.get("/api/setup/find-phases")).json())["state"] == "idle"
        bad = {"phases": [{"phase": "A", "mains": "sensor.temperature"}]}
        assert (await c.post("/api/setup/find-phases", json=bad)).status == 400
        phases = {"phases": [{"phase": p, "mains": f"sensor.phase_{p.lower()}"} for p in "ABC"]}
        r = await c.post("/api/setup/find-phases", json=phases)
        assert r.status == 200 and (await r.json())["plugs"] == 1  # the dishwasher
        for _ in range(100):
            job = await (await c.get("/api/setup/find-phases")).json()
            if job["state"] != "running":
                break
            await asyncio.sleep(0.05)
        assert job["state"] == "done" and set(job["results"]) == {"sensor.dishwasher"}
    finally:
        await c.close()
        await engine.stop()
        store.close()
