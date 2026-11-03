"""Unit tests for the detection building blocks."""

from __future__ import annotations

import math

import numpy as np
import pytest

from shadow_meter.detection.classify import band, classify
from shadow_meter.detection.clusters import Cluster, ClusterSet
from shadow_meter.detection.context import ContextMatcher
from shadow_meter.detection.cycles import CyclePairer
from shadow_meter.detection.grid import CarryForwardGrid
from shadow_meter.detection.steps import StepDetector
from shadow_meter.models import Cycle, Sample, StateChange, Step


class TestGrid:
    def test_carries_value_forward_between_reports(self) -> None:
        g = CarryForwardGrid(step_s=5)
        out = g.fill([Sample(0, 70.0), Sample(600, 0.0)], 0, 620)
        assert np.all(out[:120] == 70.0)  # no report for 10 minutes: still 70 W, not 0
        assert np.all(out[120:] == 0.0)

    def test_value_before_chunk_is_used_and_kept_between_chunks(self) -> None:
        g = CarryForwardGrid(step_s=5)
        assert np.all(g.fill([Sample(-100, 5.0)], 0, 50) == 5.0)
        assert np.all(g.fill([], 50, 100) == 5.0)

    def test_unknown_after_max_hold(self) -> None:
        g = CarryForwardGrid(step_s=5, max_hold_s=60)
        out = g.fill([Sample(0, 1.0)], 0, 120)
        assert np.all(out[:13] == 1.0)
        assert np.all(np.isnan(out[13:]))

    def test_nothing_known_is_nan(self) -> None:
        assert np.all(np.isnan(CarryForwardGrid().fill([], 0, 100)))

    def test_matches_simple_reference(self) -> None:
        """The vectorised fill gives exactly the same values as a plain loop, also across chunks."""
        rnd = np.random.default_rng(7)
        ts = np.sort(rnd.uniform(-50, 2000, 300))
        vals = rnd.normal(100, 30, 300)
        vals[rnd.integers(0, 300, 20)] = np.nan  # unavailable reports
        samples = [Sample(float(t), float(v)) for t, v in zip(ts, vals, strict=True)]
        fast = CarryForwardGrid(step_s=5, max_hold_s=120)
        got = np.concatenate(
            [
                fast.fill([s for s in samples if s.ts < 700], 0, 700),
                fast.fill([s for s in samples if s.ts >= 700], 700, 2100),
            ]
        )
        expected = []
        for i in range(420):
            t = i * 5.0
            known = [s for s in samples if s.ts <= t and math.isfinite(s.value)]
            expected.append(known[-1].value if known and t - known[-1].ts <= 120 else math.nan)
        np.testing.assert_array_equal(got, np.array(expected))

    def test_rejects_bad_step(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            CarryForwardGrid(step_s=0)


class TestSteps:
    def test_single_step_across_chunks(self) -> None:
        v = np.concatenate([np.full(50, 100.0), np.full(50, 160.0)])
        det = StepDetector("a", min_w=20)
        steps = det.feed(v[:48], 0) + det.feed(v[48:], 48 * 5)  # step right at a chunk boundary
        assert len(steps) == 1
        assert steps[0].ts == 250
        assert steps[0].delta_w == pytest.approx(60)

    def test_spike_is_not_a_step(self) -> None:
        v = np.full(100, 100.0)
        v[50] = 1000.0
        assert StepDetector("a").feed(v, 0) == []

    def test_nan_suppresses_steps(self) -> None:
        v = np.concatenate([np.full(50, 100.0), np.full(50, np.nan)])
        assert StepDetector("a").feed(v, 0) == []

    def test_gap_between_chunks_resets(self) -> None:
        det = StepDetector("a", min_w=20)
        det.feed(np.full(20, 100.0), 0)
        assert det.feed(np.full(20, 200.0), 10_000) == []  # not contiguous: no fake step


class TestCycles:
    def test_pairs_matching_steps_and_tracks_running(self) -> None:
        p = CyclePairer("a")
        assert p.feed(Step(0, "a", 48)) is None
        assert p.open_steps == [(0, 48)]
        c = p.feed(Step(1200, "a", -37))
        assert c == Cycle("a", 0, 1200, 48, 37)
        assert p.open_steps == []

    def test_overlapping_devices_matched_by_size(self) -> None:
        p = CyclePairer("a")
        p.feed(Step(0, "a", 2000))
        p.feed(Step(60, "a", 40))
        c = p.feed(Step(300, "a", -38))
        assert c is not None and c.on_w == 40
        c = p.feed(Step(400, "a", -1990))
        assert c is not None and c.on_w == 2000

    def test_unmatched_and_expired(self) -> None:
        p = CyclePairer("a", max_duration_s=100)
        p.feed(Step(0, "a", 100))
        assert p.feed(Step(50, "a", -500)) is None  # size does not match
        assert p.feed(Step(500, "a", -100)) is None  # too late, expired
        assert p.open_steps == []


class TestClassify:
    def test_band(self) -> None:
        assert band(50, 30, 250) == 1.0
        assert 0 < band(300, 30, 250) < 1
        assert band(0, 30, 250) == 0.0
        assert band(math.nan, 30, 250) == 0.0

    def test_fridge_like(self) -> None:
        f = {
            "power_w": 37,
            "duration_min": 20,
            "cycles_per_day": 24,
            "hour_uniformity": 0.98,
            "count": 100,
            "power_spread": 0.05,
            "duration_spread": 0.15,
        }
        c = classify(f)
        assert c.type == "refrigerator"
        assert c.confidence > 0.7
        # same numbers, but the run time is all over the place: random pairs of steps, not a compressor
        assert classify({**f, "duration_spread": 0.9}).type != "refrigerator"

    def test_kettle_like(self) -> None:
        f = {
            "power_w": 2200,
            "duration_min": 3,
            "cycles_per_day": 3,
            "hour_uniformity": 0.4,
            "count": 30,
            "power_spread": 0.02,
        }
        assert classify(f).type == "heating"

    def test_few_cycles_low_confidence(self) -> None:
        f = {
            "power_w": 37,
            "duration_min": 20,
            "cycles_per_day": 24,
            "hour_uniformity": 0.98,
            "count": 2,
            "power_spread": 0.05,
            "duration_spread": 0.15,
        }
        assert classify(f).confidence < 0.15

    def test_odd_device_is_unknown_with_zero_confidence(self) -> None:
        f = {
            "power_w": 400,
            "duration_min": 300,
            "cycles_per_day": 1,
            "hour_uniformity": 0.2,
            "count": 50,
            "power_spread": 0.1,
        }
        c = classify(f)
        assert c.type == "unknown"
        assert c.confidence == 0.0


class TestClusters:
    def test_assign_merge_and_roundtrip(self) -> None:
        cs = ClusterSet(tolerance=0.2)
        for i in range(10):
            cs.add(Cycle("a", i * 3600, i * 3600 + 1200, 48, 37 + (i % 3)))
        cs.add(Cycle("a", 50_000, 50_100, 2000, 2000))
        cs.add(Cycle("b", 60_000, 60_100, 40, 38))  # other phase -> own cluster
        assert sorted(len(c.recent) for c in cs.clusters.values()) == [1, 1, 10]
        restored = ClusterSet()
        restored.load(cs.to_dict())
        assert restored.to_dict() == cs.to_dict()
        new = restored.add(Cycle("a", 70_000, 70_100, 500, 500))
        assert new.id not in cs.clusters  # ids stay unique after loading

    def test_type_survives_a_noisy_week_but_follows_new_evidence(self) -> None:
        fridge = {"refrigerator": 0.85, "heating": 0.0, "unknown": 0.15}
        noisy = {"refrigerator": 0.0, "heating": 0.0, "unknown": 1.0}
        old = Cluster(id="A-1", phase="A", count=3000)
        old.smooth(fridge, 0.0, 14 * 86400)
        for day in range(1, 8):  # a week in which it does not look like a fridge
            old.count += 30
            old.smooth(noisy, day * 86400.0, 14 * 86400)
        assert old.probs is not None and old.probs["refrigerator"] > old.probs["unknown"]
        young = Cluster(id="A-2", phase="A", count=10)
        young.smooth(noisy, 0.0, 14 * 86400)  # first guess from a handful of cycles
        young.count = 200  # a day later it has 20 times the evidence
        young.smooth(fridge, 86400.0, 14 * 86400)
        assert young.probs is not None and young.probs["refrigerator"] > 0.75
        assert Cluster.from_dict(young.to_dict()).probs == young.probs

    def test_missed_off_step_does_not_inflate_duty(self) -> None:
        cs = ClusterSet()
        for i in range(40):  # 15 min every hour: duty 25 %
            cs.add(Cycle("a", i * 3600, i * 3600 + 900, 48, 37))
        cl = cs.add(Cycle("a", 40 * 3600, 40 * 3600 + 10 * 3600, 48, 37))  # off step missed for 10 h
        assert cl.features()["duty"] == pytest.approx(0.25, abs=0.05)
        assert cl.features()["kwh_per_day"] == pytest.approx(37 * 0.25 * 24 / 1000, rel=0.2)

    @staticmethod
    def _two_clusters(second_offset_s: float) -> ClusterSet:
        """A 39 W and a 34 W cluster on phase a, each running 15 min every 2 h."""
        cs = ClusterSet()
        for i in range(60):
            t = i * 7200
            cs.add(Cycle("a", t, t + 900, 48, 39))
        other = cs.add(Cycle("a", second_offset_s - 7200, second_offset_s - 6300, 40, 30))  # new cluster
        for i in range(60):
            t = i * 7200 + second_offset_s
            other.add(Cycle("a", t, t + 900, 44, 34), 0)
        assert len(cs.clusters) == 2
        return cs

    def test_clusters_that_take_turns_are_one_device(self) -> None:
        """One compressor at 34 or 39 W: the runs never overlap, so the two clusters are merged."""
        cs = self._two_clusters(second_offset_s=3600)  # 34 W runs in the gaps of the 39 W runs
        assert len(cs.merge_close()) == 1
        assert len(cs.clusters) == 1

    def test_overlapping_clusters_stay_apart(self) -> None:
        cs = self._two_clusters(second_offset_s=300)  # both run at the same time: two devices
        assert cs.merge_close() == []

    def test_user_decided_cluster_is_kept(self) -> None:
        cs = self._two_clusters(second_offset_s=3600)
        first, second = sorted(cs.clusters, key=lambda k: cs.clusters[k].first_ts)
        assert cs.merge_close(keep={second}) == [(second, first)]
        cs = self._two_clusters(second_offset_s=3600)
        assert cs.merge_close(keep=set(cs.clusters)) == []  # two decisions: never merged


class TestContext:
    def test_solo_changes_are_matched(self) -> None:
        m = ContextMatcher()
        for k in range(20):
            t = k * 1000.0
            m.add_change(StateChange(t, "light.hall", True))
            m.add_step(Step(t + 4, "c", 21.0))
            m.add_change(StateChange(t + 300, "light.hall", False))
            m.add_step(Step(t + 303, "c", -20.0))
        m.flush(1e9)
        s = m.stats["light.hall"].summary()
        assert s["phase"] == "c"
        assert s["power_w"] == pytest.approx(20.5, abs=0.6)
        assert s["match_rate"] == 1.0
        assert m.stats["light.hall"].confidence() > 0.8

    def test_chance_matches_do_not_count(self) -> None:
        """A busy phase has steps all the time: matching them by chance must not give confidence."""
        m = ContextMatcher()
        t = 0.0
        while t < 40_000:
            m.add_step(Step(t, "b", 30.0))  # unrelated device switching every 25 s
            m.add_step(Step(t + 12, "b", -30.0))
            t += 25
        for k in range(30):
            m.add_change(StateChange(500 + k * 1000.0, "light.tiny", k % 2 == 0))
        m.flush(1e9)
        s = m.stats["light.tiny"]
        assert s.summary()["match_rate"] > 0.9  # raw matches look great ...
        assert s.summary()["chance_rate"] > 0.9  # ... but happen at control times as well
        assert s.confidence() < 0.1

    def test_scene_changes_are_ignored(self) -> None:
        m = ContextMatcher()
        m.add_change(StateChange(0, "light.a", True))
        m.add_change(StateChange(2, "light.b", True))
        m.add_step(Step(3, "c", 60))
        m.flush(100)
        assert all(s.changes == 0 for s in m.stats.values())
