# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import argparse, json, pathlib, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pave.embedders import LazyEmbedder, get_embedder
from pave.stores.base import BaseStore
from pave.stores.local import LocalStore
from pave.service import (
    create_collection as svc_create_collection,
    dump_archive as svc_dump_archive,
    restore_archive as svc_restore_archive,
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
from pave.slugify import slugify
from pave.version import help_notice, version_banner

store: BaseStore | None = None


@dataclass
class RawCLIOutput:
    content: bytes


def _get_store() -> BaseStore:
    global store
    if store is None:
        cfg = get_cfg()
        store = LocalStore(
            data_dir=str(cfg.get("data_dir")),
            embedder=LazyEmbedder(get_embedder),
        )
    return store

def _dump(out, pretty: bool = True):
    if pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(out, ensure_ascii=False))


def _exit_code_for_result(out) -> int:
    if isinstance(out, dict) and out.get("ok") is False:
        return 1
    return 0


def _read(path: str) -> bytes:
    return pathlib.Path(path).read_bytes()


def _parse_filters_arg(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceError("invalid_filters", f"--filters is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise ServiceError("invalid_filters", "--filters must be a JSON object")
    return parsed


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
        raise SystemExit("use either positional ROOT or global --home, not both")
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
    return out

def cmd_create(args):
    embedder_config = (
        json.loads(args.embedder_config)
        if args.embedder_config
        else None
    )
    collection = args.collection
    display_name = args.display_name
    slug = slugify(collection)
    if slug and slug != collection:
        collection = slug
        display_name = display_name or args.collection.strip()
    return svc_create_collection(
        _get_store(),
        args.tenant,
        collection,
        display_name=display_name,
        embedder_type=args.embedder_type,
        embed_model=args.embed_model,
        embedder_config=embedder_config,
    )

def cmd_ingest(args):
    baseid = args.docid
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

    return svc_ingest_document(
        _get_store(), args.tenant, args.collection, args.file, content,
        baseid, meta, csv_options=csv_opts
    )


def cmd_ingest_batch(args):
    try:
        payload = json.loads(_read(args.file).decode("utf-8"))
    except json.JSONDecodeError as exc:
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
    meta = json.loads(args.metadata) if args.metadata else {}
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
    filters = _parse_filters_arg(args.filters)
    include_common = bool(args.include_common and cfg.common_enabled)
    return svc_search(
        _get_store(),
        args.tenant,
        args.collection,
        args.query,
        args.k,
        filters=filters,
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
    output = args.output or f"pavedb-data-{stamp}.zip"
    archive_path, _ = svc_dump_archive(_get_store(), output)
    cfg = get_cfg()
    out = {
        "ok": True,
        "archive": archive_path,
        "source": str(cfg.get("data_dir")),
    }
    return out

def cmd_restore_archive(args):
    content = _read(args.file)
    return svc_restore_archive(_get_store(), content)

def cmd_reset_metrics(args):
    cfg = get_cfg()
    data_dir = cfg.get("data_dir")
    if data_dir:
        metrics.set_data_dir(data_dir)
    return metrics.reset()

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
        help=help,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )


def build_parser() -> argparse.ArgumentParser:
    runtime_note = (
        "pre-1.0 note:\n"
        "  Runtime selectors (--home, --config, --tenants, --data-dir) "
        "are global options.\n"
        "  Pass them before the command, e.g. "
        "pavecli --home ./instance list-tenants."
    )
    notice = help_notice("pavecli")
    p = argparse.ArgumentParser(
        prog="pavecli",
        description="PaveDB CLI",
        epilog=runtime_note + (f"\n\n{notice}" if notice else ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON for scripting",
    )
    p.add_argument(
        "--version",
        action="version",
        version=version_banner("pavecli"),
        help="Print version, copyright, and license information",
    )
    p.add_argument(
        "--home",
        help=(
            f"Instance home directory; no global default; init uses "
            f"{DEFAULT_HOME} when no explicit path is passed"
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
    sub = p.add_subparsers(dest="cmd", required=True)

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

    p_create = _command(
        sub,
        "create-collection",
        help="Create a collection.",
        description=(
            "Create a collection, optionally with display and embedder "
            "configuration."
        ),
        example='pavecli create-collection demo books --display-name "Books"',
    )
    p_create.add_argument("tenant", help="Tenant namespace")
    p_create.add_argument(
        "collection",
        help=(
            "Collection slug: lowercase letters, digits, and hyphens; must "
            "start with a letter or digit; max 63 chars"
        ),
    )
    p_create.add_argument("--display-name", help="Human-readable collection label")
    p_create.add_argument(
        "--embedder-type",
        help="Embedder type, such as sbert, openai, or ollama",
    )
    p_create.add_argument("--embed-model", help="Embedding model name")
    p_create.add_argument(
        "--embedder-config",
        help="JSON object with embedder-specific config",
    )
    p_create.set_defaults(func=cmd_create)

    p_ingest = _command(
        sub,
        "ingest",
        help="Index a file into a collection.",
        description="Preprocess and index a TXT, CSV, or PDF file into a collection.",
        example=(
            "pavecli ingest demo books ./book.txt --docid book-1 "
            "--metadata '{\"lang\":\"en\"}'"
        ),
    )
    p_ingest.add_argument("tenant", help="Tenant namespace")
    p_ingest.add_argument("collection", help="Collection name")
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
            "pavecli ingest-batch demo books ./documents.json"
        ),
    )
    p_ingest_batch.add_argument("tenant", help="Tenant namespace")
    p_ingest_batch.add_argument("collection", help="Collection name")
    p_ingest_batch.add_argument("file", help="JSON batch input file")
    p_ingest_batch.set_defaults(func=cmd_ingest_batch)

    p_ingest_text = _command(
        sub,
        "ingest-text",
        help="Index raw text into a collection.",
        description="Index one raw-text document.",
        example=(
            'pavecli ingest-text demo books "Captain Nemo" --docid note-1'
        ),
    )
    p_ingest_text.add_argument("tenant", help="Tenant namespace")
    p_ingest_text.add_argument("collection", help="Collection name")
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
            "Run semantic search over a collection and print the response "
            "envelope."
        ),
        example='pavecli search demo books "captain nemo" -k 3',
    )
    p_search.add_argument("tenant", help="Tenant namespace")
    p_search.add_argument("collection", help="Collection name")
    p_search.add_argument("query", help="Natural-language search query")
    p_search.add_argument("-k", type=int, default=5, help="Number of matches to return")
    p_search.add_argument(
        "--include-common",
        action="store_true",
        help="Include configured common collection results",
    )
    p_search.add_argument("--filters", help='JSON object, e.g. {"docid":"DOC-1"}')
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
    p_list_queries.add_argument("--tenant", help="Filter by tenant")
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
        example="pavecli list-documents demo books",
    )
    p_list_docs.add_argument("tenant", help="Tenant namespace")
    p_list_docs.add_argument("collection", help="Collection name")
    p_list_docs.set_defaults(func=cmd_list_documents)

    p_get_doc = _command(
        sub,
        "get-document",
        help="Fetch one document by ID.",
        description="Print document metadata and chunk summary for one document.",
        example="pavecli get-document demo books book-1",
    )
    p_get_doc.add_argument("tenant", help="Tenant namespace")
    p_get_doc.add_argument("collection", help="Collection name")
    p_get_doc.add_argument("docid", help="Document ID")
    p_get_doc.set_defaults(func=cmd_get_document)

    p_list_chunks = _command(
        sub,
        "list-chunks",
        help="List chunks for a document.",
        description="Print chunk metadata for a document without chunk text content.",
        example="pavecli list-chunks demo books book-1",
    )
    p_list_chunks.add_argument("tenant", help="Tenant namespace")
    p_list_chunks.add_argument("collection", help="Collection name")
    p_list_chunks.add_argument("docid", help="Document ID")
    p_list_chunks.set_defaults(func=cmd_list_chunks)

    p_get_chunk = _command(
        sub,
        "get-chunk",
        help="Fetch chunk metadata.",
        description="Print metadata for one chunk by record ID.",
        example="pavecli get-chunk demo books book-1::chunk_0",
    )
    p_get_chunk.add_argument("tenant", help="Tenant namespace")
    p_get_chunk.add_argument("collection", help="Collection name")
    p_get_chunk.add_argument("rid", help="Chunk record ID")
    p_get_chunk.set_defaults(func=cmd_get_chunk)

    p_get_chunk_content = _command(
        sub,
        "get-chunk-content",
        help="Print raw chunk text.",
        description="Write one chunk's text content to stdout.",
        example="pavecli get-chunk-content demo books book-1::chunk_0",
    )
    p_get_chunk_content.add_argument("tenant", help="Tenant namespace")
    p_get_chunk_content.add_argument("collection", help="Collection name")
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
        example="pavecli delete-collection demo books",
    )
    p_delete.add_argument("tenant", help="Tenant namespace")
    p_delete.add_argument("collection", help="Collection name")
    p_delete.set_defaults(func=cmd_delete)

    p_move = _command(
        sub,
        "move-collection",
        help="Move a collection to a new name.",
        description=(
            "Rename the collection slug and move its catalog/query-home "
            "records."
        ),
        example="pavecli move-collection demo old-books books",
    )
    p_move.add_argument("tenant", help="Tenant namespace")
    p_move.add_argument("old_name", help="Current collection name")
    p_move.add_argument(
        "new_name",
        help=(
            "New collection slug: lowercase letters, digits, and hyphens; "
            "must start with a letter or digit; max 63 chars"
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
        example='pavecli update-collection demo books --display-name "Books"',
    )
    p_update_collection.add_argument("tenant", help="Tenant namespace")
    p_update_collection.add_argument("collection", help="Collection name")
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
        example="pavecli delete-document demo books book-1",
    )
    p_delete_doc.add_argument("tenant", help="Tenant namespace")
    p_delete_doc.add_argument("collection", help="Collection name")
    p_delete_doc.add_argument("docid", help="Document ID")
    p_delete_doc.set_defaults(func=cmd_delete_document)

    p_dump = _command(
        sub,
        "dump-archive",
        help="Export the instance data archive.",
        description=(
            "Write a zip archive containing the current instance data "
            "directory."
        ),
        example="pavecli dump-archive --output pavedb-data.zip",
    )
    p_dump.add_argument("--output", help="Destination ZIP file path")
    p_dump.set_defaults(func=cmd_dump_archive)

    p_restore = _command(
        sub,
        "restore-archive",
        help="Restore an instance data archive.",
        description="Replace the instance data directory from a zip archive.",
        example="pavecli restore-archive pavedb-data.zip",
    )
    p_restore.add_argument("file", help="Archive zip file path")
    p_restore.set_defaults(func=cmd_restore_archive)

    p_reset_metrics = _command(
        sub,
        "reset-metrics",
        help="Reset persisted metrics.",
        description=(
            "Clear the local metrics counters for the configured data "
            "directory."
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
    p_list_collections.add_argument("tenant", help="Tenant namespace")
    p_list_collections.set_defaults(func=cmd_list_collections)

    p_get_collection = _command(
        sub,
        "get-collection",
        help="Fetch collection details.",
        description=(
            "Print catalog, embedder, vector-space, document, and chunk "
            "details for a collection."
        ),
        example="pavecli get-collection demo books",
    )
    p_get_collection.add_argument("tenant", help="Tenant namespace")
    p_get_collection.add_argument("collection", help="Collection name")
    p_get_collection.set_defaults(func=cmd_get_collection)

    return p


def main_cli(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if args.cmd != "init":
        _prepare_runtime(args)
    try:
        out = args.func(args)
    except ServiceError as exc:
        out = {"ok": False, "code": exc.code, "error": exc.message}
    if isinstance(out, RawCLIOutput):
        sys.stdout.buffer.write(out.content)
        return 0
    _dump(out, pretty=not args.compact)
    return _exit_code_for_result(out)

if __name__ == "__main__":
    raise SystemExit(main_cli())
