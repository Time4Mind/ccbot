"""Tests for the metrics module — counters, observations, snapshot shape."""

import pytest

from ccbot import metrics


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    metrics._counters.clear()
    metrics._observations.clear()


class TestCounters:
    def test_inc_default_one(self) -> None:
        metrics.inc("foo")
        assert metrics._counters["foo"] == 1

    def test_inc_explicit_value(self) -> None:
        metrics.inc("foo", 5)
        assert metrics._counters["foo"] == 5

    def test_inc_accumulates(self) -> None:
        metrics.inc("foo")
        metrics.inc("foo", 2)
        assert metrics._counters["foo"] == 3


class TestObservations:
    def test_observe_records_sample(self) -> None:
        metrics.observe("latency_ms", 12.0)
        metrics.observe("latency_ms", 34.0)
        snap = metrics.snapshot()
        assert "latency_ms" in snap["observations"]
        assert snap["observations"]["latency_ms"]["count"] == 2

    def test_observation_window_caps_at_obs_window(self) -> None:
        for i in range(metrics.OBS_WINDOW + 50):
            metrics.observe("noisy", float(i))
        snap = metrics.snapshot()
        assert snap["observations"]["noisy"]["count"] == metrics.OBS_WINDOW

    def test_p50_p95_monotonic(self) -> None:
        for i in range(1, 101):
            metrics.observe("uniform", float(i))
        snap = metrics.snapshot()["observations"]["uniform"]
        assert snap["min"] == 1.0
        assert snap["max"] == 100.0
        assert snap["p50"] <= snap["p95"] <= snap["max"]


class TestSnapshot:
    def test_includes_uptime_and_timestamp(self) -> None:
        snap = metrics.snapshot()
        assert "uptime_seconds" in snap
        assert "ts" in snap
        assert snap["uptime_seconds"] >= 0

    def test_empty_observations_omitted(self) -> None:
        snap = metrics.snapshot()
        assert snap["observations"] == {}
