#!/usr/bin/env python3
# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chaos + correctness suite for PaveDB — a P1-52 (Level-1) prototype.

Drives a running PaveDB server and asserts invariants the error-rate stress
harness (benchmarks/stress.py) is blind to: silent data corruption and
cross-collection bleed (a 200-OK can still return wrong data). Three phases:

  Phase 1 - correctness invariants (sequential): ingest-count integrity,
    search visibility, cross-collection isolation, delete-removes-from-search,
    re-ingest-is-update, byte-exact chunk round-trip, catalog<->disk agreement.
  Phase 2 - fuzz: malformed / empty / oversized inputs (control bytes, bad
    JSON, huge docids/metadata, degenerate k, large docs + batches) must return
    a clean 4xx or 2xx - never a 5xx - and leave the server live + uncorrupted.
  Phase 3 - concurrency chaos: a never-touched "stable" doc set must stay
    retrievable and no sibling doc may ever bleed into a collection while N
    threads churn deletes / re-ingests and collection create+delete+move.

Reproducible via --seed (controls collection names + the op distribution;
thread interleaving is inherently nondeterministic). Exits non-zero on any
invariant violation, so it doubles as a regression gate for the v1.0 fixes.

Usage:
    python benchmarks/chaos.py [--url URL] [--seed N] [--duration S]
                               [--concurrency C] [--api-key TOKEN]
                               [--data-dir PATH]
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import httpx
except ImportError:
    raise SystemExit("httpx required: pip install httpx")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import local_hw  # type: ignore[import]  # noqa: E402

TENANT = "chaos"
API = "/v1"

# Semantically DISTINCT docs: each keyword retrieves exactly its own doc. A
# near-duplicate corpus makes a semantic model rank ties and produces false
# "missing doc" reports - do not replace these with numbered variants.
CORPUS = [
    ("photosynthesis", "Chlorophyll drives photosynthesis in green leaves."),
    ("volcano", "A volcano erupts molten basalt lava from the mantle."),
    ("saxophone", "The jazz saxophone solo echoed through the club."),
    ("glacier", "The arctic glacier calved icebergs into the fjord."),
    ("espresso", "A barista pulled a rich espresso shot with crema."),
    ("telescope", "The orbital telescope photographed a spiral galaxy."),
    ("marathon", "She finished the Boston marathon in record time."),
    ("origami", "He folded an intricate origami crane from paper."),
    ("submarine", "The nuclear submarine dove beneath the ice cap."),
    ("cinnamon", "Grandmother's recipe calls for cinnamon and nutmeg."),
    ("tarantula", "A hairy tarantula crept across the desert sand."),
    ("cathedral", "The gothic cathedral's buttresses soared skyward."),
    ("kayak", "They paddled a kayak through the whitewater rapids."),
    ("pottery", "The artisan shaped clay pottery on a spinning wheel."),
    ("avalanche", "The skiers triggered an avalanche on the couloir."),
    ("harmonica", "A blues harmonica wailed over the train rhythm."),
    ("vineyard", "The Tuscan vineyard produced an award-winning chianti."),
    ("lighthouse", "The coastal lighthouse warned ships off the shoals."),
    ("rhinoceros", "A black rhinoceros grazed on the savanna plain."),
    ("blacksmith", "The blacksmith hammered iron on his heavy anvil."),
    ("typhoon", "The typhoon battered the coastline with heavy rain."),
    ("ballerina", "The ballerina performed a pirouette on the stage."),
    ("geyser", "The hot geyser erupted boiling water into the air."),
    ("accordion", "A street musician squeezed an accordion polka."),
]
SIBLING_KEYWORDS = [
    "nebula", "tsunami", "pyramid", "walrus", "tractor", "lantern",
    "cobra", "dynamo", "obsidian", "marigold",
]


def _name(rng, prefix: str) -> str:
    # Seeded, conforms to ^[a-z0-9][a-z0-9-]{0,62}$.
    return f"{prefix}-{rng.randbytes(5).hex()}"


def _client(url: str, api_key: str | None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.Client(
        base_url=url, headers=headers, timeout=30,
        limits=httpx.Limits(max_connections=64),
    )


def _ingest(c: httpx.Client, coll: str, docid: str, text: str) -> httpx.Response:
    return c.post(
        f"{API}/collections/{TENANT}/{coll}/documents",
        files={"file": (f"{docid}.txt", text.encode(), "text/plain")},
        data={"docid": docid},
    )


def _move_collection(c: httpx.Client, coll: str, new_name: str) -> httpx.Response:
    return c.post(
        f"{API}/admin/collections/{TENANT}/{coll}/move",
        json={"new_name": new_name},
    )


def _search(c: httpx.Client, coll: str, q: str, k: int = 8):
    r = c.post(f"{API}/collections/{TENANT}/{coll}/search", json={"q": q, "k": k})
    docids = (
        [m["meta"]["docid"] for m in r.json().get("matches", [])]
        if r.status_code == 200 else []
    )
    return r.status_code, docids


def _is_ingest_overload(resp: httpx.Response) -> bool:
    try:
        return (
            resp.status_code == 503
            and resp.json().get("code") == "ingest_overloaded"
        )
    except ValueError:
        return False


def catalog_disk_violations(data_dir: str | os.PathLike[str]) -> list[str]:
    root = Path(data_dir)
    catalog = root / "catalog.db"
    if not catalog.is_file():
        return [f"catalog.db missing under {root}"]

    with sqlite3.connect(f"file:{catalog}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT tenant, name FROM collections").fetchall()
    cataloged = {(str(tenant), str(name)) for tenant, name in rows}

    on_disk: set[tuple[str, str]] = set()
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir() or not tenant_dir.name.startswith("t_"):
            continue
        tenant = tenant_dir.name[2:]
        for coll_dir in tenant_dir.iterdir():
            if (
                coll_dir.is_dir()
                and coll_dir.name.startswith("c_")
                and (coll_dir / "meta.db").is_file()
            ):
                on_disk.add((tenant, coll_dir.name[2:]))

    violations = [
        f"catalog row missing disk collection {tenant}/{name}"
        for tenant, name in sorted(cataloged - on_disk)
    ]
    violations.extend(
        f"disk collection missing catalog row {tenant}/{name}"
        for tenant, name in sorted(on_disk - cataloged)
    )
    return violations


def _ingest_raw(c: httpx.Client, coll: str, content: bytes, *,
                docid: str = "f", filename: str = "f.txt",
                metadata: str | None = None) -> httpx.Response:
    """Ingest raw bytes (lets fuzz send control chars / non-UTF-8 / huge)."""
    data: dict[str, str] = {"docid": docid}
    if metadata is not None:
        data["metadata"] = metadata
    return c.post(
        f"{API}/collections/{TENANT}/{coll}/documents",
        files={"file": (filename, content, "text/plain")},
        data=data,
    )


# ---------------------------------------------------------------------------
# Phase 1 - sequential correctness invariants
# ---------------------------------------------------------------------------
def phase1_correctness(c: httpx.Client, rng, viol: list[str]) -> None:
    def check(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "FAIL  ") + msg)
        if not cond:
            viol.append(f"[correctness] {msg}")

    print("\n[phase 1] correctness invariants")
    a = _name(rng, "chaos-a")
    c.post(f"{API}/collections/{TENANT}/{a}")
    for kw, text in CORPUS:
        r = _ingest(c, a, f"d-{kw}", text)
        if r.status_code != 201:
            viol.append(f"[correctness] ingest d-{kw} -> {r.status_code}")
    lst = c.get(f"{API}/collections/{TENANT}/{a}/documents").json()
    check(lst["count"] == len(CORPUS), f"count == {len(CORPUS)} (got {lst['count']})")

    miss = [kw for kw, _ in CORPUS if f"d-{kw}" not in _search(c, a, kw, 3)[1]]
    check(not miss, f"every keyword retrieves its own doc top-3 (missed {miss})")

    b = _name(rng, "chaos-b")
    c.post(f"{API}/collections/{TENANT}/{b}")
    for kw, text in CORPUS[:12]:
        _ingest(c, b, f"b-{kw}", text)  # SAME content in b
    a_bleed = [kw for kw, _ in CORPUS[:12]
               if any(d.startswith("b-") for d in _search(c, a, kw, 5)[1])]
    b_bleed = [kw for kw, _ in CORPUS[:12]
               if any(d.startswith("d-") for d in _search(c, b, kw, 5)[1])]
    check(not a_bleed, f"A-search never returns B-docs (bleed {a_bleed})")
    check(not b_bleed, f"B-search never returns A-docs (bleed {b_bleed})")

    vkw = "volcano"
    check(f"d-{vkw}" in _search(c, a, vkw, 3)[1], "victim retrievable before delete")
    c.delete(f"{API}/collections/{TENANT}/{a}/documents/d-{vkw}")
    check(
        f"d-{vkw}" not in _search(c, a, vkw, 5)[1],
        "victim gone from search after delete",
    )
    check(c.get(f"{API}/collections/{TENANT}/{a}/documents").json()["count"]
          == len(CORPUS) - 1, "count decremented after delete")

    rkw = "saxophone"
    _ingest(c, a, f"d-{rkw}", "A different sentence about quantum cryptography keys.")
    check(f"d-{rkw}" in _search(c, a, "quantum cryptography", 3)[1],
          "re-ingested doc retrievable by new content")
    check(f"d-{rkw}" not in _search(c, a, rkw, 5)[1],
          "old keyword no longer returns the re-ingested doc (stale chunk purged)")
    check(c.get(f"{API}/collections/{TENANT}/{a}/documents").json()["count"]
          == len(CORPUS) - 1, "re-ingest is update, not duplicate")

    cc = _name(rng, "chaos-c")
    c.post(f"{API}/collections/{TENANT}/{cc}")
    payload = "EXACT-BYTES line1\nline2 unicode cafe 日本語\ttab\n"
    _ingest(c, cc, "rt", payload)
    rid = c.get(f"{API}/collections/{TENANT}/{cc}/documents/rt").json()["chunk_ids"][0]
    got = c.get(f"{API}/collections/{TENANT}/{cc}/chunks/{rid}/content")
    check(
        got.status_code == 200 and got.text == payload,
        "chunk content round-trips byte-exact",
    )

    for x in (a, b, cc):
        c.delete(f"{API}/collections/{TENANT}/{x}")


# ---------------------------------------------------------------------------
# Phase 2 - fuzz: weird / empty values and large volumes
# ---------------------------------------------------------------------------
def phase_fuzz(c: httpx.Client, rng, viol: list[str]) -> None:
    """Throw malformed / empty / oversized inputs at the API. Each case must
    return < 500 (a clean 2xx accept or 4xx reject) and the server must stay
    live and uncorrupted - a 5xx, a client-side blow-up, or a broken post-fuzz
    sanity check is a finding."""
    print("\n[phase 2] fuzz - weird/empty values + large volumes")
    fz = _name(rng, "chaos-fuzz")
    c.post(f"{API}/collections/{TENANT}/{fz}")
    base = f"{API}/collections/{TENANT}/{fz}"
    n_ok = 0

    def case(desc: str, fn) -> None:
        nonlocal n_ok
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001 - a client-side blow-up is a finding
            viol.append(f"[fuzz] {desc}: client exception {type(e).__name__}: {e}")
            print(f"FAIL  {desc} -> client {type(e).__name__}")
            return
        if r.status_code >= 500:
            viol.append(f"[fuzz] {desc} -> HTTP {r.status_code}: {r.text[:120]}")
            print(f"FAIL  {desc} -> {r.status_code}")
        else:
            n_ok += 1
            print(f"  ok   {desc} -> {r.status_code}")

    weird = (
        "héllo \t\r\n ünïcødé 日本語 😀 \x01\x02\x1f null=\x00 end"
    ).encode("utf-8")
    long_id = "x" * 4096
    ctrl_id = "id\twith\nctrl\x01chars"

    # empty / whitespace / weird document bodies
    case("empty file", lambda: _ingest_raw(c, fz, b"", docid="empty"))
    case("whitespace only", lambda: _ingest_raw(c, fz, b"   \n\t  ", docid="ws"))
    case(
        "control + null bytes + unicode",
        lambda: _ingest_raw(c, fz, weird, docid="weird"),
    )
    case(
        "random non-utf8 bytes",
        lambda: _ingest_raw(c, fz, rng.randbytes(2048), docid="binary"),
    )
    case(
        "empty docid",
        lambda: _ingest_raw(c, fz, b"server defaults the docid", docid=""),
    )
    case("4KB docid", lambda: _ingest_raw(c, fz, b"long id", docid=long_id))
    case("control-char docid", lambda: _ingest_raw(c, fz, b"ctrl id", docid=ctrl_id))

    # weird metadata (JSON form field)
    case("empty metadata", lambda: _ingest_raw(c, fz, b"m1", docid="m1", metadata="{}"))
    case(
        "malformed metadata json",
        lambda: _ingest_raw(c, fz, b"m2", docid="m2", metadata="{not json"),
    )
    case(
        "metadata non-object",
        lambda: _ingest_raw(c, fz, b"m3", docid="m3", metadata="[1,2,3]"),
    )
    case(
        "weird metadata keys/values",
        lambda: _ingest_raw(
            c,
            fz,
            b"m4",
            docid="m4",
            metadata=(
                '{"": "", "k.with.dots": 1, "spaces here": null, '
                '"ünïcødé": [1,2]}'
            ),
        ),
    )
    case("huge metadata value", lambda: _ingest_raw(c, fz, b"m5", docid="m5",
         metadata='{"big": "' + "z" * 100000 + '"}'))

    # weird search
    case("empty query", lambda: c.post(f"{base}/search", json={"q": "", "k": 5}))
    case(
        "whitespace query",
        lambda: c.post(f"{base}/search", json={"q": "   ", "k": 5}),
    )
    case("huge k", lambda: c.post(f"{base}/search", json={"q": "test", "k": 1_000_000}))
    case("k=0", lambda: c.post(f"{base}/search", json={"q": "test", "k": 0}))
    case("negative k", lambda: c.post(f"{base}/search", json={"q": "test", "k": -5}))
    case("missing q", lambda: c.post(f"{base}/search", json={"k": 5}))
    case("malformed search body", lambda: c.post(f"{base}/search", content=b"{not json",
         headers={"content-type": "application/json"}))

    # ops on a nonexistent collection (must be 404, never 5xx)
    gbase = f"{API}/collections/{TENANT}/{_name(rng, 'chaos-ghost')}"
    case(
        "search nonexistent coll",
        lambda: c.post(f"{gbase}/search", json={"q": "x", "k": 3}),
    )
    case("get doc nonexistent coll", lambda: c.get(f"{gbase}/documents/nope"))
    case("delete nonexistent coll", lambda: c.delete(gbase))

    # weird batch
    case(
        "empty batch list",
        lambda: c.post(f"{base}/documents:batch", json={"documents": []}),
    )
    case("batch with empty texts", lambda: c.post(f"{base}/documents:batch",
         json={"documents": [
             {"text": "", "docid": "be1"},
             {"text": "  ", "docid": "be2"},
         ]}))
    case("batch malformed item", lambda: c.post(f"{base}/documents:batch",
         json={"documents": [{"nope": 1}]}))

    # large volumes
    case("large document (~340KB, many chunks)", lambda: _ingest_raw(c, fz,
         ("Vector databases store embeddings for similarity search. " * 6000).encode(),
         docid="bigdoc"))
    case("large batch (150 docs)", lambda: c.post(f"{base}/documents:batch",
         json={"documents": [
             {
                 "text": f"doc number {i} about topic {i % 7}",
                 "docid": f"bb{i}",
             }
             for i in range(150)
         ]}))

    # post-fuzz sanity: server alive + a clean collection still works correctly
    live = c.get("/health/live")
    if live.status_code != 200:
        viol.append(f"[fuzz] server not live after fuzz -> {live.status_code}")
    sane = _name(rng, "chaos-sane")
    c.post(f"{API}/collections/{TENANT}/{sane}")
    _ingest(c, sane, "sentinel", "the quick brown fox jumps over the lazy dog")
    if "sentinel" not in _search(c, sane, "quick brown fox", 3)[1]:
        viol.append(
            "[fuzz] post-fuzz sanity: sentinel not retrievable "
            "(state corrupted/wedged)"
        )
    else:
        print("  ok   post-fuzz sanity: server live + clean ingest/search correct")
    print(f"  {n_ok} fuzz cases handled without a 5xx")
    for x in (fz, sane):
        c.delete(f"{API}/collections/{TENANT}/{x}")


# ---------------------------------------------------------------------------
# Phase 3 - concurrency chaos
# ---------------------------------------------------------------------------
def phase3_chaos(url, api_key, rng, duration, concurrency, viol: list[str]) -> None:
    print(f"\n[phase 3] concurrency chaos ({duration}s, {concurrency} churn threads)")
    stable = CORPUS[:12]
    churn = CORPUS[12:]
    setup = _client(url, api_key)
    main = _name(rng, "chaos-main")
    setup.post(f"{API}/collections/{TENANT}/{main}")
    for kw, text in CORPUS:
        r = _ingest(setup, main, f"d-{kw}", text)
        if r.status_code != 201:
            viol.append(f"[chaos] setup ingest d-{kw} -> {r.status_code}")

    stop = threading.Event()
    lock = threading.Lock()
    churn_errs: list[str] = []

    def record(lst, msg):
        with lock:
            lst.append(msg)

    def check_resp(label: str, resp: httpx.Response) -> None:
        if resp.status_code >= 500 and not _is_ingest_overload(resp):
            record(
                viol,
                f"[chaos] {label} -> HTTP {resp.status_code}: {resp.text[:120]}",
            )

    def churn_worker(idx: int) -> None:
        wr = random.Random(rng.randint(0, 2**31) + idx)
        c = _client(url, api_key)
        while not stop.is_set():
            try:
                a = wr.random()
                if a < 0.45:
                    # delete + re-ingest a churn doc on main
                    kw, text = wr.choice(churn)
                    did = f"d-{kw}"
                    r = c.delete(f"{API}/collections/{TENANT}/{main}/documents/{did}")
                    check_resp("delete churn doc", r)
                    check_resp("re-ingest churn doc", _ingest(c, main, did, text))
                elif a < 0.8:                      # sibling create/ingest/delete
                    s = _name(wr, "chaos-sib")
                    kw = wr.choice(SIBLING_KEYWORDS)
                    check_resp(
                        "create sibling",
                        c.post(f"{API}/collections/{TENANT}/{s}"),
                    )
                    check_resp(
                        "ingest sibling",
                        _ingest(
                            c,
                            s,
                            f"sib-{kw}",
                            f"the {kw} lives only in sibling collections",
                        ),
                    )
                    check_resp(
                        "delete sibling",
                        c.delete(f"{API}/collections/{TENANT}/{s}"),
                    )
                else:                              # sibling move
                    s = _name(wr, "chaos-mv")
                    dst = _name(wr, "chaos-mv2")
                    check_resp(
                        "create move sibling",
                        c.post(f"{API}/collections/{TENANT}/{s}"),
                    )
                    check_resp(
                        "ingest move sibling",
                        _ingest(c, s, "x", "movable sibling content"),
                    )
                    moved = _move_collection(c, s, dst)
                    check_resp("move sibling", moved)
                    cleanup = dst if moved.status_code < 300 else s
                    check_resp(
                        "delete moved sibling",
                        c.delete(f"{API}/collections/{TENANT}/{cleanup}"),
                    )
            except Exception as e:                 # noqa: BLE001
                record(churn_errs, f"{type(e).__name__}: {e}")
        c.close()

    def checker() -> int:
        c = _client(url, api_key)
        end = time.time() + duration
        rounds = 0
        while time.time() < end:
            rounds += 1
            for kw, _ in stable:
                # stable present + correct + no bleed
                st, ids = _search(c, main, kw, 8)
                if st != 200:
                    record(viol, f"[chaos] main search '{kw}' -> HTTP {st}")
                    continue
                if f"d-{kw}" not in ids:
                    record(viol, f"[chaos] stable doc d-{kw} vanished (got {ids[:3]})")
                if any(i.startswith("sib-") for i in ids):
                    bleed = [i for i in ids if i.startswith("sib-")]
                    record(viol, f"[chaos] BLEED on '{kw}': {bleed}")
            for kw in SIBLING_KEYWORDS:
                # sibling keyword must never surface a sibling doc
                st, ids = _search(c, main, kw, 8)
                if st >= 500:
                    record(viol, f"[chaos] main search sib '{kw}' -> HTTP {st}")
                if any(i.startswith("sib-") for i in ids):
                    bleed = [i for i in ids if i.startswith("sib-")]
                    record(viol, f"[chaos] BLEED sib-kw '{kw}': {bleed}")
        c.close()
        return rounds

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency + 1) as ex:
        futs = [ex.submit(churn_worker, i) for i in range(concurrency)]
        try:
            rounds = checker()
        finally:
            stop.set()
        for f in futs:
            f.result()

    time.sleep(1)  # let in-flight churn settle
    docs = setup.get(f"{API}/collections/{TENANT}/{main}/documents").json()
    present = {d["docid"] for d in docs["documents"]}
    for kw, _ in stable:
        if f"d-{kw}" not in present:
            record(viol, f"[chaos] FINAL: stable doc d-{kw} missing from list")
        if f"d-{kw}" not in _search(setup, main, kw, 8)[1]:
            record(
                viol,
                f"[chaos] FINAL: stable doc d-{kw} not retrievable after churn",
            )
    setup.delete(f"{API}/collections/{TENANT}/{main}")
    setup.close()
    print(f"  ran {time.time() - t0:.0f}s, {rounds} checker rounds, "
          f"{len(churn_errs)} churn exceptions")
    if churn_errs:
        print("  sample churn exceptions:", churn_errs[:3])
        for err in churn_errs[:20]:
            record(viol, f"[chaos] churn exception: {err}")
        if len(churn_errs) > 20:
            record(viol, f"[chaos] {len(churn_errs) - 20} more churn exceptions")


def main() -> None:
    p = argparse.ArgumentParser(description="PaveDB chaos + correctness suite")
    p.add_argument("--url", default="http://localhost:8086", help="PaveDB base URL")
    p.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="RNG seed (reproducible names + ops)",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=120,
        help="Phase-3 chaos duration (s)",
    )
    p.add_argument("--concurrency", type=int, default=24, help="Phase-3 churn threads")
    p.add_argument(
        "--api-key",
        default=None,
        help="Bearer token (omit when auth.mode=none)",
    )
    p.add_argument(
        "--data-dir",
        default=None,
        help="Optional local data dir for catalog<->disk post-run invariants",
    )
    args = p.parse_args()

    rng = random.Random(args.seed)
    print(f"==> PaveDB chaos suite")
    print(f"    server : {args.url}")
    print(f"    runner : {local_hw()}")
    print(
        f"    seed={args.seed}  duration={args.duration}s  "
        f"concurrency={args.concurrency}"
    )

    viol: list[str] = []
    c = _client(args.url, args.api_key)
    try:
        phase1_correctness(c, rng, viol)
        phase_fuzz(c, rng, viol)
    finally:
        c.close()
    phase3_chaos(args.url, args.api_key, rng, args.duration, args.concurrency, viol)
    if args.data_dir:
        print("\n[post-run] catalog<->disk invariants")
        for msg in catalog_disk_violations(args.data_dir):
            viol.append(f"[catalog-disk] {msg}")
        if not any(v.startswith("[catalog-disk]") for v in viol):
            print("  ok   catalog rows match collection dirs")

    print("\n" + "=" * 60)
    if viol:
        print(f"FAIL - {len(viol)} invariant violation(s):")
        for v in viol:
            print("  - " + v)
        sys.exit(1)
    print("PASS - no correctness or chaos invariant violated")


if __name__ == "__main__":
    main()
