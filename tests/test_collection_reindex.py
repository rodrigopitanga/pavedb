# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

import pave.stores.local as local_store_mod
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _wait_for_job(get_job, *statuses: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = get_job()
        if job["status"] in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(f"reindex job did not reach {statuses}: {get_job()}")


def _store_with_paused_job(
    temp_data_dir,
    monkeypatch,
    collection: str,
) -> tuple[LocalStore, str]:
    """Build a store whose collection carries one paused reindex job."""
    entered = threading.Event()
    release = threading.Event()

    class BlockingEmbedder(FakeEmbedder):
        def encode(self, texts):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test embedder was not released")
            return super().encode(texts)

    target = BlockingEmbedder()

    def factory(_etype, model, _config):
        return target if model == "fake-v2" else FakeEmbedder()

    monkeypatch.setattr(local_store_mod, "_REINDEX_BATCH_SIZE", 1)
    store = LocalStore(
        str(temp_data_dir),
        FakeEmbedder(),
        embedder_factory=factory,
    )
    store.create_collection("acme", collection)
    store.index_records(
        "acme",
        collection,
        "doc",
        [
            ("0", "first passage", {}),
            ("1", "second passage", {}),
        ],
    )
    job_id = store.start_reindex(
        "acme",
        collection,
        embed_model="fake-v2",
    )["job_id"]
    assert entered.wait(timeout=2)
    store.pause_reindex(job_id)
    # Let the blocked batch finish so the job settles at a persisted
    # boundary; later encodes in the test run unblocked.
    release.set()
    _wait_for_job(lambda: store.get_reindex_job(job_id), "paused")
    return store, job_id


def test_admin_reindex_rebuilds_vectors_and_updates_collection(client):
    assert client.post("/v1/collections/acme/books").status_code == 201
    ingested = client.post(
        "/v1/collections/acme/books/documents",
        files={"file": ("book.txt", b"Captain Nemo aboard Nautilus", "text/plain")},
        data={"docid": "verne"},
    )
    assert ingested.status_code == 201

    started = client.post(
        "/v1/admin/collections/acme/books/reindex",
        json={"embed_model": "fake-v2"},
    )

    assert started.status_code == 202
    job_id = started.json()["job_id"]
    job = _wait_for_job(
        lambda: client.get(f"/v1/admin/reindex/{job_id}").json(),
        "succeeded",
        "failed",
    )
    assert job["status"] == "succeeded", job
    assert job["completed_chunks"] == job["total_chunks"] > 0
    assert job["target"]["embed_model"] == "fake-v2"

    detail = client.get("/v1/collections/acme/books/detail")
    assert detail.status_code == 200
    assert detail.json()["embed_model"] == "fake-v2"
    search = client.post(
        "/v1/collections/acme/books/search",
        json={"q": "Nautilus", "k": 1},
    )
    assert search.status_code == 200
    assert search.json()["matches"][0]["id"].startswith("verne::")


def test_reindex_requires_admin_key(client, cfg):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "tenant-key"})
    headers = {"Authorization": "Bearer tenant-key"}
    assert client.post(
        "/v1/collections/acme/private",
        headers=headers,
    ).status_code == 201

    response = client.post(
        "/v1/admin/collections/acme/private/reindex",
        json={"embed_model": "fake-v2"},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"


def test_reindex_job_pauses_and_resumes_after_store_restart(
    temp_data_dir,
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingEmbedder(FakeEmbedder):
        def encode(self, texts):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test embedder was not released")
            return super().encode(texts)

    target = BlockingEmbedder()

    def first_factory(_etype, model, _config):
        return target if model == "fake-v2" else FakeEmbedder()

    monkeypatch.setattr(local_store_mod, "_REINDEX_BATCH_SIZE", 1)
    first = LocalStore(
        str(temp_data_dir),
        FakeEmbedder(),
        embedder_factory=first_factory,
    )
    first.create_collection("acme", "resumable")
    first.index_records(
        "acme",
        "resumable",
        "doc",
        [
            ("0", "first passage", {}),
            ("1", "second passage", {}),
        ],
    )

    job_id = first.start_reindex(
        "acme",
        "resumable",
        embed_model="fake-v2",
    )["job_id"]
    assert entered.wait(timeout=2)
    assert first.pause_reindex(job_id)["status"] == "pausing"
    release.set()
    paused = _wait_for_job(
        lambda: first.get_reindex_job(job_id),
        "paused",
    )
    assert paused["completed_chunks"] == 1
    first.close()

    second = LocalStore(
        str(temp_data_dir),
        FakeEmbedder(),
        embedder_factory=lambda _etype, _model, _config: FakeEmbedder(),
    )
    assert second.get_reindex_job(job_id)["status"] == "paused"
    second.resume_reindex(job_id)
    finished = _wait_for_job(
        lambda: second.get_reindex_job(job_id),
        "succeeded",
        "failed",
    )

    assert finished["status"] == "succeeded", finished
    assert finished["completed_chunks"] == 2
    config = second.get_collection_config("acme", "resumable")
    assert config is not None
    assert config["embed_model"] == "fake-v2"
    assert len(second.search("acme", "resumable", "passage", k=2)) == 2


def test_delete_collection_clears_its_reindex_jobs(
    temp_data_dir,
    monkeypatch,
):
    store, job_id = _store_with_paused_job(temp_data_dir, monkeypatch, "doomed")
    job_dir = store._reindex_job_dir(job_id)
    assert job_dir.is_dir()

    store.delete_collection("acme", "doomed")

    with pytest.raises(FileNotFoundError):
        store.get_reindex_job(job_id)
    assert not job_dir.exists()

    # The freed active-job slot is what makes the collection usable again:
    # a leftover unfinished job would reject this with a conflict.
    store.create_collection("acme", "doomed")
    store.index_records(
        "acme",
        "doomed",
        "doc",
        [("0", "first passage", {})],
    )
    revived = store.start_reindex("acme", "doomed", embed_model="fake-v2")
    assert revived["job_id"] != job_id
    finished = _wait_for_job(
        lambda: store.get_reindex_job(revived["job_id"]),
        "succeeded",
        "failed",
    )
    assert finished["status"] == "succeeded", finished


def test_move_collection_carries_its_reindex_job(temp_data_dir, monkeypatch):
    store, job_id = _store_with_paused_job(temp_data_dir, monkeypatch, "before")

    store.move_collection("acme", "before", "after")

    assert store.get_reindex_job(job_id)["collection"] == "after"
    store.resume_reindex(job_id)
    finished = _wait_for_job(
        lambda: store.get_reindex_job(job_id),
        "succeeded",
        "failed",
    )

    assert finished["status"] == "succeeded", finished
    assert finished["completed_chunks"] == 2
    config = store.get_collection_config("acme", "after")
    assert config is not None
    assert config["embed_model"] == "fake-v2"


def test_restore_collection_archive_clears_reindex_jobs(
    temp_data_dir,
    monkeypatch,
):
    store, job_id = _store_with_paused_job(
        temp_data_dir,
        monkeypatch,
        "archived",
    )
    job_dir = store._reindex_job_dir(job_id)
    archive_path, tmp_dir = store.dump_collection_archive("acme", "archived")
    try:
        archive_bytes = Path(archive_path).read_bytes()
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    store.restore_collection_archive("acme", "archived", archive_bytes)

    with pytest.raises(FileNotFoundError):
        store.get_reindex_job(job_id)
    assert not job_dir.exists()
    revived = store.start_reindex("acme", "archived", embed_model="fake-v2")
    finished = _wait_for_job(
        lambda: store.get_reindex_job(revived["job_id"]),
        "succeeded",
        "failed",
    )
    assert finished["status"] == "succeeded", finished
