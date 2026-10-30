"""Keep the benchmark harness working (it runs nightly from WP 1.1)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str = "fabric_bench") -> ModuleType:
    path = ROOT / "benchmarks" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


async def test_harness_measures_every_event() -> None:
    bench = _load()
    result = await bench.run("memory", events=200, rate=None, consumers=2)
    assert result.events == 200
    assert result.throughput > 0
    assert 0 <= result.p50_ms <= result.p99_ms <= result.max_ms


def test_policy_benchmark_times_every_decision() -> None:
    bench = _load("policy_bench")
    result = bench.run(decisions=70)
    assert result.decisions == 70
    assert result.policies == 5
    assert 0 < result.p50_us <= result.p99_us <= result.max_us
