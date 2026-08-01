import pytest

from scripts.validate_release_announcement import validate_release_announcement


def write_release(
    tmp_path,
    tag,
    title,
    english="Reviewed body.",
    portuguese="Texto revisado.",
    pt_title=None,
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / f"RELEASE-{tag}.md").write_text(
        f"# {title}\n\n## English\n\n{english}\n", encoding="utf-8"
    )
    (docs / f"RELEASE-{tag}_pt.md").write_text(
        f"# {pt_title or title}\n\n## Português (Brasil)\n\n{portuguese}\n",
        encoding="utf-8",
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


@pytest.mark.parametrize(
    ("tag", "title"),
    [
        ("v0.5.4", "PatchVec 0.5.4"),
        ("v0.5.5", "PatchVec 0.5.5"),
        ("v0.5.6", "PatchVec 0.5.6"),
        ("v0.5.7", "PatchVec 0.5.7 — Readiness"),
        ("v0.5.9", "PatchVec 0.5.9 — Relevance"),
    ],
)
def test_historical_patchvec_titles(tmp_path, tag, title):
    write_release(tmp_path, tag, title)
    assert validate_release_announcement(tag, tmp_path) == title


def test_rejects_historical_patchvec_name_on_another_tag(tmp_path):
    write_release(tmp_path, "v0.5.8", "PatchVec 0.5.7 — Readiness")
    with pytest.raises(ValueError):
        validate_release_announcement("v0.5.8", tmp_path)


def test_missing_release_companion(tmp_path):
    write_release(tmp_path, "v1.0.1", "PaveDB 1.0.1")
    (tmp_path / "docs" / "RELEASE-v1.0.1_pt.md").unlink()
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


@pytest.mark.parametrize(
    ("suffix", "body", "message"),
    [
        ("", "## English\n", "empty locale section: ## English"),
        (
            "_pt",
            "## Português (Brasil)\n",
            "empty locale section: ## Português",
        ),
    ],
)
def test_requires_non_empty_owned_locale(tmp_path, suffix, body, message):
    write_release(tmp_path, "v1.0.1", "PaveDB 1.0.1")
    path = tmp_path / "docs" / f"RELEASE-v1.0.1{suffix}.md"
    path.write_text(f"# PaveDB 1.0.1\n\n{body}", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        validate_release_announcement("v1.0.1", tmp_path)


@pytest.mark.parametrize(
    ("suffix", "body", "message"),
    [
        (
            "",
            "## English\n\nText.\n\n## Português (Brasil)\n\nTexto.\n",
            "companion: .*Português",
        ),
        (
            "_pt",
            "## Português (Brasil)\n\nTexto.\n\n## English\n\nText.\n",
            "companion: .*English",
        ),
        (
            "",
            "## English\n\nText.\n\n## Français\n\nTexte.\n",
            "unexpected locale section",
        ),
    ],
)
def test_rejects_locale_heading_in_wrong_file(tmp_path, suffix, body, message):
    write_release(tmp_path, "v1.0.1", "PaveDB 1.0.1")
    path = tmp_path / "docs" / f"RELEASE-v1.0.1{suffix}.md"
    path.write_text(f"# PaveDB 1.0.1\n\n{body}", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        validate_release_announcement("v1.0.1", tmp_path)


def test_rejects_mismatched_release_h1(tmp_path):
    write_release(
        tmp_path,
        "v1.0.1",
        "PaveDB 1.0.1",
        pt_title="PaveDB 1.0.2",
    )
    with pytest.raises(ValueError, match="release H1 titles must match"):
        validate_release_announcement("v1.0.1", tmp_path)


def test_rejects_four_component_correction_tag(tmp_path):
    with pytest.raises(ValueError, match="not a standard release tag"):
        validate_release_announcement("v1.0.1.1", tmp_path)
