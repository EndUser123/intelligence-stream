"""Helpers for NotebookLM benchmark ladders.

This module defines the scenario shapes used by the load-ladder runner and
builds the command line for the existing fallback crossover benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LadderScenario:
    """A single benchmark scenario in the load ladder."""

    name: str
    description: str
    env_overrides: dict[str, str] = field(default_factory=dict)
    preserve_worker_state_root: bool = False


def default_load_ladder_scenarios() -> list[LadderScenario]:
    """Return the current default ladder order.

    The order is intentionally conservative:
    - baseline
    - notebook fullness
    - fresh vs reused worker state
    - staggered access on/off
    - rotation threshold
    """
    return [
        LadderScenario(
            name="baseline",
            description="Current notebook settings and current jitter defaults.",
        ),
        LadderScenario(
            name="fullness_25",
            description="Reduce notebook source cap to 25 to see whether fullness pressure shows up sooner.",
            env_overrides={"YTIS_NLM_SOURCE_CAP": "25"},
        ),
        LadderScenario(
            name="fresh_state",
            description="Clear worker state before the run so the notebook is recreated fresh.",
        ),
        LadderScenario(
            name="reuse_state",
            description="Reuse the shared worker state so notebook reuse can amortize setup.",
            preserve_worker_state_root=True,
        ),
        LadderScenario(
            name="staggered_off",
            description="Disable worker jitter to see how much access contention the stagger avoids.",
            env_overrides={
                "YTIS_TRANSCRIPT_WORKER_JITTER_MIN_S": "0",
                "YTIS_TRANSCRIPT_WORKER_JITTER_MAX_S": "0",
            },
        ),
        LadderScenario(
            name="staggered_on",
            description="Use the current worker jitter window to compare against staggered_off.",
            preserve_worker_state_root=True,
        ),
        LadderScenario(
            name="rotation_75",
            description="Raise the notebook source cap to 75 to see whether rotation itself is the limiter.",
            env_overrides={"YTIS_NLM_SOURCE_CAP": "75"},
        ),
        LadderScenario(
            name="route_no_captions_to_fallback",
            description="Route no-caption items directly to transcript fallback to measure source-shape splitting.",
            env_overrides={"YTIS_ROUTE_NO_CAPTIONS_TO_FALLBACK": "true"},
        ),
    ]


def scenario_by_name(name: str) -> LadderScenario:
    """Return the named default scenario."""
    for scenario in default_load_ladder_scenarios():
        if scenario.name == name:
            return scenario
    raise KeyError(name)


def build_fallback_benchmark_command(
    *,
    python_executable: str,
    fallback_benchmark_script: Path,
    trace_root: Path,
    cohort_json: Path,
    output_root: Path,
    source_url: str,
    workers: int,
    limit: int,
    batch_size: int,
    policy: str,
    cohort_shape: str = "trace",
    sample_label: str | None = None,
    manifest_json: Path | None = None,
    manifest_families: str | None = None,
    worker_state_root: Path,
    preserve_worker_state_root: bool,
) -> list[str]:
    """Build the command used to invoke the existing benchmark runner."""
    command = [
        python_executable,
        str(fallback_benchmark_script),
        "--trace-root",
        str(trace_root),
        "--cohort-json",
        str(cohort_json),
        "--output-root",
        str(output_root),
        "--source-url",
        source_url,
        "--workers",
        str(workers),
        "--limit",
        str(limit),
        "--batch-size",
        str(batch_size),
        "--policy",
        policy,
        "--cohort-shape",
        cohort_shape,
        "--worker-state-root",
        str(worker_state_root),
    ]
    if sample_label is not None:
        command.extend(["--sample-label", sample_label])
    if manifest_json is not None:
        command.extend(["--manifest-json", str(manifest_json)])
    if manifest_families is not None:
        command.extend(["--manifest-families", manifest_families])
    if preserve_worker_state_root:
        command.append("--preserve-worker-state-root")
    return command
