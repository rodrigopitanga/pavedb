#!/usr/bin/env python3
# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


PUBLIC_KEY_RE = re.compile(r"\b(?:R[1-6]-\d{2}|P\d+-[A-Za-z0-9]+)\b")
REVIEW_KEY_RE = re.compile(r"^(R[1-6])-(\d{2}):")
ROADMAP = Path(__file__).resolve().parents[1] / "ROADMAP.md"
TYPE_LABELS = {"FEAT", "REV", "BUG"}
ZERO_SHA = "0" * 40


class GitLab:
    def __init__(self) -> None:
        self.api_url = os.environ.get("CI_API_V4_URL", "https://gitlab.com/api/v4")
        self.project_id = os.environ.get("CI_PROJECT_ID", "")
        self.token = os.environ.get("GITLAB_ISSUE_TOKEN", "")
        if not self.project_id:
            raise SystemExit("CI_PROJECT_ID is required")
        if not self.token:
            raise SystemExit("GITLAB_ISSUE_TOKEN is required")

    def request(
        self,
        method: str,
        path: str,
        data: dict[str, str] | None = None,
    ) -> object:
        encoded = None
        headers = {"PRIVATE-TOKEN": self.token}
        if data is not None:
            encoded = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"GitLab API {method} {path} failed: {detail}") from exc
        return json.loads(body) if body else {}

    def paged(self, path: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        sep = "&" if "?" in path else "?"
        page = 1
        while True:
            batch = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            out.extend(x for x in batch if isinstance(x, dict))
            if len(batch) < 100:
                break
            page += 1
        return out


def sh(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def marker_value(text: str, name: str) -> str:
    match = re.search(rf"<!--\s*{re.escape(name)}:\s*(.*?)\s*-->", text)
    return match.group(1).strip() if match else ""


def public_keys(text: str) -> set[str]:
    return set(PUBLIC_KEY_RE.findall(text or ""))


def local_commit_texts() -> dict[str, str]:
    sha = os.environ.get("CI_COMMIT_SHA", "")
    before = os.environ.get("CI_COMMIT_BEFORE_SHA", "")
    if not sha:
        return {}

    rev = sha
    if before and before != ZERO_SHA:
        rev = f"{before}..{sha}"

    try:
        raw = sh("git", "log", "--format=%H%x00%s%x00%b%x01", rev)
    except (OSError, subprocess.CalledProcessError):
        msg = os.environ.get("CI_COMMIT_MESSAGE", "")
        return {sha: msg} if msg else {}

    texts: dict[str, str] = {}
    for record in raw.rstrip("\x01").split("\x01"):
        parts = record.strip("\n").split("\x00", 2)
        if len(parts) == 3:
            commit_sha, subject, body = parts
            texts[commit_sha] = f"{subject}\n\n{body}".strip()
    return texts


def mr_texts(gl: GitLab) -> tuple[dict[str, str], dict[str, str]]:
    iid = os.environ.get("CI_MERGE_REQUEST_IID", "")
    if not iid:
        return {}, {}

    mr_path = f"/projects/{gl.project_id}/merge_requests/{iid}"
    mr = gl.request("GET", mr_path)
    mr_refs: dict[str, str] = {}
    if isinstance(mr, dict):
        title = str(mr.get("title") or "")
        desc = str(mr.get("description") or "")
        web_url = str(mr.get("web_url") or "")
        mr_refs[f"!{iid}"] = f"{title}\n\n{desc}\n\n{web_url}".strip()

    commits: dict[str, str] = {}
    for commit in gl.paged(f"{mr_path}/commits"):
        sha = str(commit.get("id") or "")
        title = str(commit.get("title") or "")
        message = str(commit.get("message") or title)
        if sha:
            commits[sha] = message
    return mr_refs, commits


def commit_mr_texts(gl: GitLab, sha: str) -> dict[str, str]:
    path_sha = urllib.parse.quote(sha, safe="")
    path = f"/projects/{gl.project_id}/repository/commits/{path_sha}/merge_requests"
    refs: dict[str, str] = {}
    for mr in gl.paged(path):
        iid = str(mr.get("iid") or "")
        if not iid:
            continue
        title = str(mr.get("title") or "")
        desc = str(mr.get("description") or "")
        web_url = str(mr.get("web_url") or "")
        refs[f"!{iid}"] = f"{title}\n\n{desc}\n\n{web_url}".strip()
    return refs


def title_key(title: str) -> str:
    match = re.match(r"^((?:R[1-6]-\d{2}|P\d+-[A-Za-z0-9]+)):", title)
    return match.group(1) if match else ""


def issue_map(gl: GitLab) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for issue in gl.paged(f"/projects/{gl.project_id}/issues?state=all"):
        title = str(issue.get("title") or "")
        key = title_key(title)
        if key:
            found[key] = issue
    return found


def validate_review_keys(gl: GitLab) -> None:
    # Review keys are durable public IDs. Closed issues still reserve their
    # numeric suffix, so validate against state=all before any sync work.
    # R6 is the zero-weight/refuted trace wave; R0 is not a valid review key.
    by_number: dict[int, list[str]] = {}
    keys: list[str] = []
    for issue in gl.paged(f"/projects/{gl.project_id}/issues?state=all"):
        title = str(issue.get("title") or "")
        match = REVIEW_KEY_RE.match(title)
        if not match:
            continue
        key = f"{match.group(1)}-{match.group(2)}"
        number = int(match.group(2))
        by_number.setdefault(number, []).append(key)
        keys.append(key)

    duplicate_numbers = {
        number: sorted(set(values))
        for number, values in by_number.items()
        if len(set(values)) > 1 or len(values) > 1
    }
    if duplicate_numbers:
        detail = ", ".join(
            f"{number:02d}: {'/'.join(values)}"
            for number, values in sorted(duplicate_numbers.items())
        )
        raise RuntimeError(
            "Review issue keys must use one global numeric counter across "
            "waves, including closed issues; duplicate suffixes found: "
            f"{detail}"
        )

    if keys:
        numbers = sorted(by_number)
        expected = list(range(1, numbers[-1] + 1))
        if numbers != expected:
            missing = sorted(set(expected) - set(numbers))
            raise RuntimeError(
                "Review issue keys must be sequential across waves; "
                "missing suffixes: "
                + ", ".join(f"{number:02d}" for number in missing)
            )


def note_exists(gl: GitLab, issue_iid: int, marker: str) -> bool:
    path = f"/projects/{gl.project_id}/issues/{issue_iid}/notes"
    for note in gl.paged(path):
        if marker in str(note.get("body") or ""):
            return True
    return False


def add_note(gl: GitLab, issue_iid: int, body: str, marker: str) -> None:
    if note_exists(gl, issue_iid, marker):
        return
    gl.request(
        "POST",
        f"/projects/{gl.project_id}/issues/{issue_iid}/notes",
        {"body": f"{body}\n\n{marker}"},
    )


def commit_url(sha: str) -> str:
    base = os.environ.get("CI_PROJECT_URL", "")
    return f"{base}/-/commit/{sha}" if base else sha


def clean_cell(cell: str) -> str:
    cell = cell.strip()
    cell = re.sub(r"<!--.*?-->", "", cell)
    cell = cell.replace("~~", "")
    cell = re.sub(r"`([^`]*)`", r"\1", cell)
    cell = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cell)
    cell = re.sub(r"<br\s*/?>", " ", cell, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cell).strip()


def roadmap_rows() -> list[dict[str, str]]:
    if not ROADMAP.exists():
        return []

    rows: list[dict[str, str]] = []
    headers: list[str] = []
    for line in ROADMAP.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if cells and cells[0] == "ID":
            headers = [clean_cell(cell) for cell in cells]
            continue
        if not headers or len(cells) != len(headers):
            continue
        data = {headers[i]: clean_cell(cells[i]) for i in range(len(headers))}
        issue_id = data.get("ID", "")
        if re.fullmatch(r"P\d+-[A-Za-z0-9]+", issue_id):
            rows.append(data)
    return rows


def issue_body_for_roadmap(row: dict[str, str]) -> str:
    issue_id = row["ID"]
    title = row.get("Task", issue_id)
    description = row.get("Why it matters") or row.get("Why it blocks") or title
    source = row.get("Source", "")
    effort = row.get("Effort", "")

    body = [
        f"<!-- flowlexi-task-id: {issue_id} -->",
        "<!-- flowlexi-source: pavedb-roadmap -->",
        "",
        "## Description",
        description,
        "",
        "## Source",
        f"- Source: ROADMAP.md, roadmap table row {issue_id}",
    ]
    if source:
        body.append(f"- Target: {source}")
    if effort:
        body.append(f"- Effort: {effort}")
    return "\n".join(body)


def ensure_label(gl: GitLab, name: str, color: str) -> None:
    for label in gl.paged(f"/projects/{gl.project_id}/labels"):
        if label.get("name") == name:
            return
    gl.request(
        "POST",
        f"/projects/{gl.project_id}/labels",
        {"name": name, "color": color},
    )


def labels_with_type(labels: list[str], wanted: str) -> list[str]:
    out = [label for label in labels if label not in TYPE_LABELS]
    if wanted not in out:
        out.append(wanted)
    return sorted(out)


def sync_roadmap_issues(gl: GitLab, issues: dict[str, dict[str, object]]) -> None:
    ensure_label(gl, "FEAT", "#33A02C")
    created: list[str] = []
    updated: list[str] = []
    for row in roadmap_rows():
        issue_id = row["ID"]
        title = f"{issue_id}: {row.get('Task', issue_id)}"
        body = issue_body_for_roadmap(row)
        existing = issues.get(issue_id)
        if existing is None:
            gl.request(
                "POST",
                f"/projects/{gl.project_id}/issues",
                {
                    "title": title,
                    "description": body,
                    "labels": "FEAT",
                },
            )
            created.append(issue_id)
            continue

        labels = [str(label) for label in existing.get("labels", [])]
        new_labels = labels_with_type(labels, "FEAT")
        fields: dict[str, str] = {}
        if sorted(labels) != new_labels:
            fields["labels"] = ",".join(new_labels)
        if fields:
            gl.request(
                "PUT",
                f"/projects/{gl.project_id}/issues/{existing['iid']}",
                fields,
            )
            updated.append(issue_id)

    if created:
        print("Created roadmap issues: " + ", ".join(created))
    if updated:
        print("Updated roadmap issue labels: " + ", ".join(updated))


def sync() -> int:
    gl = GitLab()
    validate_review_keys(gl)
    issues = issue_map(gl)
    default_branch = os.environ.get("CI_DEFAULT_BRANCH", "main")
    branch = os.environ.get("CI_COMMIT_BRANCH", "")
    on_default = bool(branch and branch == default_branch)
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID", "")

    mr_refs, commit_refs = mr_texts(gl) if mr_iid else ({}, local_commit_texts())
    if on_default:
        for sha in commit_refs:
            mr_refs.update(commit_mr_texts(gl, sha))
        sync_roadmap_issues(gl, issues)
    matched: set[str] = set()

    for ref, text in mr_refs.items():
        for key in sorted(public_keys(text)):
            issue = issues.get(key)
            if not issue:
                print(f"{key}: no matching issue found", file=sys.stderr)
                continue
            issue_iid = int(issue["iid"])
            title = str(issue.get("title") or key)
            marker = f"<!-- flowlexi-issue-sync: {key}:mr:{ref.lstrip('!')} -->"
            verb = "landed on the default branch" if on_default else "references"
            add_note(
                gl,
                issue_iid,
                f"{ref} {verb} {key}.\n\n{title}",
                marker,
            )
            matched.add(key)
            if on_default and issue.get("state") != "closed":
                gl.request(
                    "PUT",
                    f"/projects/{gl.project_id}/issues/{issue_iid}",
                    {"state_event": "close"},
                )

    for sha, text in commit_refs.items():
        for key in sorted(public_keys(text)):
            issue = issues.get(key)
            if not issue:
                print(f"{key}: no matching issue found", file=sys.stderr)
                continue
            issue_iid = int(issue["iid"])
            title = str(issue.get("title") or key)
            short = sha[:12]
            marker = f"<!-- flowlexi-issue-sync: {key}:commit:{sha} -->"
            if on_default:
                body = (
                    f"Commit {short} landed on `{default_branch}` for {key}.\n\n"
                    f"{commit_url(sha)}\n\n{title}"
                )
            else:
                body = f"Commit {short} references {key}.\n\n{commit_url(sha)}"
            add_note(gl, issue_iid, body, marker)
            matched.add(key)
            if on_default and issue.get("state") != "closed":
                gl.request(
                    "PUT",
                    f"/projects/{gl.project_id}/issues/{issue_iid}",
                    {"state_event": "close"},
                )

    if matched:
        print("Synced issues: " + ", ".join(sorted(matched)))
    else:
        print("No issue keys found")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(sync())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
