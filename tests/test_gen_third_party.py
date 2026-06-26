# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from scripts.gen_third_party import bucket, render

BSD_CLASSIFIER = ["License :: OSI Approved :: BSD License"]
# A real scipy-style metadata.license: the full license *text*, which embeds
# bundled-component notices mentioning (L)GPL. Token-matching it would wrongly
# flag copyleft, so the classifier wins and freetext past 60 chars is ignored.
SCIPY_LICENSE_TEXT = (
    "Copyright (c) 2001-2002 Enthought, Inc.\n"
    "Bundled components are distributed under their own terms including LGPL "
    "and GPL where noted; see the dist-info license files for details."
)


def test_bucket_copyleft_from_short_license():
    assert bucket("", "MPL-2.0", []) == "copyleft_or_weak_copyleft"
    assert bucket("", "MPL-2.0 AND MIT", []) == "copyleft_or_weak_copyleft"
    assert bucket("AGPL-3.0-or-later", "", []) == "copyleft_or_weak_copyleft"


def test_bucket_permissive_from_clean_signal():
    assert bucket("MIT", "", []) == "permissive_or_other"
    assert bucket("BSD-3-Clause AND 0BSD", "", []) == "permissive_or_other"
    assert bucket("", "", BSD_CLASSIFIER) == "permissive_or_other"


def test_bucket_scipy_longtext_stays_permissive():
    # The copyleft tokens live only in the >60-char freetext, which is ignored;
    # the BSD classifier decides.
    assert bucket("", SCIPY_LICENSE_TEXT, BSD_CLASSIFIER) == "permissive_or_other"


def test_bucket_unknown_is_manual_review_not_permissive():
    assert bucket("", "", []) == "manual_review"
    assert bucket("", "Proprietary-ish freetext with no SPDX token here", []) == (
        "manual_review"
    )


def _entry(name, version, *, expr="", lic="", classifiers=(), requested=False):
    return {
        "requested": requested,
        "metadata": {
            "name": name,
            "version": version,
            "license_expression": expr,
            "license": lic,
            "classifier": list(classifiers),
        },
    }


def test_render_excludes_root_and_surfaces_copyleft():
    installs = [
        _entry("pavedb", "0.9.2", lic="AGPL-3.0-or-later", requested=True),
        _entry("certifi", "2026.6.17", lic="MPL-2.0"),
        _entry("numpy", "2.5.0", expr="BSD-3-Clause"),
    ]
    out = render(installs, "0.9.2")
    assert "60 third-party" not in out  # count reflects the 2 deps below
    assert "resolved by pip, 2 third-party packages" in out
    assert "`copyleft_or_weak_copyleft`: 1" in out
    # root project is documented in the header, never as a dependency row
    assert "| pavedb |" not in out
    assert "| certifi |" in out
    assert "*.dist-info" in out  # bundled-binary note retained
    # doc is CPU-scoped and must caveat the GPU image's proprietary CUDA deps
    assert "CPU runtime closure" in out
    assert "NVIDIA" in out
