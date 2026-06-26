# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from scripts.issue_key_sync import mr_texts, public_keys, title_key


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
