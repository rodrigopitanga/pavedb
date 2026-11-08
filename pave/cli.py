# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import argparse, json, os, pathlib, sys, tempfile, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pave.embedders import LazyEmbedder, get_embedder
from pave.embedders.factory import persistable_embedder_spec, resolve_embedder_spec
from pave.stores.base import BaseStore
from pave.stores.local import LocalStore
from pave.service import (
    create_collection as svc_create_collection,
    dump_archive as svc_dump_archive,
    dump_collection_archive as svc_dump_collection_archive,
    restore_archive as svc_restore_archive,
    restore_collection_archive as svc_restore_collection_archive,
    delete_collection as svc_delete_collection,
    move_collection as svc_move_collection,
    update_collection as svc_update_collection,
    delete_document as svc_delete_document,
    get_chunk as svc_get_chunk,
    get_chunk_content as svc_get_chunk_content,
    get_collection_detail as svc_get_collection_detail,
    get_document as svc_get_document,
    get_query_log_entry as svc_get_query_log_entry,
    ingest_documents_batch as svc_ingest_documents_batch,
    ingest_document as svc_ingest_document,
    ingest_text as svc_ingest_text,
    list_chunks as svc_list_chunks,
    list_tenants as svc_list_tenants,
    list_collections as svc_list_collections,
    list_documents as svc_list_documents,
    list_embedders as svc_list_embedders,
    list_query_homes as svc_list_query_homes,
    replay_query as svc_replay_query,
    start_collection_reindex as svc_start_collection_reindex,
    get_reindex_job as svc_get_reindex_job,
    pause_reindex_job as svc_pause_reindex_job,
    resume_reindex_job as svc_resume_reindex_job,
    cancel_reindex_job as svc_cancel_reindex_job,
    list_reindex_jobs as svc_list_reindex_jobs,
    search as svc_search,
    ServiceError,
    _validate_tenant_name,
)
from pave.auth import current_admin_key, rotate_admin_key
from pave.config import get_cfg, reload_cfg
from pave.data_dir_lease import DataDirInUseError, DataDirLease
from pave.filesystem import mkdir_private
from pave import metrics
from pave.runtime_paths import (
    DEFAULT_HOME,
    apply_runtime_env,
    load_asset_text,
    render_config_template,
    resolve_runtime_paths,
)
from pave.version import CONTACT_EMAIL, WEBSITE_URL, help_notice, version_banner

store: BaseStore | None = None
store_lease: DataDirLease | None = None


@dataclass
class RawCLIOutput:
    content: bytes


def _get_store() -> BaseStore:
    global store, store_lease
    if store is None:
        cfg = get_cfg()
        lease = DataDirLease(str(cfg.get("data_dir"))).acquire()
        try:
            store = LocalStore(
                data_dir=str(cfg.get("data_dir")),
                embedder=LazyEmbedder(get_embedder),
            )
        except BaseException:
            lease.close()
            raise
        store_lease = lease
    return store


def _close_owned_store() -> None:
    global store, store_lease
    if store_lease is None:
        return
    close = getattr(store, "close", None)
    if callable(close):
        close()
    store_lease.close()
    store = None
    store_lease = None


def _dump(out, pretty: bool = True, *, file=None):
    file = file or sys.stdout
    if pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True), file=file)
    else:
        print(json.dumps(out, ensure_ascii=False), file=file)


def _exit_code_for_result(out) -> int:
    if isinstance(out, dict) and out.get("ok") is False:
        return 1
    return 0


def _read(path: str, *, allow_stdin: bool = False) -> bytes:
    try:
        if path == "-" and allow_stdin:
            return sys.stdin.buffer.read()
        return pathlib.Path(path).read_bytes()
    except OSError as exc:
        raise ServiceError("file_read_failed", f"cannot read {path}: {exc}") from exc


class _CLIUsageError(Exception):
    pass


def _parse_json_object_arg(raw: str | None, option: str) -> dict | None:
    if not raw:
        return None
    code = f"invalid_{option[2:].replace('-', '_')}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceError(code, f"{option} is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise ServiceError(code, f"{option} must be a JSON object")
    return parsed


def _prepare_runtime(args) -> None:
    global store
    has_runtime_args = any((args.home, args.config, args.tenants, args.data_dir))
    if has_runtime_args:
        _close_owned_store()
    apply_runtime_env(
        home=args.home,
        config=args.config,
        tenants=args.tenants,
        data_dir=args.data_dir,
    )
    if has_runtime_args:
        reload_cfg()
        store = None


def _write_text(path: pathlib.Path, text: str, *, force: bool) -> bool:
    mkdir_private(path.parent)
    if path.exists() and not force:
        return False
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False,
    )
    temp_path = pathlib.Path(handle.name)
    try:
        with handle:
            temp_path.chmod(0o600)
            handle.write(text)
        if force:
            temp_path.replace(path)
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError:
                return False
        return True
    finally:
        temp_path.unlink(missing_ok=True)


def cmd_init(args):
    if args.root and args.home:
        raise _CLIUsageError("use either positional ROOT or global --home, not both")
    home = args.root or args.home
    paths = resolve_runtime_paths(
        home=home,
        config=args.config,
        tenants=args.tenants,
        data_dir=args.data_dir,
    )
    if not (paths.config and paths.tenants and paths.data_dir):
        raise _CLIUsageError(
            "init requires a home root or explicit config/tenants/data-dir"
        )

    config_text = render_config_template(
        data_dir=paths.data_dir,
        tenants_file=paths.tenants,
        ops_log=str(pathlib.Path(paths.home) / "ops.jsonl"),
        access_log=str(pathlib.Path(paths.home) / "access.log"),
    )
    tenants_text = load_asset_text("tenants.yml.example")

    written: list[str] = []
    skipped: list[str] = []
    config_path = pathlib.Path(paths.config)
    tenants_path = pathlib.Path(paths.tenants)
    home_path = pathlib.Path(paths.home)
    data_dir = pathlib.Path(paths.data_dir)
    mkdir_private(home_path)
    mkdir_private(data_dir)
    for label, path, text in (
        ("config", config_path, config_text),
        ("tenants", tenants_path, tenants_text),
    ):
        if _write_text(path, text, force=args.force):
            written.append(label)
        else:
            skipped.append(label)

    out = {
        "ok": True,
        "home": paths.home,
        "config": str(config_path),
        "tenants": str(tenants_path),
        "data_dir": str(data_dir),
        "written": written,
        "skipped": skipped,
    }
    return out


def _admin_key_error(exc: RuntimeError) -> dict[str, str | bool]:
    return {"ok": False, "code": "admin_key_unavailable", "error": str(exc)}


def cmd_admin_show_key(args):
    try:
        key = current_admin_key(get_cfg(), resolve_runtime_paths().home)
    except RuntimeError as exc:
        return _admin_key_error(exc)
    return RawCLIOutput(f"{key}\n".encode())


def cmd_admin_rotate_key(args):
    try:
        key = rotate_admin_key(get_cfg(), resolve_runtime_paths().home)
    except RuntimeError as exc:
        return _admin_key_error(exc)
    return RawCLIOutput(f"{key}\n".encode())

def cmd_create(args):
    embedder_config = _parse_json_object_arg(
        args.embedder_config, "--embedder-config"
    )
    return svc_create_collection(
        _get_store(),
        args.tenant,
        args.collection,
        display_name=args.display_name,
        embedder=args.embedder,
        embedder_type=args.embedder_type,
        embed_model=args.embed_model,
        embedder_config=embedder_config,
        search_mode=args.search_mode,
        chunking=_parse_json_object_arg(args.chunking, "--chunking"),
        priority_key=args.priority_key,
    )

def cmd_ingest(args):
    baseid = args.docid
    meta = _parse_json_object_arg(args.metadata, "--metadata") or {}
    content = _read(args.file)

    # CSV controls (optional)
    csv_opts = None
    if args.csv_has_header or args.csv_meta_cols or args.csv_include_cols:
        csv_opts = {
            "has_header": args.csv_has_header or "auto", # "auto" | "yes" | "no"
            "meta_cols": args.csv_meta_cols or "",       # "name1,name2" or "1,3"
            "include_cols": args.csv_include_cols or "", # "nameA,2,5"
        }

    return svc_ingest_document(
        _get_store(), args.tenant, args.collection, args.file, content,
        baseid, meta, csv_options=csv_opts
    )


def cmd_ingest_batch(args):
    try:
        payload = json.loads(_read(args.file, allow_stdin=True).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "code": "invalid_batch_json",
            "error": f"invalid batch json: {exc}",
        }
    documents = payload.get("documents") if isinstance(payload, dict) else payload
    if not isinstance(documents, list):
        return {
            "ok": False,
            "code": "invalid_batch",
            "error": "batch input must be a JSON array or object with documents",
        }
    return svc_ingest_documents_batch(
        _get_store(),
        args.tenant,
        args.collection,
        documents,
    )


def cmd_ingest_text(args):
    meta = _parse_json_object_arg(args.metadata, "--metadata") or {}
    return svc_ingest_text(
        _get_store(),
        args.tenant,
        args.collection,
        args.text,
        args.docid,
        meta,
    )


def cmd_search(args):
    cfg = get_cfg()
    filters = _parse_json_object_arg(args.filters, "--filters")
    content_filter = _parse_json_object_arg(
        args.content_filter,
        "--content-filter",
    )
    include_common = bool(args.include_common and cfg.common_enabled)
    return svc_search(
        _get_store(),
        args.tenant,
        args.collection,
        args.query,
        args.k,
        filters=filters,
        content_filter=content_filter,
        mode=args.mode,
        include_common=include_common,
        common_tenant=cfg.common_tenant,
        common_collection=cfg.common_collection,
        actor="admin",
    )


def cmd_list_queries(args):
    return svc_list_query_homes(
        _get_store(),
        tenant=args.tenant,
        collection=args.collection,
        limit=args.limit,
        offset=args.offset,
    )


def cmd_get_query(args):
    return svc_get_query_log_entry(
        _get_store(),
        args.query_id,
    )


def cmd_replay_query(args):
    return svc_replay_query(
        _get_store(),
        args.query_id,
        actor="admin",
    )


def cmd_list_documents(args):
    return svc_list_documents(
        _get_store(),
        args.tenant,
        args.collection,
    )


def cmd_get_document(args):
    return svc_get_document(
        _get_store(),
        args.tenant,
        args.collection,
        args.docid,
    )


def cmd_list_chunks(args):
    return svc_list_chunks(
        _get_store(),
        args.tenant,
        args.collection,
        args.docid,
    )


def cmd_get_chunk(args):
    return svc_get_chunk(
        _get_store(),
        args.tenant,
        args.collection,
        args.rid,
    )


def cmd_get_chunk_content(args):
    out = svc_get_chunk_content(
        _get_store(),
        args.tenant,
        args.collection,
        args.rid,
    )
    if out.get("ok") is False:
        return out
    return RawCLIOutput(content=out["content"])


def cmd_delete(args):
    return svc_delete_collection(_get_store(), args.tenant, args.collection)

def cmd_move(args):
    if args.new_tenant != args.tenant:
        raise ServiceError(
            "move_invalid",
            "move-collection renames within one tenant; "
            f"{args.tenant}/{args.old_name} cannot move to tenant "
            f"{args.new_tenant}",
        )
    return svc_move_collection(
        _get_store(),
        args.tenant,
        args.old_name,
        args.new_name,
    )

def cmd_update_collection(args):
    return svc_update_collection(
        _get_store(),
        args.tenant,
        args.collection,
        display_name=args.display_name,
    )

def cmd_delete_document(args):
    return svc_delete_document(
        _get_store(),
        args.tenant,
        args.collection,
        args.docid,
    )

def cmd_dump_archive(args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.collection:
        output = args.output or (
            f"pavedb-collection-{args.tenant}-{args.collection}-{stamp}.zip"
        )
        archive_path, _ = svc_dump_collection_archive(
            _get_store(), args.tenant, args.collection, output
        )
        return {"ok": True, "archive": archive_path}
    output = args.output or f"pavedb-data-{stamp}.zip"
    skipped: list[str] = []
    archive_path, _ = svc_dump_archive(_get_store(), output, skipped=skipped)
    cfg = get_cfg()
    out = {
        "ok": not skipped,
        "archive": archive_path,
        "source": str(cfg.get("data_dir")),
    }
    if skipped:
        # The archive is usable, but it is not the whole instance.
        out["code"] = "archive_incomplete"
        out["skipped"] = skipped
    return out

def cmd_restore_archive(args):
    content = _read(args.file, allow_stdin=args.cmd == "restore-archive")
    if args.collection:
        return svc_restore_collection_archive(
            _get_store(), args.tenant, args.collection, content,
            replace=args.replace,
        )
    return svc_restore_archive(_get_store(), content)

def cmd_reset_metrics(args):
    return metrics.reset(_get_store())

def cmd_list_tenants(args):
    return svc_list_tenants(_get_store())

def cmd_list_embedders(args):
    return svc_list_embedders(tenant=args.tenant)

def cmd_list_collections(args):
    out = svc_list_collections(_get_store(), args.tenant)
    if out.get("ok"):
        out["collections"] = [
            {
                "name": coll["name"],
                "display_name": coll.get("display_name"),
                "embedder_label": coll.get("embedder_label"),
                "vector_space_key": coll.get("vector_space_key"),
            }
            for coll in out.get("collections", [])
        ]
    return out


def cmd_get_collection(args):
    return svc_get_collection_detail(
        _get_store(),
        args.tenant,
        args.collection,
    )


def cmd_reindex(args):
    embedder_config = _parse_json_object_arg(
        args.embedder_config, "--embedder-config"
    )
    if args.embedder is not None:
        if any((
            args.embedder_type is not None,
            args.embed_model is not None,
            args.embedder_config is not None,
        )):
            raise ServiceError(
                "reindex_invalid",
                "--embedder cannot be combined with detailed embedder options",
            )
        try:
            spec = persistable_embedder_spec(
                resolve_embedder_spec(get_cfg(), selector=args.embedder)
            )
        except ValueError as exc:
            raise ServiceError("reindex_invalid", str(exc)) from exc
        embedder_type = spec["embedder_type"]
        embed_model = spec["embed_model"]
        embedder_config = spec["embedder_config"]
    else:
        embedder_type = args.embedder_type
        embed_model = args.embed_model

    store = _get_store()
    started = svc_start_collection_reindex(
        store, args.tenant, args.collection,
        embedder_type=embedder_type,
        embed_model=embed_model,
        embedder_config=embedder_config,
    )
    if not started.get("ok"):
        return started
    return _follow_reindex_job(store, started["job_id"])


def _follow_reindex_job(store: BaseStore, job_id: str):
    last_progress = None
    try:
        while True:
            job = svc_get_reindex_job(store, job_id)
            if not job.get("ok"):
                return job
            progress = (job["status"], job["completed_chunks"], job["total_chunks"])
            if progress != last_progress:
                print(
                    f"reindex {job_id}: {progress[0]} "
                    f"{progress[1]}/{progress[2]} chunks",
                    file=sys.stderr,
                )
                last_progress = progress
            if job["status"] in {"succeeded", "failed", "cancelled", "paused"}:
                if job["status"] == "failed":
                    job["ok"] = False
                return job
            time.sleep(0.2)
    except KeyboardInterrupt:
        paused = svc_pause_reindex_job(store, job_id)
        if not paused.get("ok"):
            paused = svc_get_reindex_job(store, job_id)
        while paused.get("ok") and paused["status"] == "pausing":
            time.sleep(0.2)
            paused = svc_get_reindex_job(store, job_id)
        if paused.get("ok") and paused["status"] == "paused":
            paused["code"] = "reindex_paused"
            paused["resume_hint"] = f"pavecli reindex-resume {job_id}"
        return paused


def cmd_reindex_status(args):
    if args.job_id:
        return svc_get_reindex_job(_get_store(), args.job_id)
    return svc_list_reindex_jobs(_get_store())


def cmd_reindex_resume(args):
    store = _get_store()
    resumed = svc_resume_reindex_job(store, args.job_id)
    if not resumed.get("ok"):
        return resumed
    return _follow_reindex_job(store, args.job_id)


def cmd_reindex_cancel(args):
    return svc_cancel_reindex_job(_get_store(), args.job_id)


def _command(
    subparsers,
    name,
    *,
    help,
    description,
    example,
    parents=None,
):
    examples = (example,) if isinstance(example, str) else example
    epilog = "example:\n" if len(examples) == 1 else "examples:\n"
    epilog += "\n".join(f"  {line}" for line in examples)
    return subparsers.add_parser(
        name,
        parents=parents or [],
        allow_abbrev=False,
        help=help,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )


def _collection_ref(value: str) -> tuple[str, str]:
    tenant, sep, collection = value.partition("/")
    if not sep or not tenant or not collection or "/" in collection:
        raise argparse.ArgumentTypeError(
            f"expected TENANT/COLLECTION, got {value!r}"
        )
    return _cli_tenant(tenant), collection


def _cli_tenant(value: str) -> str:
    try:
        return _validate_tenant_name(value)
    except ServiceError as exc:
        raise argparse.ArgumentTypeError(exc.message) from exc


class _CollectionRef(argparse.Action):
    """Split one TENANT/COLLECTION argument into two namespace attributes.

    The collection lands in `dest`, the tenant in `tenant_dest`, so command
    handlers keep reading `args.tenant` and `args.collection`.
    """

    def __init__(self, *args, tenant_dest: str = "tenant", **kwargs):
        self.tenant_dest = tenant_dest
        super().__init__(*args, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        tenant, collection = values or (None, None)
        setattr(namespace, self.tenant_dest, tenant)
        setattr(namespace, self.dest, collection)


def _add_collection_ref(
    parser,
    dest: str = "collection",
    *,
    help: str = "Tenant and collection, e.g. demo/books",
    tenant_dest: str = "tenant",
    metavar: str = "tenant/collection",
    optional: bool = False,
) -> None:
    parser.add_argument(
        dest,
        nargs="?" if optional else None,
        metavar=metavar,
        type=_collection_ref,
        action=_CollectionRef,
        tenant_dest=tenant_dest,
        help=help,
    )


def build_parser() -> argparse.ArgumentParser:
    runtime_note = (
        "pre-1.0 note:\n"
        "  Runtime selectors (--home, --config, --tenants, --data-dir) "
        "are global options.\n"
        "  Pass them before the command, e.g. "
        "pavecli --home ./instance list-tenants.\n"
        "  Store commands open the local data directory directly.\n"
        "  They refuse to run while another PaveDB process owns it."
    )
    notice = help_notice("pavecli")
    contact = (
        f"Report bugs to: {CONTACT_EMAIL}\n"
        f"PaveDB home page: {WEBSITE_URL}"
    )
    p = argparse.ArgumentParser(
        prog="pavecli",
        allow_abbrev=False,
        description="PaveDB CLI",
        usage="%(prog)s [OPTION]... COMMAND [ARG]...",
        epilog=runtime_note + (f"\n\n{notice}" if notice else "")
        + f"\n\n{contact}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON for scripting",
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=version_banner("pavecli"),
        help="Print version, copyright, and license information",
    )
    p.add_argument(
        "--home",
        help=(
            f"Instance home directory; omitted siblings default under it; "
            f"defaults to {DEFAULT_HOME}"
        ),
    )
    p.add_argument(
        "--config",
        help="Explicit config.yml path; default comes from PAVEDB_CONFIG/config search",
    )
    p.add_argument(
        "--tenants",
        help=(
            "Explicit tenants.yml path; default comes from "
            "PAVEDB_AUTH__TENANTS_FILE/config"
        ),
    )
    p.add_argument(
        "--data-dir",
        dest="data_dir",
        help="Explicit data directory; default comes from PAVEDB_DATA_DIR/config",
    )
    sub = p.add_subparsers(
        dest="cmd", required=True, title="commands", metavar="COMMAND",
        prog="pavecli",
    )

    p_help = _command(
        sub,
        "help",
        help="Show help for a command.",
        description="Show help for the CLI or a named command.",
        example=("pavecli help", "pavecli help search", "pavecli help admin show-key"),
    )
    p_help.add_argument("command", nargs="*", help="Command path to describe")

    p_init = _command(
        sub,
        "init",
        help="Create a PaveDB instance config and data directory.",
        description=(
            "Create config.yml, tenants.yml, and the data directory for an "
            "instance."
        ),
        example=(
            "pavecli init",
            "pavecli init ~/pavedb-staging",
            "pavecli --config /etc/pavedb/config.yml \\\n"
            "  --tenants /var/pavedb/tenants.yml \\\n"
            "  --data-dir /var/pavedb/data init",
        ),
    )
    p_init.add_argument("root", nargs="?", help="Instance home directory")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite config and tenants files if they already exist",
    )
    p_init.set_defaults(func=cmd_init)

    p_admin = _command(
        sub,
        "admin",
        help="Manage the local admin key.",
        description="Show or rotate the local instance admin key.",
        example=(
            "pavecli admin show-key",
            "pavecli admin rotate-key",
        ),
    )
    admin_sub = p_admin.add_subparsers(dest="admin_cmd", required=True)
    p_show_key = _command(
        admin_sub,
        "show-key",
        help="Print the existing admin key.",
        description="Print the existing admin key without generating one.",
        example="pavecli admin show-key",
    )
    p_show_key.set_defaults(func=cmd_admin_show_key)
    p_rotate_key = _command(
        admin_sub,
        "rotate-key",
        help="Replace the local admin key.",
        description="Atomically replace the local admin key and print it once.",
        example="pavecli admin rotate-key",
    )
    p_rotate_key.set_defaults(func=cmd_admin_rotate_key)

    p_create = _command(
        sub,
        "create-collection",
        help="Create a collection.",
        description=(
            "Create a collection, optionally with display and embedder "
            "configuration."
        ),
        example='pavecli create-collection --display-name "Books" demo/books',
    )
    _add_collection_ref(
        p_create,
        help=(
            "Tenant and new collection, e.g. demo/books. The collection slug "
            "takes lowercase letters, digits, and hyphens; must start with a "
            "letter or digit; max 63 chars"
        ),
    )
    p_create.add_argument("--display-name", help="Human-readable collection label")
    p_create.add_argument(
        "--embedder",
        help=(
            "Configured embedder selector: instance key, type, type:model, "
            "or full vector-space key"
        ),
    )
    p_create.add_argument(
        "--embedder-type",
        help="Embedder type, such as sbert, openai, or ollama",
    )
    p_create.add_argument("--embed-model", help="Embedding model name")
    p_create.add_argument(
        "--embedder-config",
        help="JSON object with embedder-specific config",
    )
    p_create.add_argument(
        "--search-mode",
        choices=("vector", "boost", "hybrid"),
        help="Default ranking mode for searches that name none (vector)",
    )
    p_create.add_argument(
        "--priority-key",
        help=(
            "Metadata field whose value in [-1, 1] scales scores by "
            "(1 + value); omitted: prio_boost"
        ),
    )
    p_create.add_argument(
        "--chunking",
        help=(
            'JSON object, e.g. {"strategy":"fixed","size":800,"overlap":100} '
            'or {"strategy":"none"}; omitted fields take the instance default'
        ),
    )
    p_create.set_defaults(func=cmd_create)

    p_ingest = _command(
        sub,
        "ingest",
        help="Index a file into a collection.",
        description="Preprocess and index a TXT, CSV, or PDF file into a collection.",
        example=(
            "pavecli ingest --docid book-1 "
            "--metadata '{\"lang\":\"en\"}' demo/books ./book.txt"
        ),
    )
    _add_collection_ref(p_ingest)
    p_ingest.add_argument("file", help="Path to TXT, CSV, or PDF input")
    p_ingest.add_argument(
        "--docid",
        help="Stable document ID; defaults from filename when omitted",
    )
    p_ingest.add_argument("--metadata", help="JSON object attached to the document")

    # --- CSV controls ---
    p_ingest.add_argument("--csv-has-header", choices=["auto", "yes", "no"],
                          help="CSV header handling: auto, yes, or no")
    p_ingest.add_argument(
        "--csv-meta-cols",
        help="CSV columns for metadata only (not indexed). "
             "Names or 1-based indices, comma-separated")
    p_ingest.add_argument(
        "--csv-include-cols",
        help="CSV columns to index. Names or 1-based indices, "
             "comma-separated. Defaults to all non-meta columns")

    p_ingest.set_defaults(func=cmd_ingest)

    p_ingest_batch = _command(
        sub,
        "ingest-batch",
        help="Index raw-text documents into a collection.",
        description="Index a JSON batch of raw-text documents.",
        example=(
            "pavecli ingest-batch demo/books ./documents.json",
            "pavecli ingest-batch demo/books - < documents.json",
        ),
    )
    _add_collection_ref(p_ingest_batch)
    p_ingest_batch.add_argument("file", help="JSON batch file, or - for stdin")
    p_ingest_batch.set_defaults(func=cmd_ingest_batch)

    p_ingest_text = _command(
        sub,
        "ingest-text",
        help="Index raw text into a collection.",
        description="Index one raw-text document.",
        example=(
            'pavecli ingest-text --docid note-1 demo/books "Captain Nemo"'
        ),
    )
    _add_collection_ref(p_ingest_text)
    p_ingest_text.add_argument("text", help="Raw text to index")
    p_ingest_text.add_argument(
        "--docid",
        help="Stable document ID; generated when omitted",
    )
    p_ingest_text.add_argument(
        "--metadata",
        help="JSON object attached to the document",
    )
    p_ingest_text.set_defaults(func=cmd_ingest_text)

    p_search = _command(
        sub,
        "search",
        help="Search a collection.",
        description=(
            "Run ranked search over a collection and print the response "
            "envelope."
        ),
        example='pavecli search -k 3 demo/books "captain nemo"',
    )
    _add_collection_ref(p_search)
    p_search.add_argument("query", help="Natural-language search query")
    p_search.add_argument("-k", type=int, default=5, help="Number of matches to return")
    p_search.add_argument(
        "--mode",
        choices=("vector", "boost", "hybrid"),
        help="Ranking mode; defaults to the collection's search mode",
    )
    p_search.add_argument(
        "--include-common",
        action="store_true",
        help="Include configured common collection results",
    )
    p_search.add_argument("--filters", help='JSON object, e.g. {"docid":"DOC-1"}')
    p_search.add_argument(
        "--content-filter",
        help=(
            "JSON object with op exact, phrase, prefix, or contains and a "
            "non-empty value"
        ),
    )
    p_search.set_defaults(func=cmd_search)

    p_list_queries = _command(
        sub,
        "list-queries",
        help="List logged query IDs.",
        description=(
            "List logged query IDs, optionally scoped to a tenant or "
            "collection."
        ),
        example="pavecli list-queries --tenant demo --collection books --limit 20",
    )
    # Filters, not an address: --collection alone matches that name in
    # every tenant, so the two stay independent options.
    p_list_queries.add_argument("--tenant", type=_cli_tenant, help="Filter by tenant")
    p_list_queries.add_argument("--collection", help="Filter by collection")
    p_list_queries.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum entries to return",
    )
    p_list_queries.add_argument("--offset", type=int, default=0, help="Entries to skip")
    p_list_queries.set_defaults(func=cmd_list_queries)

    p_get_query = _command(
        sub,
        "get-query",
        help="Fetch a logged query by ID.",
        description=(
            "Resolve a query_id through the catalog and print the stored query "
            "log entry."
        ),
        example="pavecli get-query 6f7cfd26-7d7f-4e5d-942b-f2a516f515a6",
    )
    p_get_query.add_argument("query_id", help="Query ID to fetch")
    p_get_query.set_defaults(func=cmd_get_query)

    p_replay_query = _command(
        sub,
        "replay-query",
        help="Replay a logged query by ID.",
        description=(
            "Resolve a query_id through the catalog and run the stored query "
            "again."
        ),
        example="pavecli replay-query 6f7cfd26-7d7f-4e5d-942b-f2a516f515a6",
    )
    p_replay_query.add_argument("query_id", help="Query ID to replay")
    p_replay_query.set_defaults(func=cmd_replay_query)

    p_list_docs = _command(
        sub,
        "list-documents",
        help="List documents in a collection.",
        description="Print indexed documents and their collection metadata.",
        example="pavecli list-documents demo/books",
    )
    _add_collection_ref(p_list_docs)
    p_list_docs.set_defaults(func=cmd_list_documents)

    p_get_doc = _command(
        sub,
        "get-document",
        help="Fetch one document by ID.",
        description="Print document metadata and chunk summary for one document.",
        example="pavecli get-document demo/books book-1",
    )
    _add_collection_ref(p_get_doc)
    p_get_doc.add_argument("docid", help="Document ID")
    p_get_doc.set_defaults(func=cmd_get_document)

    p_list_chunks = _command(
        sub,
        "list-chunks",
        help="List chunks for a document.",
        description="Print chunk metadata for a document without chunk text content.",
        example="pavecli list-chunks demo/books book-1",
    )
    _add_collection_ref(p_list_chunks)
    p_list_chunks.add_argument("docid", help="Document ID")
    p_list_chunks.set_defaults(func=cmd_list_chunks)

    p_get_chunk = _command(
        sub,
        "get-chunk",
        help="Fetch chunk metadata.",
        description="Print metadata for one chunk by record ID.",
        example="pavecli get-chunk demo/books book-1::chunk_0",
    )
    _add_collection_ref(p_get_chunk)
    p_get_chunk.add_argument("rid", help="Chunk record ID")
    p_get_chunk.set_defaults(func=cmd_get_chunk)

    p_get_chunk_content = _command(
        sub,
        "get-chunk-content",
        help="Print raw chunk text.",
        description="Write one chunk's text content to stdout.",
        example="pavecli get-chunk-content demo/books book-1::chunk_0",
    )
    _add_collection_ref(p_get_chunk_content)
    p_get_chunk_content.add_argument("rid", help="Chunk record ID")
    p_get_chunk_content.set_defaults(func=cmd_get_chunk_content)

    p_delete = _command(
        sub,
        "delete-collection",
        help="Delete a collection.",
        description=(
            "Remove a collection, including its local metadata and vector "
            "index."
        ),
        example="pavecli delete-collection demo/books",
    )
    _add_collection_ref(p_delete)
    p_delete.set_defaults(func=cmd_delete)

    p_move = _command(
        sub,
        "move-collection",
        help="Move a collection to a new name.",
        description=(
            "Rename the collection slug and move its catalog/query-home "
            "records."
        ),
        example="pavecli move-collection demo/old-books demo/books",
    )
    _add_collection_ref(
        p_move, "old_name", help="Tenant and current collection, e.g. demo/old"
    )
    _add_collection_ref(
        p_move,
        "new_name",
        tenant_dest="new_tenant",
        metavar="tenant/new-collection",
        help=(
            "Same tenant and the new collection slug: lowercase letters, "
            "digits, and hyphens; must start with a letter or digit; max 63 "
            "chars"
        ),
    )
    p_move.set_defaults(func=cmd_move)

    p_update_collection = _command(
        sub,
        "update-collection",
        help="Update collection metadata.",
        description=(
            "Update collection metadata such as the display name, without "
            "re-indexing."
        ),
        example='pavecli update-collection --display-name "Books" demo/books',
    )
    _add_collection_ref(p_update_collection)
    p_update_collection.add_argument(
        "--display-name",
        required=True,
        help="New human-readable collection label",
    )
    p_update_collection.set_defaults(func=cmd_update_collection)

    p_delete_doc = _command(
        sub,
        "delete-document",
        help="Delete a document by ID.",
        description="Remove one document and its chunks from a collection.",
        example="pavecli delete-document demo/books book-1",
    )
    _add_collection_ref(p_delete_doc)
    p_delete_doc.add_argument("docid", help="Document ID")
    p_delete_doc.set_defaults(func=cmd_delete_document)

    p_dump = _command(
        sub,
        "dump-archive",
        help="Export the instance, or one collection, as an archive.",
        description=(
            "Offline only; stop the server before reading its data directory. "
            "With tenant/collection, writes that collection as a portable "
            "archive. Without it, writes the whole instance; collections that "
            "fail validation are left out and listed under 'skipped', and the "
            "command exits non-zero."
        ),
        example=(
            "pavecli dump-archive --output pavedb-data.zip",
            "pavecli dump-archive --output books.zip demo/books",
        ),
    )
    _add_collection_ref(
        p_dump, optional=True, help="Collection to export; omit for the instance"
    )
    p_dump.add_argument("--output", help="Destination ZIP file path")
    p_dump.set_defaults(func=cmd_dump_archive)

    p_restore = _command(
        sub,
        "restore-archive",
        help="Restore the instance, or one collection, from an archive.",
        description=(
            "Offline only; stop the server first. With tenant/collection, "
            "creates that collection from a same-version collection archive, "
            "or with --replace overwrites the existing one. Without it, "
            "replaces the data directory from an instance archive."
        ),
        example=(
            "pavecli restore-archive pavedb-data.zip",
            "pavecli restore-archive demo/books books.zip",
            "pavecli restore-archive - < pavedb-data.zip",
        ),
    )
    _add_collection_ref(
        p_restore,
        optional=True,
        help="Destination collection; omit for an instance archive",
    )
    p_restore.add_argument("file", help="Archive zip file, or - for stdin")
    p_restore.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite an existing collection instead of creating it",
    )
    p_restore.set_defaults(func=cmd_restore_archive)

    p_reset_metrics = _command(
        sub,
        "reset-metrics",
        help="Reset persisted metrics.",
        description=(
            "Clear the shared runtime metrics in the configured catalog "
            "database."
        ),
        example="pavecli reset-metrics",
    )
    p_reset_metrics.set_defaults(func=cmd_reset_metrics)

    p_list_tenants = _command(
        sub,
        "list-tenants",
        help="List tenants with cataloged collections.",
        description="Print tenants currently present in the collection catalog.",
        example="pavecli list-tenants",
    )
    p_list_tenants.set_defaults(func=cmd_list_tenants)

    p_list_embedders = _command(
        sub,
        "list-embedders",
        help="List configured embedder instances.",
        description=(
            "Print configured embedder instances and the resolved default "
            "routing pool."
        ),
        example=(
            "pavecli list-embedders",
            "pavecli list-embedders --tenant demo",
        ),
    )
    p_list_embedders.add_argument(
        "--tenant",
        type=_cli_tenant,
        help="Tenant namespace to echo in the response",
    )
    p_list_embedders.set_defaults(func=cmd_list_embedders)

    p_list_collections = _command(
        sub,
        "list-collections",
        help="List collections for a tenant.",
        description=(
            "Print collection names, display labels, and embedder labels for "
            "a tenant."
        ),
        example="pavecli list-collections demo",
    )
    p_list_collections.add_argument(
        "tenant", type=_cli_tenant, help="Tenant namespace"
    )
    p_list_collections.set_defaults(func=cmd_list_collections)

    p_get_collection = _command(
        sub,
        "get-collection",
        help="Fetch collection details.",
        description=(
            "Print catalog, embedder, vector-space, document, and chunk "
            "details for a collection."
        ),
        example="pavecli get-collection demo/books",
    )
    _add_collection_ref(p_get_collection)
    p_get_collection.set_defaults(func=cmd_get_collection)

    p_reindex = _command(
        sub,
        "reindex",
        help="Rebuild collection vectors and follow the job.",
        description=(
            "Offline only; start a reindex job and show progress until it "
            "finishes. Ctrl-C pauses the job after its current batch."
        ),
        example="pavecli reindex --embedder v2 demo/books",
    )
    _add_collection_ref(p_reindex)
    p_reindex.add_argument(
        "--embedder", help="Configured embedder selector for the new vector space"
    )
    p_reindex.add_argument("--embedder-type", help="Target embedder type")
    p_reindex.add_argument("--embed-model", help="Target embedding model")
    p_reindex.add_argument(
        "--embedder-config", help="JSON object with target embedder settings"
    )
    p_reindex.set_defaults(func=cmd_reindex)

    p_reindex_status = _command(
        sub,
        "reindex-status",
        help="Show one reindex job or list all jobs.",
        description="Show durable reindex progress, or list all retained jobs.",
        example=("pavecli reindex-status", "pavecli reindex-status JOB_ID"),
    )
    p_reindex_status.add_argument("job_id", nargs="?", help="Reindex job ID")
    p_reindex_status.set_defaults(func=cmd_reindex_status)

    p_reindex_resume = _command(
        sub,
        "reindex-resume",
        help="Resume a paused or failed reindex job.",
        description=(
            "Resume reindexing from the persisted staged index and follow "
            "progress until it finishes. Ctrl-C pauses the job."
        ),
        example="pavecli reindex-resume JOB_ID",
    )
    p_reindex_resume.add_argument("job_id", help="Reindex job ID")
    p_reindex_resume.set_defaults(func=cmd_reindex_resume)

    p_reindex_cancel = _command(
        sub,
        "reindex-cancel",
        help="Cancel a reindex job.",
        description="Cancel the job and discard its staged vectors.",
        example="pavecli reindex-cancel JOB_ID",
    )
    p_reindex_cancel.add_argument("job_id", help="Reindex job ID")
    p_reindex_cancel.set_defaults(func=cmd_reindex_cancel)

    return p


def main_cli(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if args.cmd == "help":
        target = p
        for name in args.command:
            subcommands = next(
                (action.choices for action in target._actions
                 if isinstance(action, argparse._SubParsersAction)),
                {},
            )
            if name not in subcommands:
                p.error(f"unknown command: {' '.join(args.command)}")
            target = subcommands[name]
        target.print_help()
        return 0
    try:
        try:
            if args.cmd != "init":
                _prepare_runtime(args)
            out = args.func(args)
        except _CLIUsageError as exc:
            p.error(str(exc))
        except ServiceError as exc:
            out = {"ok": False, "code": exc.code, "error": exc.message}
        except DataDirInUseError as exc:
            out = {
                "ok": False,
                "code": "data_dir_in_use",
                "error": str(exc),
            }
    finally:
        _close_owned_store()
    if isinstance(out, RawCLIOutput):
        sys.stdout.buffer.write(out.content)
        return 0
    _dump(
        out,
        pretty=not args.compact,
        file=sys.stderr if _exit_code_for_result(out) else sys.stdout,
    )
    if isinstance(out, dict) and out.get("code") == "reindex_paused":
        return 130
    return _exit_code_for_result(out)

if __name__ == "__main__":
    raise SystemExit(main_cli())
