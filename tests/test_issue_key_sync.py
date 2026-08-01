# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

import scripts.issue_key_sync as issue_key_sync
from scripts.issue_key_sync import (
    closing_keys,
    issue_body_for_roadmap,
    mr_texts,
    public_keys,
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


def test_mr_texts_sources_commit_keys_not_description(monkeypatch):
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "5")
    text = " ".join(mr_texts(_FakeGL()).values())
    assert public_keys(text) == {"R3-23"}  # only the committed key
    assert "R3-99" not in text             # description key must not leak


def test_mr_texts_empty_without_mr_context(monkeypatch):
    monkeypatch.delenv("CI_MERGE_REQUEST_IID", raising=False)
    assert mr_texts(_FakeGL()) == {}


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
