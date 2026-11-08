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


# Waits sized for a starved CI runner, not an idle host (P3-56). A reindex
# job here is ~1.5s of real work and the suite as a whole normally finishes
# in under four minutes; on a contended runner both stretch by an order of
# magnitude -- job 17679513723 reached 55% of the suite in 40m and two of
# these tests went red on the way. Reproduced under local load: the jobs
# still run to completion, they just need more than 5s. The assertions are
# unchanged; only the patience is.
_JOB_WAIT_S = 60.0
_ENCODE_WAIT_S = 30.0


@pytest.fixture(autouse=True)
def _configured_target(cfg):
    # A reindex target must be a configured instance (B90).
    cfg.set("embedder.instances.v2.type", "sbert")
    cfg.set("embedder.instances.v2.model", "fake-v2")


def _wait_for_job(get_job, *statuses: str) -> dict:
    deadline = time.monotonic() + _JOB_WAIT_S
    while time.monotonic() < deadline:
        job = get_job()
        if job["status"] in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(
        f"reindex job did not reach {statuses} in {_JOB_WAIT_S}s: {get_job()}"
    )


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
            if not release.wait(timeout=_ENCODE_WAIT_S):
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
    assert entered.wait(timeout=_ENCODE_WAIT_S)
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

    refused = client.post(
        "/v1/admin/collections/acme/books/reindex",
        json={"embed_model": "attacker/any-hf-repo"},
    )
    assert refused.status_code == 400, refused.text

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


def test_list_reindex_jobs_survives_restart(
    client, app, temp_data_dir, monkeypatch,
):
    empty = client.get("/v1/admin/reindex")
    assert empty.status_code == 200
    assert empty.json()["jobs"] == []

    store = app.state.store.impl
    monkeypatch.setattr(store, "_launch_reindex_job", lambda job_id: None)
    expected = {}
    for tenant, status in (("acme", "paused"), ("other", "succeeded")):
        store.create_collection(tenant, "books")
        started = client.post(
            f"/v1/admin/collections/{tenant}/books/reindex",
            json={"embed_model": "fake-v2"},
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        store._ensure_catalog().update_reindex_job(job_id, status=status)
        expected[job_id] = (tenant, status)

    store.close()
    app.state.store = LocalStore(str(temp_data_dir), FakeEmbedder())
    response = client.get("/v1/admin/reindex")
    assert response.status_code == 200, response.text
    jobs = response.json()["jobs"]
    assert {job["job_id"]: (job["tenant"], job["status"]) for job in jobs} == expected
    assert [job["created_at"] for job in jobs] == sorted(
        job["created_at"] for job in jobs
    )
    for job in jobs:
        detail = client.get(f"/v1/admin/reindex/{job['job_id']}")
        assert detail.status_code == 200
        assert all(
            detail.json()[key] == value
            for key, value in job.items()
            if key not in {"request_id", "latency_ms"}
        )


@pytest.mark.parametrize("token, status", [(None, 401), ("tenant-key", 403)])
def test_list_reindex_jobs_requires_admin(client, cfg, app, monkeypatch, token, status):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "tenant-key"})

    def forbidden_read():
        pytest.fail("unauthorized listing reached the store")

    monkeypatch.setattr(app.state.store.impl, "list_reindex_jobs", forbidden_read)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.get("/v1/admin/reindex", headers=headers)
    assert response.status_code == status
    assert response.json()["ok"] is False


def test_reindex_job_pauses_and_resumes_after_store_restart(
    temp_data_dir,
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingEmbedder(FakeEmbedder):
        def encode(self, texts):
            entered.set()
            if not release.wait(timeout=_ENCODE_WAIT_S):
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
    assert entered.wait(timeout=_ENCODE_WAIT_S)
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

    store.restore_collection_archive(
        "acme", "archived", archive_bytes, replace=True,
    )

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


_SENSITIVE_ERROR = (
    "Incorrect API key: sk-test-ABCD...WXYZ; Bearer secret-fragment; "
    "proxy https://embed.private.example:8443 cache /srv/private/models/cache"
)
_PUBLIC_ERROR = "reindex operation failed; see the server log for this job"


@pytest.fixture
def redaction_job(client, app, cfg, monkeypatch):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "tenant-key"})
    client.headers["Authorization"] = "Bearer tenant-key"
    store = app.state.store.impl
    store.create_collection("acme", "books")
    store.index_records("acme", "books", "doc", [("0", "passage", {})])
    monkeypatch.setattr(store, "_launch_reindex_job", lambda job_id: None)
    job = store.start_reindex("acme", "books", embed_model="fake-v2")
    return store, job["job_id"]


def _assert_private_job(client, store, job_id, status):
    response = client.get(f"/v1/collections/acme/books/reindex/{job_id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status
    assert response.json()["error"] == _PUBLIC_ERROR
    assert store._get_reindex_job_internal(job_id)["error"] == _PUBLIC_ERROR
    return response.json()


def test_list_reindex_jobs_redacts_historical_errors(client, redaction_job):
    store, job_id = redaction_job
    store._ensure_catalog().update_reindex_job(
        job_id, status="failed", error=_SENSITIVE_ERROR,
        source_digest="internal-corpus-digest",
    )
    response = client.get(
        "/v1/admin/reindex", headers={"Authorization": "Bearer root"},
    )
    assert response.status_code == 200
    job, = response.json()["jobs"]
    assert job["job_id"] == job_id
    assert job["error"] == _PUBLIC_ERROR
    assert "source_config" not in job
    assert "source_digest" not in job
    assert _SENSITIVE_ERROR not in response.text
    assert store._get_reindex_job_internal(job_id)["error"] == _SENSITIVE_ERROR


def test_list_reindex_jobs_catalog_failure_is_private(
    client, app, monkeypatch, caplog,
):
    def fail():
        raise RuntimeError(_SENSITIVE_ERROR)

    monkeypatch.setattr(app.state.store.impl._ensure_catalog(), "list_reindex_jobs", fail)
    response = client.get("/v1/admin/reindex")
    assert response.status_code == 500
    assert response.json()["ok"] is False
    assert response.json()["code"] == "reindex_failed"
    assert _SENSITIVE_ERROR not in response.text
    assert "secret-fragment" not in caplog.text


@pytest.mark.parametrize("status", ["failed", "paused", "cancelled", "committing"])
def test_historical_reindex_errors_are_private(
    client, redaction_job, status,
):
    store, job_id = redaction_job
    store._ensure_catalog().update_reindex_job(
        job_id, status=status, error=_SENSITIVE_ERROR,
    )
    response = client.get(f"/v1/collections/acme/books/reindex/{job_id}")
    assert response.status_code == 200
    assert response.json()["error"] == _PUBLIC_ERROR
    # Reading the public view does not rewrite historical catalog state.
    assert store._get_reindex_job_internal(job_id)["error"] == _SENSITIVE_ERROR
    if status == "cancelled":
        cancelled = client.delete(
            f"/v1/collections/acme/books/reindex/{job_id}",
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["error"] == _PUBLIC_ERROR


def test_reindex_provider_failure_is_private_and_retryable(
    client, redaction_job, monkeypatch, caplog,
):
    store, job_id = redaction_job

    class BrokenEmbedder(FakeEmbedder):
        def encode(self, texts):
            raise RuntimeError(_SENSITIVE_ERROR)

    with monkeypatch.context() as patch:
        patch.setattr(
            store, "_get_embedder_for_config", lambda *a, **k: BrokenEmbedder(),
        )
        store._run_reindex_job(job_id)
    job = _assert_private_job(client, store, job_id, "failed")
    assert job["completed_chunks"] == 0
    assert job["finished_at"] is not None
    assert "EmbedderUnavailableError" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "frame " in caplog.text
    assert "sk-test" not in caplog.text
    assert "secret-fragment" not in caplog.text
    assert "embed.private" not in caplog.text
    assert "/srv/private" not in caplog.text

    store.resume_reindex(job_id)
    store._run_reindex_job(job_id)
    done = store.get_reindex_job(job_id)
    assert done["status"] == "succeeded"
    assert done["error"] is None


@pytest.mark.parametrize("action", ["start", "resume", "cancel"])
@pytest.mark.parametrize("exception", [RuntimeError, ValueError, FileNotFoundError])
def test_reindex_control_failures_are_private(
    client, redaction_job, monkeypatch, caplog, action, exception,
):
    store, job_id = redaction_job

    def fail(*args, **kwargs):
        raise exception(_SENSITIVE_ERROR)

    if action == "start":
        store.cancel_reindex(job_id)
        monkeypatch.setattr(store, "_launch_reindex_job", fail)
        response = client.post(
            "/v1/collections/acme/books/reindex", json={"embed_model": "fake-v2"},
        )
        jobs = store._ensure_catalog().list_reindex_jobs()
        job_id = next(job["job_id"] for job in jobs if job["job_id"] != job_id)
        status = "cancelled"
    elif action == "resume":
        store.pause_reindex(job_id)
        monkeypatch.setattr(store, "_launch_reindex_job", fail)
        response = client.post(
            f"/v1/admin/reindex/{job_id}/resume",
            headers={"Authorization": "Bearer root"},
        )
        status = "failed"
    else:
        monkeypatch.setattr(store, "_cleanup_reindex_job_dir", fail)
        response = client.delete(f"/v1/collections/acme/books/reindex/{job_id}")
        status = "failed"
    assert response.status_code in {400, 404, 409, 500}
    assert _SENSITIVE_ERROR not in response.text
    _assert_private_job(client, store, job_id, status)
    assert exception.__name__ in caplog.text
    assert "sk-test" not in caplog.text
    assert "secret-fragment" not in caplog.text


def test_reindex_install_failure_is_private_and_rolls_back(
    client, redaction_job, monkeypatch, caplog,
):
    store, job_id = redaction_job
    original = store._set_collection_embedder_config

    def fail_target(tenant, collection, config):
        if config["embed_model"] == "fake-v2":
            raise PermissionError(13, _SENSITIVE_ERROR, "/srv/private/index")
        return original(tenant, collection, config)

    monkeypatch.setattr(store, "_set_collection_embedder_config", fail_target)
    store._run_reindex_job(job_id)
    _assert_private_job(client, store, job_id, "paused")
    assert store.get_collection_detail("acme", "books")["embed_model"] != "fake-v2"
    assert "PermissionError errno=13" in caplog.text
    assert "/srv/private" not in caplog.text
    assert "sk-test" not in caplog.text


def test_reindex_recovery_failure_is_private(
    client, redaction_job, monkeypatch, caplog,
):
    store, job_id = redaction_job
    store._ensure_catalog().update_reindex_job(job_id, status="committing")
    (store._reindex_job_dir(job_id) / "index").mkdir(parents=True)

    def fail(*args):
        raise PermissionError(13, _SENSITIVE_ERROR, "/srv/private/index")

    monkeypatch.setattr(store, "_set_collection_embedder_config", fail)
    with pytest.raises(RuntimeError, match="reindex recovery failed"):
        store._recover_reindex_jobs()
    _assert_private_job(client, store, job_id, "committing")
    assert "PermissionError errno=13" in caplog.text
    assert "sk-test" not in caplog.text
    assert "/srv/private" not in caplog.text


def test_reindex_worker_cleanup_failure_is_private(
    client, redaction_job, monkeypatch, caplog,
):
    store, job_id = redaction_job
    store._ensure_catalog().update_reindex_job(job_id, status="cancelling")

    def fail(*args):
        raise PermissionError(13, _SENSITIVE_ERROR, "/srv/private/index")

    monkeypatch.setattr(store, "_cleanup_reindex_job_dir", fail)
    store._fail_reindex_job(job_id, RuntimeError(_SENSITIVE_ERROR))
    _assert_private_job(client, store, job_id, "failed")
    assert "RuntimeError" in caplog.text
    assert "PermissionError errno=13" in caplog.text
    assert "sk-test" not in caplog.text
    assert "/srv/private" not in caplog.text
