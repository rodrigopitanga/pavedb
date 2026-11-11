# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import pathlib
import subprocess
import sys

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "update_changelog.py"


def _commit(repo, msg, env):
    subprocess.check_call(["git", "commit", "-q", "--allow-empty", "-m", msg], cwd=repo, env=env)


def test_changelog_thanks_reporters_from_trailers(tmp_path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.check_call(["git", "init", "-q"], cwd=tmp_path, env=env)
    _commit(tmp_path, "[core] base", env)
    subprocess.check_call(["git", "tag", "v1.0.0"], cwd=tmp_path, env=env)
    _commit(tmp_path, "[store][fix] a (B1)\n\nReported-by: Ann (@ann)", env)
    _commit(tmp_path, "chore(test): b\n\nReported-by: Bo <bo@x.org>\nReported-by: Ann (@ann)", env)
    _commit(tmp_path, "[api] c", env)
    subprocess.check_call([sys.executable, str(_SCRIPT), "1.1.0"], cwd=tmp_path, env=env,
                          stdout=subprocess.DEVNULL)
    text = (tmp_path / "CHANGELOG.md").read_text()
    assert ("Thanks to the developers who reported or contributed issue reports: "
            "Ann (@ann), Bo <bo@x.org>.") in text
