# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the bench latency gate needs from the pipeline to mean anything.

The p99 threshold is a property of the machine that runs it: 2000ms was
calibrated on GitLab SaaS runners (search p99 ~280ms) and the shared group
runner measures ~2.3s for the same run, so the job names the runner it was
calibrated against. A pin only holds while that runner is idle, though, and
no job here is interruptible: with no resource group, a second push to an
open merge request leaves the superseded bench job timing the same box as
the new one; a per-ref group serializes that push, but two open merge
requests get a group each and time the box side by side. Either half missing
shows up as a gate that fails for reasons the server never caused, which is
indistinguishable from the regression it exists to catch, so both halves are
pinned to a value rather than to each other.
"""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]

# The machine the 2000ms was measured against. pytest-arm64 happens to share
# it — that is why it sits idle between tags — but sharing is the reason for
# the pin, not the pin itself: asserting only that the two jobs agree stays
# green when both move to the shared pool, and goes red when the arm64 job
# moves to the real macOS host its own comment anticipates, which would
# report a healthy gate as broken.
CALIBRATED_RUNNER = ["m4-colima"]

# GitLab expands CI/CD variables in `resource_group`, and a per-ref name like
# `$CI_COMMIT_REF_SLUG` is the documented idiom for serializing deploys. Here
# it would give every merge request a group of its own and let two bench jobs
# time the one pinned box again, so the name has to be the same constant in
# every pipeline.
RESOURCE_GROUP = "bench"


def _config():
    return yaml.safe_load((ROOT / "gitlab-ci.yml").read_text(encoding="utf-8"))


def test_the_latency_gate_runs_on_the_runner_it_was_calibrated_on():
    """2000ms is a property of this machine, not of the shared runner pool."""
    assert _config()["bench"]["tags"] == CALIBRATED_RUNNER


def test_only_one_latency_run_is_timed_at_a_time():
    """Two bench jobs on one runner measure each other, not the server."""
    assert _config()["bench"]["resource_group"] == RESOURCE_GROUP
