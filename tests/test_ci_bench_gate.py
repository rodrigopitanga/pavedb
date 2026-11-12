# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pin the benchmark policy after removing the artifact-based gate (B97).

The shared benchmark template holds the runnable script, stage, resource
serialization, and artifact-download policy. Both real jobs inherit it, so
checking only the keys written directly under `bench` misses their effective
configuration. Colima is diagnostic on merge requests; the Flowlexi runner
performs the mandatory benchmark after merge and on release tags.

The p99 ceiling must accommodate the slowest eligible host, and all timed
runs share a project-wide resource group to avoid measuring contention.
"""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
LATENCY_CEILING_MS = 4000
PLAN = ROOT / "docs" / "design" / "PLAN-BENCH-CI.md"
RESOURCE_GROUP = "bench"
BENCHMARK_JOBS = ("bench", "bench-flowlexi")


def _config():
    return yaml.safe_load((ROOT / "gitlab-ci.yml").read_text(encoding="utf-8"))


def _job(config, name):
    """Resolve the single-parent `extends` used by the benchmark jobs.

    These tests rely on inherited top-level settings, which GitLab merges
    into the effective job even when the job only declares `extends`.
    """
    child = dict(config[name])
    base = child.pop("extends", None)
    if base:
        return {**_job(config, base), **child}
    return child


def _bench_script(config, name):
    (command,) = _job(config, name)["script"]
    return command


def test_the_latency_threshold_is_sized_for_the_slowest_eligible_host():
    ceiling = f"LAT_SLO_P99_MS=${{LAT_SLO_P99_MS:-{LATENCY_CEILING_MS}}}"
    config = _config()

    for name in BENCHMARK_JOBS:
        assert ceiling in _bench_script(config, name), name


def test_the_plan_doc_names_the_ceiling_the_job_sets():
    plan = PLAN.read_text(encoding="utf-8")

    assert f"| {LATENCY_CEILING_MS} ms |" in plan
    assert f"> {LATENCY_CEILING_MS}.0ms" in plan


def test_only_one_latency_run_is_timed_at_a_time():
    config = _config()
    for name in BENCHMARK_JOBS:
        assert _job(config, name)["resource_group"] == RESOURCE_GROUP, name


def test_benchmark_jobs_fetch_no_prior_artifacts():
    """Neither timed run needs test-stage or release-note artifacts."""
    config = _config()
    for name in BENCHMARK_JOBS:
        job = _job(config, name)
        assert job["dependencies"] == [], name
        assert "needs" not in job, name


def test_benchmark_jobs_still_wait_for_the_test_stage():
    """Both benchmarks must remain behind the test stage, not bypass it."""
    config = _config()
    stages = config["stages"]

    for name in BENCHMARK_JOBS:
        assert stages.index(_job(config, name)["stage"]) > stages.index("test")


def test_both_benchmark_jobs_measure_bypass_and_tenant_latency():
    config = _config()
    for name in BENCHMARK_JOBS:
        assert "make benchmark bench-latency-rate-limited" in _bench_script(config, name)


def test_no_artifact_gate_or_hosted_benchmark_is_scheduled():
    """B97: no container just to inspect a marker or hit exhausted SaaS quota."""
    config = _config()
    assert "bench-gate" not in config
    assert "bench-hosted" not in config


def test_merge_requests_use_optional_colima_and_releases_require_flowlexi():
    config = _config()
    assert config["bench"]["extends"] == ".benchmark-job"
    assert config["bench-flowlexi"]["extends"] == ".benchmark-job"

    mr = _job(config, "bench")
    assert mr["tags"] == ["m4-colima"]
    assert mr["allow_failure"] is True
    assert mr["rules"] == [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]

    mandatory = _job(config, "bench-flowlexi")
    assert mandatory["tags"] == ["flowlexi"]
    assert mandatory["allow_failure"] is False
    assert mandatory["rules"] == [
        {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
        {"if": "$CI_COMMIT_TAG"},
    ]
