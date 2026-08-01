import pytest

from scripts.validate_release_announcement import validate_release_announcement


def write_release(tmp_path, tag, title):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / f"RELEASE-{tag}.md").write_text(
        f"Intro\n\n# {title}\n\nReviewed body.\n", encoding="utf-8"
    )


def test_named_major_minor_release(tmp_path):
    write_release(tmp_path, "v1.0.0", "PaveDB 1.0.0 — First Light")
    assert (
        validate_release_announcement("v1.0.0", tmp_path)
        == "PaveDB 1.0.0 — First Light"
    )


def test_unnamed_patch_release(tmp_path):
    write_release(tmp_path, "v1.0.2", "PaveDB 1.0.2")
    assert validate_release_announcement("v1.0.2", tmp_path) == "PaveDB 1.0.2"


def test_missing_release_file(tmp_path):
    with pytest.raises(ValueError, match="missing release announcement"):
        validate_release_announcement("v1.0.1", tmp_path)


@pytest.mark.parametrize(
    ("tag", "title"),
    [
        ("v1.0.0", "PaveDB 1.0.0"),
        ("v1.0.0", "PaveDB 1.0.0 — "),
        ("v1.0.1", "PaveDB 1.0.1 — Hotfix"),
    ],
)
def test_rejects_malformed_title_or_name(tmp_path, tag, title):
    write_release(tmp_path, tag, title)
    with pytest.raises(ValueError):
        validate_release_announcement(tag, tmp_path)


def test_rejects_four_component_correction_tag(tmp_path):
    with pytest.raises(ValueError, match="not a standard release tag"):
        validate_release_announcement("v1.0.1.1", tmp_path)
