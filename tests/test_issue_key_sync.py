# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from scripts.issue_key_sync import mr_texts, public_keys


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
