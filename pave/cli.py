# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import argparse, json, uuid, pathlib
from datetime import datetime, timezone
from pave.embedders import get_embedder
from pave.stores.base import BaseStore
from pave.stores.local import LocalStore
from pave.service import (
    create_collection as svc_create_collection,
    dump_archive as svc_dump_archive,
    restore_archive as svc_restore_archive,
    delete_collection as svc_delete_collection,
    rename_collection as svc_rename_collection,
    delete_document as svc_delete_document,
    get_query_log_entry as svc_get_query_log_entry,
    ingest_document as svc_ingest_document,
    list_tenants as svc_list_tenants,
    list_collections as svc_list_collections,
    list_query_logs as svc_list_query_logs,
    search as svc_search,
    ServiceError,
)
from pave.config import get_cfg, reload_cfg
from pave import metrics
from pave.runtime_paths import (
    DEFAULT_HOME,
    apply_runtime_env,
    load_asset_text,
    render_config_template,
    resolve_runtime_paths,
)

store: BaseStore | None = None


def _get_store() -> BaseStore:
    global store
    if store is None:
        cfg = get_cfg()
        store = LocalStore(
            data_dir=str(cfg.get("data_dir")),
            embedder=get_embedder(),
        )
    return store

def _dump(out, pretty: bool = True):
    if pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(out, ensure_ascii=False))

def _read(path: str) -> bytes:
    return pathlib.Path(path).read_bytes()


def _prepare_runtime(args) -> None:
    global store
    paths = apply_runtime_env(
        home=args.home,
        config=args.config,
        tenants=args.tenants,
        data_dir=args.data_dir,
    )
    if any((paths.config, paths.tenants, paths.data_dir)):
        reload_cfg()
        store = None


def _write_text(path: pathlib.Path, text: str, *, force: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def cmd_init(args):
    if args.root and args.home:
        raise SystemExit("use either positional ROOT or --home, not both")
    home = args.root or args.home
    if not home and not any((args.config, args.tenants, args.data_dir)):
        home = DEFAULT_HOME
    paths = resolve_runtime_paths(
        home=home,
        config=args.config,
        tenants=args.tenants,
        data_dir=args.data_dir,
    )
    if not (paths.config and paths.tenants and paths.data_dir):
        raise SystemExit("init requires a home root or explicit config/tenants/data-dir")

    config_text = render_config_template(
        data_dir=paths.data_dir,
        tenants_file=paths.tenants,
    )
    tenants_text = load_asset_text("tenants.yml.example")

    written: list[str] = []
    skipped: list[str] = []
    config_path = pathlib.Path(paths.config)
    tenants_path = pathlib.Path(paths.tenants)
    data_dir = pathlib.Path(paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
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
    _dump(out, pretty=not args.compact)

def cmd_create(args):
    out = svc_create_collection(_get_store(), args.tenant, args.collection)
    _dump(out, pretty=not args.compact)

def cmd_ingest(args):
    baseid = args.docid or str(uuid.uuid4())
    meta = json.loads(args.metadata) if args.metadata else {}
    content = _read(args.file)

    # CSV controls (optional)
    csv_opts = None
    if args.csv_has_header or args.csv_meta_cols or args.csv_include_cols:
        csv_opts = {
            "has_header": args.csv_has_header or "auto", # "auto" | "yes" | "no"
            "meta_cols": args.csv_meta_cols or "",       # "name1,name2" or "1,3"
            "include_cols": args.csv_include_cols or "", # "nameA,2,5"
        }

    out = svc_ingest_document(
        _get_store(), args.tenant, args.collection, args.file, content,
        baseid if args.docid else None, meta, csv_options=csv_opts
    )
    _dump(out, pretty=not args.compact)

def cmd_search(args):
    filters = json.loads(args.filters) if args.filters else None
    out = svc_search(
        _get_store(),
        args.tenant,
        args.collection,
        args.query,
        args.k,
        filters=filters,
    )
    _dump(out, pretty=not args.compact)


def cmd_list_queries(args):
    out = svc_list_query_logs(
        _get_store(),
        args.tenant,
        args.collection,
        args.limit,
        args.offset,
    )
    _dump(out, pretty=not args.compact)


def cmd_get_query(args):
    out = svc_get_query_log_entry(
        _get_store(),
        args.tenant,
        args.collection,
        args.query_id,
    )
    _dump(out, pretty=not args.compact)


def cmd_delete(args):
    out = svc_delete_collection(_get_store(), args.tenant, args.collection)
    _dump(out, pretty=not args.compact)

def cmd_rename(args):
    out = svc_rename_collection(
        _get_store(),
        args.tenant,
        args.old_name,
        args.new_name,
    )
    _dump(out, pretty=not args.compact)

def cmd_delete_document(args):
    out = svc_delete_document(
        _get_store(),
        args.tenant,
        args.collection,
        args.docid,
    )
    _dump(out, pretty=not args.compact)

def cmd_dump_archive(args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or f"pavedb-data-{stamp}.zip"
    archive_path, _ = svc_dump_archive(_get_store(), output)
    out = {
        "ok": True,
        "archive": archive_path,
        "source": str(get_cfg().get("data_dir")),
    }
    _dump(out, pretty=not args.compact)

def cmd_restore_archive(args):
    content = _read(args.file)
    out = svc_restore_archive(_get_store(), content)
    _dump(out, pretty=not args.compact)

def cmd_reset_metrics(args):
    cfg = get_cfg()
    data_dir = cfg.get("data_dir")
    if data_dir:
        metrics.set_data_dir(data_dir)
    out = metrics.reset()
    _dump(out, pretty=not args.compact)

def cmd_list_tenants(args):
    out = svc_list_tenants(_get_store())
    _dump(out, pretty=not args.compact)

def cmd_list_collections(args):
    out = svc_list_collections(_get_store(), args.tenant)
    if out.get("ok"):
        out["collections"] = [
            {
                "name": coll["name"],
                "display_name": coll.get("display_name"),
                "embedder_label": coll.get("embedder_label"),
            }
            for coll in out.get("collections", [])
        ]
    _dump(out, pretty=not args.compact)

def main_cli(argv=None):
    p = argparse.ArgumentParser(prog="pavecli")
    p.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON for scripting",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    runtime = argparse.ArgumentParser(add_help=False)
    runtime.add_argument(
        "--home",
        help="Use an instance home dir",
    )
    runtime.add_argument("--config", help="Explicit config.yml path")
    runtime.add_argument("--tenants", help="Explicit tenants.yml path")
    runtime.add_argument("--data-dir", dest="data_dir", help="Explicit data directory")

    p_init = sub.add_parser(
        "init",
        parents=[runtime],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  pavecli init                        "
            "  # ~/pavedb (default)\n"
            "  pavecli init ~/pavedb-staging        "
            "  # separate instance\n"
            "  pavecli init --config /etc/pavedb/"
            "config.yml \\\n"
            "    --tenants /var/pavedb/tenants.yml"
            " \\\n"
            "    --data-dir /var/pavedb/data         "
            " # distro layout"
        ),
    )
    p_init.add_argument("root", nargs="?", help="Instance home dir")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite config and tenants files if they already exist",
    )
    p_init.set_defaults(func=cmd_init)

    p_create = sub.add_parser("create-collection", parents=[runtime])
    p_create.add_argument("tenant")
    p_create.add_argument("collection")
    p_create.set_defaults(func=cmd_create)

    p_ingest = sub.add_parser("ingest", parents=[runtime])
    p_ingest.add_argument("tenant")
    p_ingest.add_argument("collection")
    p_ingest.add_argument("file")
    p_ingest.add_argument("--docid")
    p_ingest.add_argument("--metadata")

    # --- CSV controls ---
    p_ingest.add_argument("--csv-has-header", choices=["auto", "yes", "no"],
                          help="CSV header handling: auto (sniff), yes, or no")
    p_ingest.add_argument(
        "--csv-meta-cols",
        help="CSV columns for metadata only (not indexed). "
             "Names or 1-based indices, comma-separated")
    p_ingest.add_argument(
        "--csv-include-cols",
        help="CSV columns to index. Names or 1-based indices, "
             "comma-separated. Defaults to all non-meta columns")

    p_ingest.set_defaults(func=cmd_ingest)

    p_search = sub.add_parser("search", parents=[runtime])
    p_search.add_argument("tenant")
    p_search.add_argument("collection")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=5)
    p_search.add_argument("--filters", help='JSON object, e.g. {"docid":"DOC-1"}')
    p_search.set_defaults(func=cmd_search)

    p_list_queries = sub.add_parser("list-queries", parents=[runtime])
    p_list_queries.add_argument("tenant")
    p_list_queries.add_argument("collection")
    p_list_queries.add_argument("--limit", type=int, default=50)
    p_list_queries.add_argument("--offset", type=int, default=0)
    p_list_queries.set_defaults(func=cmd_list_queries)

    p_get_query = sub.add_parser("get-query", parents=[runtime])
    p_get_query.add_argument("tenant")
    p_get_query.add_argument("collection")
    p_get_query.add_argument("query_id")
    p_get_query.set_defaults(func=cmd_get_query)

    p_delete = sub.add_parser("delete-collection", parents=[runtime])
    p_delete.add_argument("tenant")
    p_delete.add_argument("collection")
    p_delete.set_defaults(func=cmd_delete)

    p_rename = sub.add_parser("rename-collection", parents=[runtime])
    p_rename.add_argument("tenant")
    p_rename.add_argument("old_name")
    p_rename.add_argument("new_name")
    p_rename.set_defaults(func=cmd_rename)

    p_delete_doc = sub.add_parser("delete-document", parents=[runtime])
    p_delete_doc.add_argument("tenant")
    p_delete_doc.add_argument("collection")
    p_delete_doc.add_argument("docid")
    p_delete_doc.set_defaults(func=cmd_delete_document)

    p_dump = sub.add_parser("dump-archive", parents=[runtime])
    p_dump.add_argument("--output", help="Destination ZIP file path")
    p_dump.set_defaults(func=cmd_dump_archive)

    p_restore = sub.add_parser("restore-archive", parents=[runtime])
    p_restore.add_argument("file")
    p_restore.set_defaults(func=cmd_restore_archive)

    p_reset_metrics = sub.add_parser("reset-metrics", parents=[runtime])
    p_reset_metrics.set_defaults(func=cmd_reset_metrics)

    p_list_tenants = sub.add_parser("list-tenants", parents=[runtime])
    p_list_tenants.set_defaults(func=cmd_list_tenants)

    p_list_collections = sub.add_parser("list-collections", parents=[runtime])
    p_list_collections.add_argument("tenant")
    p_list_collections.set_defaults(func=cmd_list_collections)

    args = p.parse_args(argv)
    if args.cmd != "init":
        _prepare_runtime(args)
    try:
        return args.func(args)
    except ServiceError as exc:
        out = {"ok": False, "code": exc.code, "error": exc.message}
        _dump(out, pretty=not args.compact)
        return 1

if __name__ == "__main__":
    raise SystemExit(main_cli())
