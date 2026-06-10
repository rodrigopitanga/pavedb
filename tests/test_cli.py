# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import importlib
import zipfile
from pathlib import Path

import pytest
from pave.config import get_cfg
from pave.stores.local import LocalStore
from utils import DummyStore, FakeEmbedder, SpyStore

@pytest.fixture
def cli_env(temp_data_dir, tmp_path, monkeypatch):
    store = SpyStore(DummyStore())
    from pave import cli as pvcli_mod

    pvcli = importlib.reload(pvcli_mod)
    monkeypatch.setattr(
        pvcli,
        "get_embedder",
        lambda: FakeEmbedder(),
        raising=True,
    )
    pvcli.store = store
    return pvcli, store, tmp_path


@pytest.fixture
def cli_query_env(temp_data_dir, tmp_path, monkeypatch):
    from pave import cli as pvcli_mod

    pvcli = importlib.reload(pvcli_mod)
    monkeypatch.setattr(
        pvcli,
        "get_embedder",
        lambda: FakeEmbedder(),
        raising=True,
    )
    store = SpyStore(LocalStore(str(temp_data_dir), FakeEmbedder()))
    pvcli.store = store
    return pvcli, store, tmp_path

def test_cli_ingest_on_fresh_collection_with_empty_index_dir(cli_env, tmp_path):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "invoices"
    sample = tmp_path / "s.txt"
    sample.write_text("one two three quatro cinco", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC1", "--metadata", '{"lang":"pt"}'])

    assert ("create_collection", tenant, coll) in store.calls
    assert ("purge_doc", tenant, coll, "DOC1") not in store.calls
    assert any(c[0] == "index_records" and c[1] == tenant and c[2] == coll \
               and c[3] == "DOC1" for c in store.calls)


def test_cli_create_collection_accepts_embedder_args(cli_env, capsys):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "cfgcli"

    rc = pvcli.main_cli(
        [
            "create-collection",
            tenant,
            coll,
            "--embedder-type",
            "sbert",
            "--embed-model",
            "fake",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["ok"] is True
    assert out["embedder_type"] == "sbert"
    assert out["embed_model"] == "fake"
    assert ("create_collection", tenant, coll) in store.calls


def test_cli_create_collection_rejects_wrong_embed_model(cli_env, capsys):
    pvcli, _, _ = cli_env

    rc = pvcli.main_cli(
        [
            "create-collection",
            "acme",
            "badcfgcli",
            "--embed-model",
            "sentence-transformers/all-MiniLM-L6-v2",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["ok"] is False
    assert out["code"] == "embed_model_not_supported"


def test_cli_ingest_passes_doc_meta_through_wrapper(cli_env, tmp_path):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "metawrap"
    sample = tmp_path / "meta.txt"
    sample.write_text("conteúdo de teste", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(
        [
            "ingest", tenant, coll, str(sample),
            "--docid", "DOCMETA",
            "--metadata", '{"lang":"pt","source":"cli"}',
        ]
    )

    calls = [
        c for c in store.calls
        if c[0] == "index_records" and c[1] == tenant
        and c[2] == coll and c[3] == "DOCMETA"
    ]
    assert calls
    doc_meta = calls[-1][5]
    assert isinstance(doc_meta, dict)
    assert doc_meta["docid"] == "DOCMETA"
    assert doc_meta["lang"] == "pt"
    assert doc_meta["source"] == "cli"
    assert doc_meta["filename"].endswith("meta.txt")
    assert doc_meta["ingested_at"].endswith("Z")


def test_cli_reingest_same_docid_triggers_purge(cli_env, tmp_path):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "reupcli"
    sample = tmp_path / "reup.txt"
    sample.write_text("alpha bravo", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC-REUP"])

    sample.write_text("delta echo", encoding="utf-8")
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC-REUP"])

    # Purge now happens atomically inside index_records, so the service no
    # longer issues a separate purge_doc call. Verify the two-ingest path
    # actually went through index_records both times.
    index_calls = [
        c for c in store.calls
        if c[0] == "index_records" and c[1] == tenant
        and c[2] == coll and c[3] == "DOC-REUP"
    ]
    assert len(index_calls) == 2
    assert ("purge_doc", tenant, coll, "DOC-REUP") not in store.calls

def test_cli_search_returns_matches(cli_env, tmp_path):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "invoices"
    sample = tmp_path / "s2.txt"
    sample.write_text(
        "O avião sobrevoa o oceano. Mapas e correntes.",
        encoding="utf-8"
    )

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC2"])
    pvcli.main_cli(["search", tenant, coll, "avião", "-k", "5"])

    assert any(c[0] == "search" and c[1] == tenant and c[2] == coll \
               and c[3] == "avião" and c[4] == 5 for c in store.calls)


def test_cli_search_does_not_merge_common_by_default(
    cli_env,
    tmp_path,
    capsys,
):
    pvcli, store, _ = cli_env
    cfg = get_cfg()
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "common")
    local = tmp_path / "local.txt"
    common = tmp_path / "common.txt"
    local.write_text("captain local", encoding="utf-8")
    common.write_text("captain common", encoding="utf-8")

    pvcli.main_cli(["create-collection", "acme", "local"])
    pvcli.main_cli(["create-collection", "global", "common"])
    pvcli.main_cli(["ingest", "acme", "local", str(local), "--docid", "DOC-L"])
    pvcli.main_cli(
        ["ingest", "global", "common", str(common), "--docid", "DOC-C"]
    )
    capsys.readouterr()

    pvcli.main_cli(
        [
            "search",
            "acme",
            "local",
            "captain",
            "-k",
            "2",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert len(out["matches"]) == 1
    assert not any(
        call[:5] == ("search", "global", "common", "captain", 10)
        for call in store.calls
    )
    log_calls = [call for call in store.calls if call[0] == "log_query"]
    assert log_calls
    payload = log_calls[-1][1]
    assert payload["include_common"] is False


def test_cli_search_merges_common_when_enabled(
    cli_env,
    tmp_path,
    capsys,
):
    pvcli, store, _ = cli_env
    cfg = get_cfg()
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "common")
    local = tmp_path / "local-merge.txt"
    common = tmp_path / "common-merge.txt"
    local.write_text("captain local", encoding="utf-8")
    common.write_text("captain common", encoding="utf-8")

    pvcli.main_cli(["create-collection", "acme", "localmerge"])
    pvcli.main_cli(["create-collection", "global", "common"])
    pvcli.main_cli(
        ["ingest", "acme", "localmerge", str(local), "--docid", "DOC-L"]
    )
    pvcli.main_cli(
        ["ingest", "global", "common", str(common), "--docid", "DOC-C"]
    )
    capsys.readouterr()

    pvcli.main_cli(
        [
            "search",
            "acme",
            "localmerge",
            "captain",
            "-k",
            "2",
            "--merge-common=true",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert len(out["matches"]) == 2
    assert any(
        call[:5] == ("search", "global", "common", "captain", 10)
        for call in store.calls
    )
    log_calls = [call for call in store.calls if call[0] == "log_query"]
    payload = log_calls[-1][1]
    assert payload["include_common"] is True
    assert payload["common_tenant"] == "global"
    assert payload["common_collection"] == "common"

def test_cli_list_queries_returns_logged_searches(
    cli_query_env,
    capsys,
):
    pvcli, store, tmp_path = cli_query_env
    tenant, coll = "acme", "qlogcli"
    sample = tmp_path / "qlog.txt"
    sample.write_text("hello world from cli", encoding="utf-8")
    other = tmp_path / "qlog-other.txt"
    other.write_text("other tenant query", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC1"])
    pvcli.main_cli(["search", tenant, coll, "hello", "-k", "1"])
    pvcli.main_cli(["create-collection", "beta", "other"])
    pvcli.main_cli(["ingest", "beta", "other", str(other), "--docid", "DOC2"])
    pvcli.main_cli(["search", "beta", "other", "other", "-k", "1"])
    capsys.readouterr()

    rc = pvcli.main_cli(
        [
            "list-queries",
            "--tenant",
            tenant,
            "--collection",
            coll,
            "--limit",
            "10",
            "--offset",
            "0",
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["tenant"] == tenant
    assert out["collection"] == coll
    assert out["count"] == 1
    assert out["queries"][0]["query_id"]
    assert out["queries"][0]["tenant"] == tenant
    assert out["queries"][0]["collection"] == coll
    assert out["queries"][0]["created_at"].endswith("Z")
    assert ("list_query_homes", tenant, coll, 10, 0) in store.calls

    rc = pvcli.main_cli(["list-queries", "--limit", "10", "--offset", "0"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["tenant"] is None
    assert out["collection"] is None
    assert out["count"] >= 2
    rows = {
        (row["tenant"], row["collection"])
        for row in out["queries"]
    }
    assert {("acme", "qlogcli"), ("beta", "other")}.issubset(rows)


def test_cli_get_query_returns_full_entry(cli_query_env, capsys):
    pvcli, store, tmp_path = cli_query_env
    tenant, coll = "acme", "qdetailcli"
    sample = tmp_path / "qdetail.txt"
    sample.write_text("captain nemo from cli", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC1"])
    pvcli.main_cli(["search", tenant, coll, "captain", "-k", "1"])
    capsys.readouterr()

    query_id = store.impl.list_query_logs(tenant, coll)[0]["query_id"]
    rc = pvcli.main_cli(["get-query", query_id])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["query"]["query_id"] == query_id
    assert out["query"]["tenant"] == tenant
    assert out["query"]["collection"] == coll
    assert out["query"]["actor"] == "admin"
    assert out["query"]["query_text"] == "captain"
    assert out["query"]["result_ids"]
    assert ("resolve_query_home", query_id) in store.calls
    assert ("get_query_log_entry", tenant, coll, query_id) in store.calls


def test_cli_get_query_missing_returns_exit_code_1(cli_query_env, capsys):
    pvcli, _, _ = cli_query_env

    rc = pvcli.main_cli(["get-query", "missing-query-id"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["ok"] is False
    assert out["code"] == "query_not_found"


def test_cli_replay_query_returns_fresh_results_and_logs_replay(
    cli_query_env,
    capsys,
):
    pvcli, store, tmp_path = cli_query_env
    tenant, coll = "acme", "qreplaycli"
    first = tmp_path / "qreplay-1.txt"
    second = tmp_path / "qreplay-2.txt"
    first.write_text("hello world from cli", encoding="utf-8")
    second.write_text("hello again from cli", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(first), "--docid", "DOC1"])
    pvcli.main_cli(["search", tenant, coll, "hello", "-k", "5"])
    capsys.readouterr()

    original_query_id = store.impl.list_query_logs(tenant, coll)[0]["query_id"]
    pvcli.main_cli(["ingest", tenant, coll, str(second), "--docid", "DOC2"])
    capsys.readouterr()
    store.calls.clear()

    rc = pvcli.main_cli(["replay-query", original_query_id])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["ok"] is True
    assert out["original_query_id"] == original_query_id
    assert out["replay_query_id"] != original_query_id
    assert {match["meta"]["docid"] for match in out["matches"]} == {
        "DOC1",
        "DOC2",
    }
    assert ("resolve_query_home", original_query_id) in store.calls
    assert ("get_query_log_entry", tenant, coll, original_query_id) in store.calls
    log_calls = [call for call in store.calls if call[0] == "log_query"]
    assert len(log_calls) == 1
    assert log_calls[0][1]["actor"] == "admin"
    assert log_calls[0][1]["replay_of"] == original_query_id

    logs = store.impl.list_query_logs(tenant, coll)
    assert len(logs) == 2
    replay_entry = next(
        row for row in logs if row["query_id"] == out["replay_query_id"]
    )
    assert replay_entry["replay_of"] == original_query_id


def test_cli_replay_query_missing_returns_exit_code_1(
    cli_query_env,
    capsys,
):
    pvcli, _, _ = cli_query_env

    rc = pvcli.main_cli(["replay-query", "missing-query-id"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["ok"] is False
    assert out["code"] == "query_not_found"


def test_cli_list_queries_collection_filter_without_tenant_works(
    cli_query_env,
    capsys,
):
    pvcli, _, tmp_path = cli_query_env
    collection = "sharedfiltercli"
    first = tmp_path / "filter-first.txt"
    second = tmp_path / "filter-second.txt"
    first.write_text("same collection one", encoding="utf-8")
    second.write_text("same collection two", encoding="utf-8")

    pvcli.main_cli(["create-collection", "acme", collection])
    pvcli.main_cli(["create-collection", "beta", collection])
    pvcli.main_cli(["ingest", "acme", collection, str(first), "--docid", "DOC1"])
    pvcli.main_cli(["ingest", "beta", collection, str(second), "--docid", "DOC2"])
    pvcli.main_cli(["search", "acme", collection, "same", "-k", "1"])
    pvcli.main_cli(["search", "beta", collection, "same", "-k", "1"])
    capsys.readouterr()

    rc = pvcli.main_cli(["list-queries", "--collection", collection])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["tenant"] is None
    assert out["collection"] == collection
    assert out["count"] == 2
    assert {row["tenant"] for row in out["queries"]} == {"acme", "beta"}


def test_cli_list_documents_returns_doc_summaries(cli_env, tmp_path, capsys):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "clidocs"
    sample = tmp_path / "clidocs.txt"
    sample.write_text("alpha bravo", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC1"])
    capsys.readouterr()

    pvcli.main_cli(["list-documents", tenant, coll])
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert out["tenant"] == tenant
    assert out["collection"] == coll
    assert out["count"] == 1
    assert out["documents"][0]["docid"] == "DOC1"
    assert out["documents"][0]["version"] == 1
    assert out["documents"][0]["chunk_count"] == 1
    assert ("list_documents", tenant, coll) in store.calls


def test_cli_get_document_returns_full_document(cli_env, tmp_path, capsys):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "cligetdoc"
    sample = tmp_path / "cligetdoc.txt"
    sample.write_text("hello document", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(
        [
            "ingest",
            tenant,
            coll,
            str(sample),
            "--docid",
            "DOC-GET-1",
            "--metadata",
            '{"lang":"pt","source":"cli"}',
        ]
    )
    capsys.readouterr()

    pvcli.main_cli(["get-document", tenant, coll, "DOC-GET-1"])
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert out["tenant"] == tenant
    assert out["collection"] == coll
    assert out["docid"] == "DOC-GET-1"
    assert out["chunk_ids"] == ["DOC-GET-1::chunk_0"]
    assert out["chunk_count"] == 1
    assert out["metadata"]["docid"] == "DOC-GET-1"
    assert ("get_document", tenant, coll, "DOC-GET-1") in store.calls


def test_cli_list_chunks_returns_chunk_summaries(
    cli_query_env,
    tmp_path,
    capsys,
):
    pvcli, store, _ = cli_query_env
    tenant, coll = "acme", "clichunks"
    sample = tmp_path / "clichunks.txt"
    sample.write_text("hello chunk cli", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC1"])
    capsys.readouterr()

    rc = pvcli.main_cli(["list-chunks", tenant, coll, "DOC1"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["ok"] is True
    assert out["tenant"] == tenant
    assert out["collection"] == coll
    assert out["docid"] == "DOC1"
    assert out["count"] == 1
    assert out["chunks"][0]["rid"] == "DOC1::chunk_0"
    assert out["chunks"][0]["ingested_at"].endswith("Z")
    assert ("list_chunks", tenant, coll, "DOC1") in store.calls


def test_cli_get_chunk_returns_metadata_without_text(
    cli_query_env,
    tmp_path,
    capsys,
):
    pvcli, store, _ = cli_query_env
    tenant, coll = "acme", "cligetchunk"
    sample = tmp_path / "cligetchunk.txt"
    sample.write_text("hello chunk metadata", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC1"])
    capsys.readouterr()

    rc = pvcli.main_cli(["get-chunk", tenant, coll, "DOC1::chunk_0"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["ok"] is True
    assert out["tenant"] == tenant
    assert out["collection"] == coll
    assert out["docid"] == "DOC1"
    assert out["rid"] == "DOC1::chunk_0"
    assert "text" not in out
    assert ("get_chunk", tenant, coll, "DOC1::chunk_0") in store.calls


def test_cli_get_chunk_content_writes_raw_bytes(
    cli_query_env,
    tmp_path,
    capsysbinary,
):
    pvcli, store, _ = cli_query_env
    tenant, coll = "acme", "cligetchunkcontent"
    sample = tmp_path / "cligetchunkcontent.txt"
    sample.write_text("hello chunk content", encoding="utf-8")

    pvcli.main_cli(["create-collection", tenant, coll])
    pvcli.main_cli(["ingest", tenant, coll, str(sample), "--docid", "DOC1"])
    capsysbinary.readouterr()

    rc = pvcli.main_cli(
        ["get-chunk-content", tenant, coll, "DOC1::chunk_0"]
    )
    out = capsysbinary.readouterr().out

    assert rc == 0
    assert out == b"hello chunk content"
    assert ("get_chunk_content", tenant, coll, "DOC1::chunk_0") in store.calls


def test_cli_get_chunk_content_missing_returns_exit_code_1(
    cli_query_env,
    capsys,
):
    pvcli, _, _ = cli_query_env

    rc = pvcli.main_cli(["get-chunk-content", "acme", "docs", "missing"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["ok"] is False
    assert out["code"] == "chunk_content_not_found"


def test_cli_dump_archive_creates_zip(cli_env, tmp_path, capsys):
    pvcli, _, _ = cli_env
    data_dir = Path(get_cfg().get("data_dir"))
    sample = data_dir / "sample.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("hello", encoding="utf-8")

    target = tmp_path / "export.zip"
    pvcli.main_cli(["dump-archive", "--output", str(target)])

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert Path(out["archive"]) == target

    with zipfile.ZipFile(target) as zf:
        assert "sample.txt" in zf.namelist()
        with zf.open("sample.txt") as f:
            assert f.read().decode("utf-8") == "hello"


def test_cli_list_tenants(cli_env, tmp_path, capsys, monkeypatch):
    pvcli, _, _ = cli_env
    from pave.config import get_cfg
    cfg = get_cfg()
    monkeypatch.setattr(cfg, "_cfg", {**cfg._cfg, "data_dir": str(tmp_path)})

    beta = tmp_path / "t_beta" / "c_docs" / "meta.db"
    alpha = tmp_path / "t_alpha" / "c_docs" / "meta.db"
    beta.parent.mkdir(parents=True, exist_ok=True)
    alpha.parent.mkdir(parents=True, exist_ok=True)
    beta.touch()
    alpha.touch()

    pvcli.main_cli(["list-tenants"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["tenants"] == ["alpha", "beta"]
    assert out["count"] == 2


def test_cli_list_tenants_accepts_home_flag(cli_env, tmp_path, capsys):
    pvcli, _, _ = cli_env
    home = tmp_path / "instance"
    beta = home / "data" / "t_beta" / "c_docs" / "meta.db"
    alpha = home / "data" / "t_alpha" / "c_docs" / "meta.db"
    beta.parent.mkdir(parents=True, exist_ok=True)
    alpha.parent.mkdir(parents=True, exist_ok=True)
    beta.touch()
    alpha.touch()

    pvcli.main_cli(["list-tenants", "--home", str(home)])
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert out["tenants"] == ["alpha", "beta"]


def test_cli_list_tenants_does_not_load_embedder(tmp_path, capsys, monkeypatch):
    from pave import cli as pvcli_mod

    pvcli = importlib.reload(pvcli_mod)
    pvcli.store = None

    def fail_get_embedder():
        raise AssertionError("list-tenants loaded an embedder")

    monkeypatch.setattr(pvcli, "get_embedder", fail_get_embedder, raising=True)

    beta = tmp_path / "t_beta" / "c_docs" / "meta.db"
    alpha = tmp_path / "t_alpha" / "c_docs" / "meta.db"
    beta.parent.mkdir(parents=True, exist_ok=True)
    alpha.parent.mkdir(parents=True, exist_ok=True)
    beta.touch()
    alpha.touch()

    rc = pvcli.main_cli([
        "--compact",
        "list-tenants",
        "--data-dir",
        str(tmp_path),
    ])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["ok"] is True
    assert out["tenants"] == ["alpha", "beta"]


def test_cli_get_collection_returns_detail(cli_env, capsys):
    pvcli, store, _ = cli_env
    tenant, coll = "acme", "detailcli"

    pvcli.main_cli(["create-collection", tenant, coll])
    capsys.readouterr()

    pvcli.main_cli(["get-collection", tenant, coll])
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert out["tenant"] == tenant
    assert out["name"] == coll
    assert out["embedder_type"] == "sbert"
    assert out["embed_model"] == "fake"
    assert out["doc_count"] == 0
    assert out["chunk_count"] == 0
    assert ("get_collection_detail", tenant, coll) in store.calls


def test_cli_init_writes_default_instance_files(cli_env, monkeypatch, tmp_path, capsys):
    pvcli, _, _ = cli_env
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    pvcli.main_cli(["init"])
    out = json.loads(capsys.readouterr().out)
    instance = home / "pavedb"
    config_path = instance / "config.yml"
    tenants_path = instance / "tenants.yml"

    assert out["ok"] is True
    assert Path(out["config"]) == config_path
    assert Path(out["tenants"]) == tenants_path
    assert Path(out["data_dir"]) == instance / "data"
    assert config_path.is_file()
    assert tenants_path.is_file()
    assert "tenants_file:" in config_path.read_text(encoding="utf-8")
    assert "data_dir:" in config_path.read_text(encoding="utf-8")
