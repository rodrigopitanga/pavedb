# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the bench latency gate needs from the pipeline to mean anything.

The p99 ceiling is a property of the host that times the run, and this job
names no runner, so the number has to hold on the slowest host that can pick
it up: 2000 ms was calibrated where search p99 is ~280 ms (GitLab SaaS), and
the shared group runner measures ~2.3 s for the same 200-query run, which is
red before any regression exists. The ceiling is sized for that host and
pinned to a value here, so a red gate stays a statement about the server
rather than about the host.

The gate runs wherever a project runner is free, so one timed run at a time
is the only thing keeping it off a busy host. No job here is interruptible:
with no resource group, a second push to an open merge request leaves the
superseded bench job timing the same box as the new one; a per-ref group
serializes that push, but two open merge requests get a group each and time
the box side by side. Contention shows up as a gate that fails for reasons
the server never caused, which is indistinguishable from the regression it
exists to catch, so the group name is pinned to a value rather than to the
ref.

The same goes for what the job fetches before it starts. `dependencies` is
invisible in the job text when it is left out, and leaving it out is how the
gate came to download the test stage's artifacts on every run — a round trip
`make benchmark` has no use for, and the one job 17704770842 died inside
without timing a query.
"""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]

# Sized for the slowest host that can pick this job up, not for the machine
# 2000 ms was calibrated on. Asserting only that some ceiling is set stays
# green when the number drifts back to a SaaS-only one, which reports a loaded
# host as a latency regression on every merge request.
LATENCY_CEILING_MS = 4000

# The plan doc states that ceiling in its SLO table and shows it again in the
# violation output a red gate gets compared against. Both are the number this
# job sets, so a number left behind there explains the next red gate with the
# threshold the job stopped using.
PLAN = ROOT / "docs" / "design" / "PLAN-BENCH-CI.md"

# GitLab expands CI/CD variables in `resource_group`, and a per-ref name like
# `$CI_COMMIT_REF_SLUG` is the documented idiom for serializing deploys. Here
# it would give every merge request a group of its own and let two bench jobs
# time the same host again, so the name has to be the same constant in
# every pipeline.
RESOURCE_GROUP = "bench"


def _config():
    return yaml.safe_load((ROOT / "gitlab-ci.yml").read_text(encoding="utf-8"))


def _bench_script():
    (command,) = _config()["bench"]["script"]
    return command


def test_the_latency_gate_is_sized_for_the_slowest_eligible_host():
    """2000ms is a property of one machine, and this job names no runner."""
    ceiling = f"LAT_SLO_P99_MS=${{LAT_SLO_P99_MS:-{LATENCY_CEILING_MS}}}"

    assert ceiling in _bench_script()


def test_the_plan_doc_names_the_ceiling_the_job_sets():
    """The doc read when the gate goes red cannot name another threshold."""
    plan = PLAN.read_text(encoding="utf-8")

    assert f"| {LATENCY_CEILING_MS} ms |" in plan
    assert f"> {LATENCY_CEILING_MS}.0ms" in plan


def test_only_one_latency_run_is_timed_at_a_time():
    """Two bench jobs on one runner measure each other, not the server."""
    assert _config()["bench"]["resource_group"] == RESOURCE_GROUP


def test_the_gate_fetches_nothing_before_it_measures():
    """`make benchmark` builds from the checkout and serves itself. The test
    stage's junit.xml and .pytest_cache are triage output, and the release
    title and notes it publishes where the announce jobs run already have a
    consumer: those jobs name `validate-release-announcement` in their own
    `dependencies`. None of it buys the measurement anything, and fetching it
    can fail on its own."""
    assert _config()["bench"]["dependencies"] == []


def test_the_gate_still_waits_for_the_stage_it_runs_behind():
    """`needs: []` empties the download too, and also cuts the job loose from
    stage order: the gate would start alongside the suite instead of after
    it, and a red suite would stop holding it back. Stage order is the only
    thing sequencing this job, so nothing may replace it."""
    config = _config()
    bench = config["bench"]

    assert "needs" not in bench
    assert config["stages"].index(bench["stage"]) > config["stages"].index("test")
