# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

import scripts.issue_key_sync as issue_key_sync
from scripts.issue_key_sync import (
    CHAOS_FAILURE_MARKER,
    closing_keys,
    issue_body_for_roadmap,
    mr_texts,
    public_keys,
    sync_chaos_failure,
    sync_roadmap_issues,
    title_key,
    validate_review_keys,
)


class _FakeGL:
    """GitLab stub whose MR object carries a description key (R3-99) that
    must never reach key extraction; the single commit carries R3-23."""

    project_id = "1"

    def request(self, method, path, data=None):
        return {
            "title": "wave 2",
            "description": "context: also mentions R3-99 (deferred, keep open)",
            "web_url": "http://x",
        }

    def paged(self, path):
        return [
            {"id": "sha1", "title": "[core] x (R3-23)", "message": "[core] x (R3-23)"}
        ]


class _IssuesGL:
    def __init__(self, titles):
        self.project_id = "1"
        self.titles = titles

    def paged(self, path):
        return [{"title": title} for title in self.titles]


class _RoadmapGL:
    project_id = "1"

    def __init__(self):
        self.requests = []
        self.paged_paths = []

    def request(self, method, path, data=None):
        self.requests.append((method, path, data))
        return {}

    def paged(self, path):
        self.paged_paths.append(path)
        if path.endswith("/labels"):
            return [{"name": "FEAT"}]
        if path.endswith("/milestones"):
            return [
                {"id": 10, "title": "v1.0"},
                {"id": 11, "title": "v1.1"},
            ]
        return []


P1_46 = {
    "ID": "P1-46",
    "Task": "Distribution documentation",
    "Effort": "🔧",
    "Why it matters": "Ship the supported documentation.",
    "Source": "v1.0",
}


class _ChaosGL:
    project_id = "1"

    def __init__(self, issues):
        self.issues = issues
        self.requests = []

    def paged(self, path):
        if path.endswith("/labels"):
            return [{"name": "BUG"}, {"name": "bench"}]
        return self.issues

    def request(self, method, path, data=None):
        self.requests.append((method, path, data))


def test_mr_texts_sources_commit_keys_not_description(monkeypatch):
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "5")
    text = " ".join(mr_texts(_FakeGL()).values())
    assert public_keys(text) == {"R3-23"}  # only the committed key
    assert "R3-99" not in text             # description key must not leak


def test_mr_texts_empty_without_mr_context(monkeypatch):
    monkeypatch.delenv("CI_MERGE_REQUEST_IID", raising=False)
    assert mr_texts(_FakeGL()) == {}


def test_cli_accepts_reconcile_and_rejects_unknown_args(monkeypatch):
    reconcile = []
    monkeypatch.setattr(
        issue_key_sync,
        "sync",
        lambda *, reconcile_requested=False: reconcile.append(reconcile_requested) or 0,
    )

    assert issue_key_sync.main(["--reconcile"]) == 0
    assert reconcile == [True]
    with pytest.raises(SystemExit, match="usage"):
        issue_key_sync.main(["--unknown"])


def test_bfg_keys_recognized_alongside_rp():
    # Commit text and issue titles must agree, else B/F/G never auto-close.
    text = "[fix] x (B1) also B0, F2-10, G3; plus (R3-23) and P3-40"
    assert public_keys(text) == {"B1", "B0", "F2-10", "G3", "R3-23", "P3-40"}
    assert title_key("B1: native embedder device validation") == "B1"
    assert title_key("B0: csv ingest loses columns") == "B0"
    assert title_key("P1-53c: still works") == "P1-53c"  # R/P unregressed


def test_roadmap_sync_updates_existing_metadata_and_preserves_labels(monkeypatch):
    gl = _RoadmapGL()
    monkeypatch.setattr(issue_key_sync, "roadmap_rows", lambda: [P1_46])
    existing = {
        "P1-46": {
            "iid": 46,
            "title": "P1-46: Docs site - preview",
            "description": "stale",
            "labels": ["doc", "REV", "infra"],
            "milestone": {"id": 11, "title": "v1.1"},
        }
    }

    sync_roadmap_issues(gl, existing)

    assert gl.paged_paths.count("/projects/1/milestones") == 1
    assert gl.requests == [
        (
            "PUT",
            "/projects/1/issues/46",
            {
                "title": "P1-46: Distribution documentation",
                "description": issue_body_for_roadmap(P1_46),
                "labels": "FEAT,doc,infra",
                "milestone_id": "10",
            },
        )
    ]


def test_roadmap_sync_creates_issue_with_matching_milestone(monkeypatch):
    gl = _RoadmapGL()
    monkeypatch.setattr(issue_key_sync, "roadmap_rows", lambda: [P1_46])

    sync_roadmap_issues(gl, {})

    assert gl.requests == [
        (
            "POST",
            "/projects/1/issues",
            {
                "title": "P1-46: Distribution documentation",
                "description": issue_body_for_roadmap(P1_46),
                "labels": "FEAT",
                "milestone_id": "10",
            },
        )
    ]


def test_roadmap_sync_does_not_guess_compound_milestone(monkeypatch):
    gl = _RoadmapGL()
    row = {**P1_46, "ID": "P1-49", "Source": "v1.0 / v1.1"}
    monkeypatch.setattr(issue_key_sync, "roadmap_rows", lambda: [row])

    sync_roadmap_issues(gl, {})

    assert "milestone_id" not in gl.requests[0][2]


def test_roadmap_rows_rejects_keyed_row_with_wrong_width(monkeypatch, tmp_path):
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        "| ID | Task | Why | Source |\n"
        "| --- | --- | --- | --- |\n"
        "| P3-54 | Flat metrics | Exact search | v1.0 | extra |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(issue_key_sync, "ROADMAP", roadmap)

    with pytest.raises(RuntimeError, match=r"P3-54 has 5 cells; expected 4"):
        issue_key_sync.roadmap_rows()


def test_plain_key_gets_note_but_only_exact_trailer_closes():
    plain = "[infra] Reconcile roadmap metadata (P1-46)"
    trailer = f"{plain}\n\nCloses: P1-46"

    assert public_keys(plain) == {"P1-46"}
    assert closing_keys(plain) == set()
    assert closing_keys(trailer) == {"P1-46"}
    assert closing_keys(f"{trailer}\nCloses: B20") == {"P1-46", "B20"}
    assert closing_keys(f"{plain}\n\ncloses: P1-46") == set()
    assert closing_keys(f"{trailer}\n\nMore context") == set()


def test_review_key_validation_warns_for_reserved_suffix_gaps(capsys):
    titles = [f"R4-{number:02d}: done" for number in range(1, 66)]
    titles.append("R2-67: done")
    validate_review_keys(_IssuesGL(titles))

    assert "reserved or skipped suffixes: 66" in capsys.readouterr().err


def test_review_key_validation_rejects_duplicate_suffixes():
    with pytest.raises(RuntimeError, match="duplicate suffixes found: 68"):
        validate_review_keys(_IssuesGL(["R4-68: one", "R2-68: two"]))


def test_chaos_failure_updates_marked_issue_with_rerun_evidence(
    monkeypatch, tmp_path
):
    fake = _ChaosGL(
        [
            {
                "iid": 17,
                "state": "closed",
                "description": CHAOS_FAILURE_MARKER,
            }
        ]
    )
    monkeypatch.setattr(issue_key_sync, "GitLab", lambda: fake)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_PIPELINE_URL", "https://gitlab/pipelines/7")
    monkeypatch.setenv("CI_JOB_URL", "https://gitlab/jobs/9")
    (tmp_path / "chaos.log").write_text(
        "    seed=42  duration=60s  concurrency=8\n"
        "  27 fuzz cases handled without a 5xx\n"
        "  ran 60s, 12 checker rounds, 3 churn exceptions\n"
        "FAIL - 2 invariant violation(s):\n"
        "  - [chaos] stable doc vanished\n"
        "  - [catalog-disk] orphaned collection\n",
        encoding="utf-8",
    )

    assert sync_chaos_failure() == 0
    assert len(fake.requests) == 1
    method, path, fields = fake.requests[0]
    assert (method, path) == ("PUT", "/projects/1/issues/17")
    assert fields["state_event"] == "reopen"
    assert "https://gitlab/jobs/9/artifacts/file/chaos.log" in fields["description"]
    assert "make chaos CHAOS_SEED=42 CHAOS_DURATION=60 CHAOS_CONCUR=8" in fields[
        "description"
    ]
    assert "Release blocking: no" in fields["description"]
    assert "the seed fixes inputs, not thread scheduling" in fields["description"]
    assert "27 fuzz cases handled without a 5xx" in fields["description"]
    assert "12 checker rounds, 3 churn exceptions" in fields["description"]
    assert "[chaos] stable doc vanished" in fields["description"]
    assert "[catalog-disk] orphaned collection" in fields["description"]


def test_chaos_failure_creates_next_bug_key(monkeypatch, tmp_path):
    fake = _ChaosGL([{"title": "B12: Existing bug", "description": ""}])
    monkeypatch.setattr(issue_key_sync, "GitLab", lambda: fake)
    monkeypatch.chdir(tmp_path)

    assert sync_chaos_failure() == 0
    assert fake.requests[0][2]["title"] == "B13: Nightly PaveDB chaos failure"
