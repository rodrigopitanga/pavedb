# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

# pave/ui.py — minimal, crash-proof UI wiring
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from starlette.staticfiles import StaticFiles
from pathlib import Path
from urllib.parse import quote
import copy

from pave.version import (
    CONTACT_EMAIL,
    LICENSE,
    LICENSE_URL,
    PRODUCT,
    PRODUCT_EMOJI,
    PRODUCT_FAVICON,
    PRODUCT_ICON_PNG,
    REPOSITORY_URL,
    WEBSITE_URL,
    product_display_name,
)

_UI_TAGS = {
    "Scoped Search": {
        "name": "Scoped Search",
        "description": "Search within a specific tenant and collection.",
    },
    "Global Search": {
        "name": "Global Search",
        "description": "Search across shared and cross-collection scopes.",
    },
    "Documents": {
        "name": "Documents",
        "description": "Add, review, and remove source documents.",
    },
    "Chunk Inspection": {
        "name": "Chunk Inspection",
        "description": "Inspect extracted chunks and their stored content.",
    },
    "Collection Catalog": {
        "name": "Collection Catalog",
        "description": "Create, review, update, and retire collections.",
    },
    "Query Inspection": {
        "name": "Query Inspection",
        "description": "Review query history and rerun saved queries.",
    },
    "Instance Admin": {
        "name": "Instance Admin",
        "description": "Manage archives, tenants, and metrics.",
    },
    "Query Admin": {
        "name": "Query Admin",
        "description": "Open or rerun any query by ID.",
    },
}

# ultra-simple fallback template (no f-string; plain string -> safe braces)
_FALLBACK_TMPL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" sizes="192x192"
      href="__PRODUCT_ICON_PNG__?v=__VERSION__" />
<link rel="apple-touch-icon" href="__PRODUCT_ICON_PNG__?v=__VERSION__" />
<link rel="icon" href="__PRODUCT_FAVICON__?v=__VERSION__" />
<title>__DISPLAY_NAME__ • Search</title>
</head>
<body>
  <div class="tabs">
    <button class="tab active" data-target="search"
            data-title="__DISPLAY_NAME__ • Search">Search</button>
    <button class="tab" data-target="data"
            data-title="__DISPLAY_NAME__ • Data">Data</button>
    <button class="tab" data-target="admin"
            data-title="__DISPLAY_NAME__ • Admin">Admin</button>
    <div class="desc">__INST_DESC__</div>
  </div>
  <iframe id="search" class="frame active" src="/ui/search"
          title="Search"></iframe>
  <iframe id="data" class="frame" src="/ui/data" title="Data"></iframe>
  <iframe id="admin" class="frame" src="/ui/admin" title="Admin"></iframe>
  <div class="footer">
    <span>__PRODUCT_EMOJI__ __PRODUCT__ v__VERSION__</span>
    · <a href="__DOCS_URL__" target="_blank" rel="noopener">Docs</a>
    · <a href="__REPO_URL__" target="_blank" rel="noopener">Source</a>
    · <a href="__LICENSE_URL__" target="_blank" rel="noopener">__LICENSE__</a>
    · <a href="__CONTACT_URL__">Contact</a>
  </div>
<script>
  const TAB_KEY = '__STORAGE_KEY__';
  const tabs = document.querySelectorAll('.tab');
  const frames = document.querySelectorAll('.frame');
  function tabNames(){
    return Array.from(tabs).map(function(tab){ return tab.dataset.target; });
  }
  function tabFromLocation(){
    const qs = new URLSearchParams(window.location.search);
    const queryTab = qs.get('tab');
    if(queryTab) return queryTab;
    const hash = window.location.hash.replace(/^#\\/?/, '');
    return hash || '';
  }
  function rememberedTab(){
    try { return window.localStorage.getItem(TAB_KEY) || ''; }
    catch (_err) { return ''; }
  }
  function writeTab(name){
    try { window.localStorage.setItem(TAB_KEY, name); }
    catch (_err) {}
    const url = new URL(window.location.href);
    url.searchParams.set('tab', name);
    url.hash = '/' + name;
    window.history.replaceState(null, '', url);
  }
  function activateTab(name, updateUrl){
    if(tabNames().indexOf(name) === -1) name = 'search';
    tabs.forEach(function(t){ t.classList.remove('active'); });
    frames.forEach(function(f){ f.classList.remove('active'); });
    const tab = document.querySelector('.tab[data-target="' + name + '"]');
    const frame = document.getElementById(name);
    if(tab) tab.classList.add('active');
    if(frame) frame.classList.add('active');
    if(tab && tab.dataset.title) document.title = tab.dataset.title;
    if(updateUrl) writeTab(name);
  }
  tabs.forEach(function(tab){
    tab.addEventListener('click', function(){
      activateTab(tab.dataset.target, true);
    });
  });
  const initialTab = tabFromLocation() || rememberedTab() || 'search';
  activateTab(initialTab, false);
</script>
</body></html>
"""


def _display_name(cfg) -> str:
    return product_display_name(cfg.get("instance.name"))


def _storage_key() -> str:
    return f"{PRODUCT.lower()}.ui.tab"


def _contact_url() -> str:
    return f"mailto:{CONTACT_EMAIL}?subject={quote(f'[{PRODUCT}]')}"


def attach_ui(app: FastAPI):
    cfg = app.state.cfg
    version = app.state.version

    # static + favicon (hardcoded path relative to this file)
    # never crash if dir missing.
    assets_dir = (Path(__file__).parent / "assets").resolve()
    app.mount(
        "/assets", \
        StaticFiles(directory=str(assets_dir), check_dir=False), \
        name="assets"
    )

    @app.get(PRODUCT_FAVICON, include_in_schema=False)
    def favicon():
        return FileResponse(
            str((Path(__file__).parent / "assets" / "favicon.ico").resolve()),
            media_type="image/x-icon",
        )

    # openapi (bearer + repo/license)
    _openapi_cache = {"doc": None}
    def _openapi_full():
        if _openapi_cache["doc"] is None:
            schema = get_openapi(
                title=app.title,
                version=version,
                description=app.description,
                routes=app.routes
            )
            comps = schema.setdefault("components", {})\
                          .setdefault("securitySchemes", {})
            comps["bearerAuth"] = {
                "type": "http",
                "scheme": "bearer",  # plain bearer (no JWT - yet)
                "description": "Send Authorization: Bearer <token>",
            }
            schema["security"] = [{"bearerAuth": []}]
            info = schema.setdefault("info", {})
            info["x-repository"] = REPOSITORY_URL
            info["license"] = {"name": LICENSE, "url": LICENSE_URL}
            _openapi_cache["doc"] = schema
        return _openapi_cache["doc"]

    def _filter(schema: dict, pred):
        s = copy.deepcopy(schema)
        used_tags = set()
        selected_ops = []
        for path in list(s.get("paths", {}).keys()):
            methods = s["paths"][path]
            for m in list(methods.keys()):
                if not pred(path, methods[m]):
                    methods.pop(m, None)
                    continue
                op = methods[m]
                _retitle_ui_ops(path, m, op)
                selected_ops.append((path, m, op))
                for tag in op.get("tags", []):
                    used_tags.add(tag)
            if not methods:
                s["paths"].pop(path, None)
        ordered_paths = {}
        for path, method, op in sorted(selected_ops, key=_ui_op_sort_key):
            ordered_paths.setdefault(path, {})
            ordered_paths[path][method] = op
        s["paths"] = ordered_paths
        if used_tags:
            s["tags"] = [
                meta for tag, meta in _UI_TAGS.items()
                if tag in used_tags
            ]
        else:
            s.pop("tags", None)
        return s

    def _retitle_ui_ops(path: str, method: str, op: dict) -> None:
        if path == "/v1/admin/metrics" and method.lower() == "delete":
            op["summary"] = "Reset metrics"

    def _ui_op_sort_key(item):
        path, method, op = item
        tag = op.get("tags", [""])[0]
        tag_rank = list(_UI_TAGS).index(tag) if tag in _UI_TAGS else 999
        local_rank = _ui_op_rank(tag, path, method)
        return (tag_rank, local_rank, path, method)

    def _ui_op_rank(tag: str, path: str, method: str) -> int:
        key = (method.lower(), path)
        ranks = {
            "Documents": {
                ("post", "/v1/collections/{tenant}/{collection}/documents"): 0,
                ("get", "/v1/collections/{tenant}/{collection}/documents"): 1,
                (
                    "get",
                    "/v1/collections/{tenant}/{collection}/documents/{docid}",
                ): 2,
                (
                    "delete",
                    "/v1/collections/{tenant}/{collection}/documents/{docid}",
                ): 3,
            },
            "Chunk Inspection": {
                (
                    "get",
                    "/v1/collections/{tenant}/{collection}/documents/"
                    "{docid}/chunks",
                ): 0,
                ("get", "/v1/collections/{tenant}/{collection}/chunks/{rid}"): 1,
                (
                    "get",
                    "/v1/collections/{tenant}/{collection}/chunks/{rid}/content",
                ): 2,
            },
            "Collection Catalog": {
                ("get", "/v1/collections/{tenant}"): 0,
                ("get", "/v1/embedders/{tenant}"): 1,
                ("get", "/v1/collections/{tenant}/{name}/detail"): 2,
                ("post", "/v1/collections/{tenant}/{name}/move"): 3,
                ("post", "/v1/collections/{tenant}/{name}"): 4,
                ("patch", "/v1/collections/{tenant}/{name}"): 5,
                ("delete", "/v1/collections/{tenant}/{name}"): 6,
            },
            "Instance Admin": {
                ("get", "/v1/admin/archive"): 0,
                ("put", "/v1/admin/archive"): 1,
                ("get", "/v1/admin/tenants"): 2,
                ("post", "/v1/admin/collections/{tenant}/{name}/move"): 3,
                ("delete", "/v1/admin/metrics"): 4,
            },
        }
        return ranks.get(tag, {}).get(key, 100)

    def _is_v1(path: str) -> bool:
        return path.startswith("/v1/")

    def _is_search(path: str, _op: dict) -> bool:
        p = path.lower()
        return _is_v1(path) and "/search" in p

    def _is_admin(path: str, _op: dict) -> bool:
        p = path.lower()
        return _is_v1(path) and ("/admin/" in p or "/queries" in p)

    def _is_data(path: str, _op: dict) -> bool:
        return _is_v1(path) and not _is_search(path, _op) and not _is_admin(
            path, _op
        )

    @app.get("/openapi-search.json", include_in_schema=False)
    def openapi_search_only():
        return _filter(_openapi_full(), _is_search)

    @app.get("/openapi-data.json", include_in_schema=False)
    def openapi_data_only():
        return _filter(_openapi_full(), _is_data)

    @app.get("/openapi-admin.json", include_in_schema=False)
    def openapi_admin_only():
        return _filter(_openapi_full(), _is_admin)

    _swui_params = {
            "defaultModelsExpandDepth": -1,
            "displayRequestDuration": True,
            "docExpansion": "list",
            "tryItOutEnabled": True,
        }

    @app.get("/ui/search", include_in_schema=False)
    def ui_search():
        inst_name = _display_name(cfg)
        return get_swagger_ui_html(
            openapi_url="/openapi-search.json",
            title=f"{inst_name} • Search",
            swagger_ui_parameters=_swui_params
        )

    @app.get("/ui/data", include_in_schema=False)
    def ui_data():
        inst_name = _display_name(cfg)
        return get_swagger_ui_html(
            openapi_url="/openapi-data.json",
            title=f"{inst_name} • Data",
            swagger_ui_parameters=_swui_params
        )

    @app.get("/ui/admin", include_in_schema=False)
    def ui_admin():
        inst_name = _display_name(cfg)
        return get_swagger_ui_html(
            openapi_url="/openapi-admin.json",
            title=f"{inst_name} • Admin",
            swagger_ui_parameters=_swui_params
        )

    # lazy-read template on request (so missing file never kills startup)
    tmpl_path = assets_dir / "ui.html"

    @app.get("/ui", include_in_schema=False)
    def ui_home():
        # instance strings
        display_name = _display_name(cfg)
        inst_desc = cfg.get("instance.desc")
        try:
            html = tmpl_path.read_text(encoding="utf-8")
        except Exception:
            html = _FALLBACK_TMPL
        html = (
            html.replace("__DISPLAY_NAME__", display_name)
                .replace("__PRODUCT__", PRODUCT)
                .replace("__PRODUCT_EMOJI__", PRODUCT_EMOJI)
                .replace("__PRODUCT_ICON_PNG__", PRODUCT_ICON_PNG)
                .replace("__PRODUCT_FAVICON__", PRODUCT_FAVICON)
                .replace("__INST_DESC__", inst_desc)
                .replace("__VERSION__", str(version))
                .replace("__DOCS_URL__", WEBSITE_URL)
                .replace("__REPO_URL__", REPOSITORY_URL)
                .replace("__LICENSE__", LICENSE)
                .replace("__LICENSE_URL__", LICENSE_URL)
                .replace("__CONTACT_URL__", _contact_url())
                .replace("__STORAGE_KEY__", _storage_key())
        )
        return HTMLResponse(html)

    @app.get("/", include_in_schema=False)
    def root_redirect():
        return RedirectResponse("/ui", status_code=308)
