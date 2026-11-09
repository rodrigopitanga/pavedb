#!/usr/bin/env python3
# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import datetime
import email.message
import email.utils
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# Issue keys: R review, P PaveDB feature, B/F/G bug/Flymmatik/Patchgram.
# Shared by commit notes, exact close trailers, and issue titles so public key
# recognition cannot drift between those paths.
_KEY_BODY = r"R[1-9]\d*-\d+|P\d+-[A-Za-z0-9]+|[BFG]\d+(?:-[A-Za-z0-9]+)?"
PUBLIC_KEY_RE = re.compile(rf"\b(?:{_KEY_BODY})\b")
REVIEW_KEY_RE = re.compile(r"^(R[1-9]\d*)-(\d+):")
ROADMAP = Path(__file__).resolve().parents[1] / "ROADMAP.md"
TYPE_LABELS = {"FEAT", "REV", "BUG"}
ZERO_SHA = "0" * 40
CHAOS_FAILURE_MARKER = "<!-- flowlexi-ci-failure: pavedb-chaos-level1 -->"
ROADMAP_BODY_HASH = "flowlexi-roadmap-body-sha256"
SOURCE_BULLET_RE = re.compile(r"-\s+(?:Source|Target|Effort):\s")
RETRY_AFTER_CAP = 120


def retry_after_delay(headers: email.message.Message | None, fallback: float) -> float:
    """Seconds to wait before retrying a throttled GitLab response.

    ``Retry-After`` carries either delta-seconds or an HTTP-date (RFC 9110
    10.2.3) and the sender picks the form, so reading only the integer form
    left date-form replies on the 1-2s exponential backoff this retry policy
    exists to replace. Values that are unparsable, non-finite, zero or already
    past fall back to that backoff rather than reaching ``time.sleep``, which
    rejects them -- a negative header would otherwise abort the sync run this
    loop is meant to save.
    """
    raw = (headers.get("Retry-After") or "").strip() if headers else ""
    if not raw:
        return fallback
    try:
        delay = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return fallback
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        delay = (when - now).total_seconds()
    if not math.isfinite(delay) or delay <= 0:
        return fallback
    return min(delay, RETRY_AFTER_CAP)


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
        # Retry transient failures so a single API hiccup does not orphan a
        # push's issue keys (the root cause of un-closed, already-fixed issues:
        # the old code raised on the first error with no retry, dropping the
        # whole push's sync). Backoff only fires on failure, so the happy path
        # keeps its original cost.
        # A 429 carries Retry-After; waiting less than that only burns the
        # attempt. A scheduled pipeline on the default branch matches both
        # issue-key-sync and issue-reconcile, so two runs of this script
        # spend one GITLAB_ISSUE_TOKEN rate budget at once; the header wins
        # over the backoff whenever it parses to a usable wait.
        last: Exception | None = None
        for attempt in range(5):
            req = urllib.request.Request(
                f"{self.api_url}{path}",
                data=encoded,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode()
                return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                    last = exc
                    time.sleep(retry_after_delay(exc.headers, 2**attempt))
                    continue
                detail = exc.read().decode(errors="replace")
                raise RuntimeError(
                    f"GitLab API {method} {path} failed: {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < 4:
                    last = exc
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"GitLab API {method} {path} failed: {exc}") from exc
            except TimeoutError as exc:
                if method == "GET" and attempt < 4:
                    last = exc
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"GitLab API {method} {path} failed: {exc}") from exc
        raise RuntimeError(f"GitLab API {method} {path} failed after retries: {last}")

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


def closing_keys(text: str) -> set[str]:
    lines = (text or "").splitlines()[1:]
    pattern = re.compile(rf"Closes: ({_KEY_BODY})")
    while lines and not lines[-1]:
        lines.pop()
    keys: set[str] = set()
    while lines and (match := pattern.fullmatch(lines[-1])):
        keys.add(match.group(1))
        lines.pop()
    return keys


def subject_keys(text: str) -> set[str]:
    """The issue keys a commit fixes: a trailing ``(KEY)`` in the SUBJECT
    line. That is the repo convention (``[fix] ... (R3-77)``); a key merely
    referenced mid-body does not count as "this commit fixes it", which avoids
    the over-close the ``Closes:`` gate was added to prevent without requiring a
    trailer nobody writes. (Leading ``KEY:`` is the issue-title format, not a
    commit format, so it is intentionally not matched here.)

    One key per commit is the convention; a list (``(B41, B42)``) is accepted
    so a commit that had to fix several still closes them all. ``(refs ...)``
    only references, so it never matches.
    """
    subject = (text or "").splitlines()[0] if text else ""
    trail = re.search(
        rf"\(((?:{_KEY_BODY})(?:,\s*(?:{_KEY_BODY}))*)\)\s*$", subject
    )
    return set(re.split(r",\s*", trail.group(1))) if trail else set()


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


def mr_texts(gl: GitLab) -> dict[str, str]:
    """Commit messages for the current merge request, keyed by SHA.

    Issue keys are sourced only from commit messages, never the MR title
    or description: a key merely referenced in the description (a deferred
    item, an "earlier wave" list, a context note) must not get a sync note
    or be auto-closed. A trailing subject key identifies the completed issue.
    """
    iid = os.environ.get("CI_MERGE_REQUEST_IID", "")
    if not iid:
        return {}

    mr_path = f"/projects/{gl.project_id}/merge_requests/{iid}"
    commits: dict[str, str] = {}
    for commit in gl.paged(f"{mr_path}/commits"):
        sha = str(commit.get("id") or "")
        title = str(commit.get("title") or "")
        message = str(commit.get("message") or title)
        if sha:
            commits[sha] = message
    return commits


def title_key(title: str) -> str:
    match = re.match(rf"^((?:{_KEY_BODY})):", title)
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
    for issue in gl.paged(f"/projects/{gl.project_id}/issues?state=all"):
        title = str(issue.get("title") or "")
        match = REVIEW_KEY_RE.match(title)
        if not match:
            continue
        key = f"{match.group(1)}-{match.group(2)}"
        number = int(match.group(2))
        by_number.setdefault(number, []).append(key)

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

    if by_number:
        missing = [
            number for number in range(1, max(by_number) + 1)
            if number not in by_number
        ]
        if missing:
            detail = ", ".join(f"{number:02d}" for number in missing)
            print(
                "Review issue keys have reserved or skipped suffixes: "
                f"{detail}",
                file=sys.stderr,
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


def closed_by_marker(key: str) -> str:
    return f"<!-- flowlexi-issue-sync: {key}:closed-by -->"


def add_closed_by(gl: GitLab, issue_iid: int, key: str, sha: str) -> None:
    """One ``Closed by:`` note per issue, so an issue maps back to its fix."""
    add_note(
        gl,
        issue_iid,
        f"Closed by: [`{sha[:8]}`]({commit_url(sha)})",
        closed_by_marker(key),
    )


def has_closed_by(gl: GitLab, issue_iid: int, key: str) -> bool:
    """A ``Closed by:`` note, or the reconcile ``Fixed on`` note that already
    names the fixing commit."""
    path = f"/projects/{gl.project_id}/issues/{issue_iid}/notes"
    for note in gl.paged(path):
        body = str(note.get("body") or "")
        if (
            closed_by_marker(key) in body
            or body.startswith("Closed by:")
            or body.startswith("Fixed on `")
        ):
            return True
    return False


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
        if not headers:
            continue
        if len(cells) != len(headers):
            issue_id = clean_cell(cells[0])
            if issue_id.startswith(("P", "F", "G")) and PUBLIC_KEY_RE.fullmatch(
                issue_id
            ):
                raise RuntimeError(
                    f"ROADMAP row {issue_id} has {len(cells)} cells; "
                    f"expected {len(headers)}"
                )
            continue
        data = {headers[i]: clean_cell(cells[i]) for i in range(len(headers))}
        issue_id = data.get("ID", "")
        if re.fullmatch(r"P\d+-[A-Za-z0-9]+", issue_id):
            task = cells[headers.index("Task")] if "Task" in headers else ""
            data["Status"] = (
                "complete" if re.fullmatch(r"~~(?:(?!~~).)+~~", task) else "open"
            )
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
    generated = "\n".join(body)
    # A later sync may replace only a body that still matches this digest.
    digest = hashlib.sha256(generated.encode("utf-8")).hexdigest()
    body.insert(2, f"<!-- {ROADMAP_BODY_HASH}: {digest} -->")
    return "\n".join(body)


def refresh_roadmap_source(body: str, row: dict[str, str]) -> str:
    """Refresh source metadata without replacing an issue's curated prose."""
    generated = issue_body_for_roadmap(row)
    source = generated.split("## Source\n", 1)[1]
    lines = [
        line for line in body.splitlines()
        if not re.fullmatch(
            r"<!--\s*(?:flowlexi-task-id|flowlexi-source|"
            rf"{ROADMAP_BODY_HASH}):.*?-->", line.strip()
        )
    ]
    while lines and not lines[0].strip():
        lines.pop(0)
    markers = generated.splitlines()[:2]
    for index, line in enumerate(lines):
        if line.strip() == "## Source":
            # This rewrite owns only the generated bullets, so curated prose,
            # snippets and subsections under Source survive the refresh.
            end = index + 1
            while end < len(lines) and (
                not lines[end].strip()
                or SOURCE_BULLET_RE.match(lines[end].strip())
            ):
                end += 1
            lines[index:end] = ["## Source", source, ""]
            break
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.extend(["## Source", source])
    return "\n".join(markers + ["", *lines]).strip()


def generated_roadmap_body(body: str, row: dict[str, str]) -> bool:
    """Only a body with intact generation proof may be replaced wholesale."""
    digest = marker_value(body, ROADMAP_BODY_HASH)
    if digest:
        original = re.sub(
            rf"^<!-- {ROADMAP_BODY_HASH}: [^\n]+ -->\n",
            "",
            body,
            count=1,
            flags=re.MULTILINE,
        )
        return hashlib.sha256(original.encode("utf-8")).hexdigest() == digest
    legacy = issue_body_for_roadmap(row)
    legacy = re.sub(
        rf"^<!-- {ROADMAP_BODY_HASH}: [^\n]+ -->\n",
        "",
        legacy,
        count=1,
        flags=re.MULTILINE,
    )
    return body == legacy


def ensure_label(gl: GitLab, name: str, color: str) -> None:
    for label in gl.paged(f"/projects/{gl.project_id}/labels"):
        if label.get("name") == name:
            return
    gl.request(
        "POST",
        f"/projects/{gl.project_id}/labels",
        {"name": name, "color": color},
    )


def chaos_failure_body(log: str) -> str:
    seed = os.environ.get("CHAOS_SEED", "1337")
    duration = os.environ.get("CHAOS_DURATION", "120")
    concurrency = os.environ.get("CHAOS_CONCUR", "24")
    config = re.search(
        r"seed=(\S+)\s+duration=(\S+)s\s+concurrency=(\S+)",
        log,
    )
    if config:
        seed, duration, concurrency = config.groups()

    op_counts = re.findall(
        r"^\s*((?:\d+ fuzz cases handled without a 5xx|"
        r"ran \d+s, \d+ checker rounds, \d+ churn exceptions))$",
        log,
        flags=re.MULTILINE,
    )
    failures = re.findall(
        r"^\s+- (\[[^\n]+)$",
        log,
        flags=re.MULTILINE,
    )
    failure_lines = [f"- {failure}" for failure in failures[:20]]
    if len(failures) > 20:
        failure_lines.append(f"- ... {len(failures) - 20} more; see chaos.log")

    pipeline_url = os.environ.get("CI_PIPELINE_URL", "")
    job_url = os.environ.get("CI_JOB_URL", "")
    artifact_url = f"{job_url}/artifacts/file/chaos.log" if job_url else ""
    replay = " ".join(
        (
            "make chaos",
            f"CHAOS_SEED={shlex.quote(seed)}",
            f"CHAOS_DURATION={shlex.quote(duration)}",
            f"CHAOS_CONCUR={shlex.quote(concurrency)}",
        )
    )
    return "\n".join(
        [
            CHAOS_FAILURE_MARKER,
            "",
            "## Latest failure",
            "PaveDB Level-1 nightly chaos failed.",
            "",
            f"- Pipeline: {pipeline_url or 'unavailable'}",
            f"- Job: {job_url or 'unavailable'}",
            f"- chaos.log: {artifact_url or 'unavailable'}",
            f"- Seed: `{seed}`",
            f"- Duration: `{duration}s`",
            f"- Concurrency: `{concurrency}`",
            "- Release blocking: no",
            "- Rerun note: the seed fixes inputs, not thread scheduling.",
            "",
            "## Operation counts",
            *(
                [f"- {count}" for count in op_counts]
                or ["- unavailable; see chaos.log"]
            ),
            "",
            "## Invariant failures",
            *(failure_lines or ["- no invariant summary; see chaos.log"]),
            "",
            "## Rerun",
            f"`{replay}`",
        ]
    )


def sync_chaos_failure() -> int:
    gl = GitLab()
    log_path = Path("chaos.log")
    log = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.exists()
        else ""
    )
    body = chaos_failure_body(log)
    ensure_label(gl, "BUG", "#D9534F")
    ensure_label(gl, "bench", "#1F75CB")
    issues = gl.paged(f"/projects/{gl.project_id}/issues?state=all")
    issue = next(
        (
            issue
            for issue in issues
            if CHAOS_FAILURE_MARKER in str(issue.get("description") or "")
        ),
        None,
    )
    if issue is None:
        bug_numbers = [
            int(key[1:].split("-", 1)[0])
            for issue in issues
            if (key := title_key(str(issue.get("title") or ""))).startswith("B")
        ]
        # ponytail: scheduled CI is the single key writer; add a counter service
        # only if concurrent bug-key minting becomes real.
        key = f"B{max(bug_numbers, default=0) + 1}"
        gl.request(
            "POST",
            f"/projects/{gl.project_id}/issues",
            {
                "title": f"{key}: Nightly PaveDB chaos failure",
                "description": body,
                "labels": "BUG,bench",
            },
        )
        print(f"Created chaos failure issue {key}")
        return 0

    fields = {"description": body}
    if issue.get("state") == "closed":
        fields["state_event"] = "reopen"
    gl.request(
        "PUT",
        f"/projects/{gl.project_id}/issues/{issue['iid']}",
        fields,
    )
    print(f"Updated chaos failure issue {issue['iid']}")
    return 0


def labels_with_type(labels: list[str], wanted: str) -> list[str]:
    out = [label for label in labels if label not in TYPE_LABELS]
    if wanted not in out:
        out.append(wanted)
    return sorted(out)


def sync_roadmap_issues(gl: GitLab, issues: dict[str, dict[str, object]]) -> None:
    ensure_label(gl, "FEAT", "#33A02C")
    milestones = {
        str(milestone.get("title") or ""): str(milestone.get("id") or "")
        for milestone in gl.paged(f"/projects/{gl.project_id}/milestones")
    }
    created: list[str] = []
    updated: list[str] = []
    for row in roadmap_rows():
        issue_id = row["ID"]
        title = f"{issue_id}: {row.get('Task', issue_id)}"
        body = issue_body_for_roadmap(row)
        milestone_id = milestones.get(row.get("Source", ""))
        existing = issues.get(issue_id)
        if existing is None:
            fields = {
                "title": title,
                "description": body,
                "labels": "FEAT",
            }
            if milestone_id:
                fields["milestone_id"] = milestone_id
            gl.request(
                "POST",
                f"/projects/{gl.project_id}/issues",
                fields,
            )
            created.append(issue_id)
            continue

        labels = [str(label) for label in existing.get("labels", [])]
        new_labels = labels_with_type(labels, "FEAT")
        fields: dict[str, str] = {}
        if str(existing.get("title") or "") != title:
            fields["title"] = title
        existing_body = str(existing.get("description") or "")
        next_body = (
            body if generated_roadmap_body(existing_body, row)
            else refresh_roadmap_source(existing_body, row)
        )
        if existing_body != next_body:
            fields["description"] = next_body
        if sorted(labels) != new_labels:
            fields["labels"] = ",".join(new_labels)
        milestone = existing.get("milestone")
        existing_milestone_id = (
            str(milestone.get("id") or "") if isinstance(milestone, dict) else ""
        )
        if milestone_id and not existing_milestone_id:
            fields["milestone_id"] = milestone_id
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
        print("Updated roadmap issues: " + ", ".join(updated))


def closure_keys(text: str, completed: set[str]) -> set[str]:
    """Accept subject keys and optional trailers; require completed P scope."""
    return {
        key
        for key in subject_keys(text) | closing_keys(text)
        if not key.startswith("P") or key in completed
    }


def completed_roadmap_keys() -> set[str]:
    return {row["ID"] for row in roadmap_rows() if row["Status"] == "complete"}


def main_fixes(
    default_branch: str,
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Closing commits and subject fixes from one history scan.

    Requires full history (GIT_DEPTH: 0 in the reconciliation job) and commit
    bodies: subject keys authorize closure without a trailer; completed
    roadmap scope is also required for P keys.
    """
    out = ""
    for ref in (default_branch, f"origin/{default_branch}", f"gitlab/{default_branch}"):
        try:
            out = sh("git", "log", ref, "--format=%H%x00%B%x01")
            break
        except (OSError, subprocess.CalledProcessError):
            continue
    completed = completed_roadmap_keys()
    closers: dict[str, tuple[str, str]] = {}
    subjects: dict[str, tuple[str, str]] = {}
    for record in out.split("\x01"):
        if "\x00" not in record:
            continue
        sha, text = record.strip("\n").split("\x00", 1)
        subject = text.splitlines()[0] if text else ""
        for key in closure_keys(text, completed):
            closers.setdefault(key, (sha, subject))  # newest wins
        for key in subject_keys(text):
            subjects.setdefault(key, (sha, subject))
    return closers, subjects


def reconcile(gl: GitLab, issues: dict[str, dict[str, object]], default_branch: str) -> int:
    """Close any OPEN key-titled issue whose fix already landed on the default
    branch but which an earlier push-scoped run missed. Idempotent: closed
    issues are skipped, so a steady-state run closes nothing and makes no write
    calls. Reuses the already-fetched ``issues`` map — the only added cost is
    one local ``git log``.
    """
    fixes, subject_fixes = main_fixes(default_branch)
    closed: list[str] = []
    unmapped: list[str] = []
    for key, issue in issues.items():
        issue_iid = int(issue["iid"])
        if issue.get("state") == "closed":
            # Backfill: every closed issue names what closed it -- the fix
            # commit when one is on the default branch, else the closer.
            # ponytail: one note lookup per closed issue per scheduled run;
            # cache "already has closed-by" if the issue count grows.
            if has_closed_by(gl, issue_iid, key):
                continue
            fix = fixes.get(key) or subject_fixes.get(key)
            if fix:
                add_closed_by(gl, issue_iid, key, fix[0])
                continue
            closer = (issue.get("closed_by") or {}).get("username") or "unknown"
            add_note(
                gl,
                issue_iid,
                f"Closed by: @{closer}, with no eligible fixing commit on "
                f"`{default_branch}` for {key}.",
                closed_by_marker(key),
            )
            unmapped.append(key)
            continue
        if key not in fixes:
            continue
        sha, subject = fixes[key]
        marker = f"<!-- flowlexi-issue-sync: {key}:reconcile:{sha} -->"
        body = (
            f"Fixed on `{default_branch}` by {sha[:12]} — {subject}.\n\n"
            f"{commit_url(sha)}\n\nReconcile-close: an earlier push-scoped sync "
            f"run did not process this commit."
        )
        add_note(gl, issue_iid, body, marker)
        add_closed_by(gl, issue_iid, key, sha)
        gl.request(
            "PUT",
            f"/projects/{gl.project_id}/issues/{issue_iid}",
            {"state_event": "close"},
        )
        closed.append(key)
    print("Reconcile closed: " + (", ".join(sorted(closed)) if closed else "nothing"))
    if unmapped:
        print(
            "Closed without an eligible fixing commit on the default branch: "
            + ", ".join(sorted(unmapped))
        )
    return 0


def selfcheck() -> int:
    assert closure_keys("[cli] Reject non-object --filters (R3-26)", set()) == {"R3-26"}
    assert closure_keys("[fix] Bounds (B95, R2-104, R7-86)", set()) == {"B95", "R2-104", "R7-86"}
    assert closure_keys("[doc] Reference (refs B95)\n\nbody (R2-104)", set()) == set()
    assert closing_keys("[cli] Reject non-object --filters (R3-26)") == set()
    assert closing_keys("subj\n\nbody\n\nCloses: B15") == {"B15"}
    assert closing_keys("subj\n\nno trailer") == set()
    print("selfcheck ok")
    return 0


def sync(*, reconcile_requested: bool = False) -> int:
    gl = GitLab()
    validate_review_keys(gl)
    issues = issue_map(gl)
    default_branch = os.environ.get("CI_DEFAULT_BRANCH", "main")
    branch = os.environ.get("CI_COMMIT_BRANCH", "")
    on_default = bool(branch and branch == default_branch)
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID", "")

    # Full reconcile (scan the default branch, close every open issue already
    # fixed) is O(history) + touches many issues, so it must NOT run on the
    # frequent commit jobs. Gate it to scheduled pipelines or an explicit
    # local `--reconcile`; commit jobs keep their cheap push-range scope below.
    reconcile_requested = reconcile_requested or (
        os.environ.get("CI_PIPELINE_SOURCE") == "schedule"
        or os.environ.get("ISSUE_SYNC_RECONCILE") == "1"
    )
    if reconcile_requested:
        return reconcile(gl, issues, default_branch)

    commit_refs = mr_texts(gl) if mr_iid else local_commit_texts()
    if on_default:
        sync_roadmap_issues(gl, issues)
    completed = completed_roadmap_keys() if on_default else set()
    matched: set[str] = set()

    for sha, text in commit_refs.items():
        closes = closure_keys(text, completed)
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
            if on_default and key in closes and issue.get("state") != "closed":
                add_closed_by(gl, issue_iid, key, sha)
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


def main(args: list[str]) -> int:
    if "--selfcheck" in args:
        return selfcheck()
    if args == ["chaos-failure"]:
        return sync_chaos_failure()
    if args in ([], ["--reconcile"]):
        return sync(reconcile_requested=bool(args))
    raise SystemExit("usage: issue_key_sync.py [--reconcile|chaos-failure]")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
