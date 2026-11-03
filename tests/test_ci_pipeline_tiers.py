# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which pytest tier each GitLab pipeline type runs.

The full suite costs more wall clock than a per-merge job budget holds, so
it runs only where coverage is a release gate (default branch and tags) and
the merge path runs the fast tier. Rules are easy to edit into a hole that
no pipeline exercises, or into one that quietly puts the full suite back on
the merge path, and neither shows up until a pipeline runs. These checks pin
the mapping per pipeline type instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]

TIERS = ("pytest", "pytest-full")

# GitLab always defines CI_DEFAULT_BRANCH; CI_COMMIT_BRANCH is unset in merge
# request and tag pipelines, and CI_OPEN_MERGE_REQUESTS is only set in a branch
# pipeline whose branch already had an open MR when the pipeline was created.
PIPELINES = {
    "merge request": {"CI_PIPELINE_SOURCE": "merge_request_event"},
    "default branch": {"CI_COMMIT_BRANCH": "main"},
    "tag": {"CI_COMMIT_TAG": "v0.9.8"},
    # The branch pipeline that timed out on 2026-10-30 (job 17617632088).
    "branch without an open merge request": {"CI_COMMIT_BRANCH": "bug/bench-runner"},
    "branch with an open merge request": {
        "CI_COMMIT_BRANCH": "bug/bench-runner",
        "CI_OPEN_MERGE_REQUESTS": "flowlexi/pavedb!88",
    },
}


def _config():
    return yaml.safe_load((ROOT / "gitlab-ci.yml").read_text(encoding="utf-8"))


def _job(config, name):
    """Resolve `extends`. Shallow: the tiers replace keys, never merge into
    them, which is all GitLab does for the scalars and lists used here."""
    job = dict(config[name])
    base = job.pop("extends", None)
    if base:
        job = {**config[base], **job}
    return job


def _env(pipeline):
    env = {"CI_PIPELINE_SOURCE": "push", "CI_DEFAULT_BRANCH": "main"}
    env.update(PIPELINES[pipeline])
    return env


# The tier rules use only `$VAR`, `==`, `!=`, `null`, `&&` and `||`. The rest
# of GitLab's expression syntax is refused rather than guessed at, because the
# fallback for an unrecognized clause is a bare variable lookup: `$CI_COMMIT_TAG
# =~ /^v/` would resolve as the variable named `CI_COMMIT_TAG =~ /^v/`, which is
# never set, so the clause would read as never-matched. A regex rule added to a
# tier — the idiom ten of the pipeline's twenty-one rules already use — would
# then put that tier on a pipeline type with every check below still green,
# which is the one edit this file exists to catch.
_UNSUPPORTED = (
    "rule expression {expr!r} uses GitLab syntax these checks cannot evaluate; "
    "teach _clause/_runs the operator or re-express the rule"
)


def _value(token, env):
    token = token.strip()
    if token == "null":
        return None
    if token.startswith("$"):
        name = token[1:]
        if not name.isidentifier():
            raise NotImplementedError(_UNSUPPORTED.format(expr=token))
        return env.get(name)
    return token.strip("\"'")


def _clause(expr, env):
    for operator in ("==", "!="):
        if operator in expr:
            left, right = expr.split(operator, 1)
            matched = _value(left, env) == _value(right, env)
            return matched if operator == "==" else not matched
    return bool(_value(expr, env))


def _runs(job, env):
    """First matching rule wins, as GitLab evaluates them, and `&&` binds
    tighter than `||`. Parentheses regroup that, so they are refused."""
    for rule in job["rules"]:
        expr = rule["if"]
        if "(" in expr or ")" in expr:
            raise NotImplementedError(_UNSUPPORTED.format(expr=expr))
        if any(
            all(_clause(part, env) for part in conjunction.split("&&"))
            for conjunction in expr.split("||")
        ):
            return rule.get("when", "on_success") != "never"
    return False


def _command(job):
    return next(step for step in job["script"] if step.startswith("pytest "))


@pytest.mark.parametrize(
    "pipeline,expected",
    [
        ("merge request", {"pytest"}),
        ("default branch", {"pytest-full"}),
        ("tag", {"pytest-full"}),
        ("branch without an open merge request", {"pytest"}),
        # Covered by the merge request pipeline, so the branch pipeline skips
        # the suite instead of running it a second time.
        ("branch with an open merge request", set()),
    ],
)
def test_each_pipeline_type_runs_exactly_one_suite_tier(pipeline, expected):
    config = _config()
    env = _env(pipeline)
    assert {name for name in TIERS if _runs(_job(config, name), env)} == expected


def test_a_rule_these_checks_cannot_read_goes_red_instead_of_never_matching():
    """A tier that gains a regex rule has to surface here, not pass quietly.

    Scored as never-matched, `$CI_COMMIT_TAG =~ /^v/` on the fast tier leaves
    the mapping above green while that tier runs on every tag beside the full
    suite — the full suite would no longer be the only thing gating a release.
    """
    tier = {"rules": [{"if": "$CI_COMMIT_TAG =~ /^v/"}]}

    with pytest.raises(NotImplementedError):
        _runs(tier, _env("tag"))


def test_rules_joined_with_or_are_grouped_the_way_gitlab_groups_them():
    """`&&` binds tighter than `||`, so a rule pair rewritten as the one
    equivalent rule still selects the same pipeline types."""

    def selects(expr):
        tier = {"rules": [{"if": expr}]}
        return {name for name in PIPELINES if _runs(tier, _env(name))}

    release_path = "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH || $CI_COMMIT_TAG"
    assert selects(release_path) == {"default branch", "tag"}

    mixed = (
        "$CI_COMMIT_BRANCH && $CI_COMMIT_BRANCH != $CI_DEFAULT_BRANCH"
        " || $CI_COMMIT_TAG"
    )
    assert selects(mixed) == {
        "branch without an open merge request",
        "branch with an open merge request",
        "tag",
    }


def test_the_merge_path_is_fast_and_the_release_path_is_the_full_suite():
    config = _config()

    assert '-m "not slow"' in _command(_job(config, "pytest"))
    assert "-m" not in _command(_job(config, "pytest-full")).split()


def test_the_full_suite_is_never_selected_outside_the_release_path():
    config = _config()
    full = _job(config, "pytest-full")

    for pipeline in PIPELINES:
        if _runs(full, _env(pipeline)):
            assert pipeline in ("default branch", "tag"), pipeline


def test_a_suite_that_ran_out_of_time_is_not_retried():
    """A timeout is deterministic; re-running it just spends the budget twice
    more. The tagged jobs keep the default retry, which covers a single busy
    runner reported as stuck."""
    config = _config()

    for name in TIERS:
        assert _job(config, name)["retry"]["when"] == ["runner_system_failure"]


def test_jobs_that_declare_a_junit_report_generate_the_file():
    config = _config()
    checked = set()

    for name, job in config.items():
        if name.startswith(".") or not isinstance(job, dict):
            continue
        job = _job(config, name)
        report = job.get("artifacts", {}).get("reports", {}).get("junit")
        if not report:
            continue
        checked.add(name)
        assert f"--junitxml={report}" in _command(job), name

    assert checked == {"pytest", "pytest-full", "pytest-arm64"}


def test_the_suite_tiers_run_on_four_workers():
    """The shared runner has sixteen threads and the tiers run in-process
    (TestClient, FakeEmbedder): one worker left fifteen of them idle for
    twenty minutes (job 17708580395). The arm64 VM has two cores."""
    config = _config()

    for name in TIERS:
        assert " -n 4 " in _command(_job(config, name)), name
    assert " -n " not in _command(_job(config, "pytest-arm64"))
    assert '"pytest-xdist"' in (ROOT / "setup.py").read_text(encoding="utf-8")
