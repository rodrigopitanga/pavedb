# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import inspect

from pydantic import BaseModel

import pave.schemas as schemas
from pave.main import VERSION

# Implementation terms that must not leak into user-facing API docs.
# See the audience-voice principle in docs/design/PLAN-DOCS.md.
BANNED_DOC_TERMS = (
    "catalog probe",
    "pushdown",
    "hydrate",
    "warmup",
    "executor",
    "subprocess",
    "sqlite",
    "vector store",
    "vector backend",
)


def _our_schema_names():
    names = set()
    for _, obj in inspect.getmembers(schemas, inspect.isclass):
        if issubclass(obj, BaseModel) and obj.__module__ == schemas.__name__:
            names.add(obj.__name__)
    return names


def _assert_no_banned(text, where):
    blob = text.lower()
    for term in BANNED_DOC_TERMS:
        assert term not in blob, (where, term)

def test_ui_renders_instance_strings(client, app, cfg):
    cfg.set("instance.name", "PV-a-name")
    cfg.set("instance.desc", "PV-a-desc")

    r = client.get("/ui")
    assert r.status_code == 200
    assert "PV-a-name" in r.text
    assert "PV-a-desc" in r.text

    # runtime change should reflect on UI
    cfg.set("instance.name", "PV-Changed")
    cfg.set("instance.desc", "D-Changed")
    r2 = client.get("/ui")
    assert "PV-Changed" in r2.text
    assert "D-Changed" in r2.text

def test_openapi_split_and_security(client, app):
    r = client.get("/openapi-search.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["info"]["title"] == app.title
    assert doc["info"]["version"] == VERSION
    assert "bearerAuth" in doc.get("components", {}).get(
        "securitySchemes", {}
    )
    assert doc.get("security") == [{"bearerAuth": []}]

def test_openapi_split_covers_all_v1_paths(client):
    full = client.get("/openapi.json")
    search = client.get("/openapi-search.json")
    data = client.get("/openapi-data.json")
    admin = client.get("/openapi-admin.json")

    assert full.status_code == 200
    assert search.status_code == 200
    assert data.status_code == 200
    assert admin.status_code == 200

    full_paths = full.json().get("paths", {})
    search_paths = set(search.json().get("paths", {}))
    data_paths = set(data.json().get("paths", {}))
    admin_paths = set(admin.json().get("paths", {}))
    search_tags = [tag["name"] for tag in search.json().get("tags", [])]
    search_desc = {
        tag["name"]: tag.get("description", "")
        for tag in search.json().get("tags", [])
    }
    data_tags = [tag["name"] for tag in data.json().get("tags", [])]
    data_desc = {
        tag["name"]: tag.get("description", "")
        for tag in data.json().get("tags", [])
    }
    admin_tags = [tag["name"] for tag in admin.json().get("tags", [])]
    admin_desc = {
        tag["name"]: tag.get("description", "")
        for tag in admin.json().get("tags", [])
    }
    v1_paths = {
        path for path in full_paths
        if path.startswith("/v1/")
    }

    assert search_paths | data_paths | admin_paths == v1_paths
    assert search_paths.isdisjoint(data_paths)
    assert search_paths.isdisjoint(admin_paths)
    assert data_paths.isdisjoint(admin_paths)
    assert "/v1/search" in search_paths
    assert "/v1/admin/archive" in admin_paths
    assert (
        "/v1/admin/collections/{tenant}/{name}/archive" in admin_paths
    )
    assert "/v1/admin/tenants" in admin_paths
    assert "/v1/admin/queries/{query_id}" in admin_paths
    assert (
        "/v1/collections/{tenant}/{name}/queries/{query_id}/replay"
        in admin_paths
    )
    assert "/v1/collections/{tenant}/{collection}/chunks/{rid}" in data_paths
    assert search_tags == ["Scoped Search", "Global Search"]
    assert search_desc["Scoped Search"] == (
        "Search within a specific tenant and collection."
    )
    assert search_desc["Global Search"] == (
        "Search across shared and cross-collection scopes."
    )
    assert data_tags == [
        "Documents",
        "Chunk Inspection",
        "Collection Portability",
        "Collection Catalog",
    ]
    assert data_desc["Documents"] == (
        "Add, review, and remove source documents."
    )
    assert data_desc["Chunk Inspection"] == (
        "Inspect extracted chunks and their stored content."
    )
    assert data_desc["Collection Portability"] == (
        "Archive, restore, and reindex your collections."
    )
    assert data_desc["Collection Catalog"] == (
        "Create, review, update, and retire collections."
    )
    assert admin_tags == [
        "Query Inspection",
        "Instance Admin",
        "Query Admin",
    ]
    assert admin_desc["Query Inspection"] == (
        "Review query history and rerun saved queries."
    )
    assert admin_desc["Instance Admin"] == (
        "Manage archives, tenants, and metrics."
    )
    assert admin_desc["Query Admin"] == "Open or rerun any query by ID."
    admin_doc = admin.json()
    assert (
        admin_doc["paths"]["/v1/admin/metrics"]["delete"]["summary"]
        == "Reset metrics"
    )

def test_data_openapi_orders_primary_flows(client):
    data = client.get("/openapi-data.json")
    assert data.status_code == 200

    ops = []
    for path, methods in data.json().get("paths", {}).items():
        for method in methods:
            ops.append((method, path))

    assert ops[:4] == [
        ("post", "/v1/collections/{tenant}/{collection}/documents"),
        ("get", "/v1/collections/{tenant}/{collection}/documents"),
        ("get", "/v1/collections/{tenant}/{collection}/documents/{docid}"),
        ("delete", "/v1/collections/{tenant}/{collection}/documents/{docid}"),
    ]
    assert ops[-1] == ("delete", "/v1/collections/{tenant}/{name}")

def test_ui_home_has_search_data_admin_tabs(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert 'data-target="search"' in r.text
    assert 'data-target="data"' in r.text
    assert 'data-target="admin"' in r.text

def test_ui_home_persists_tab_state(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert "pavedb.ui.tab" in r.text
    assert "searchParams.set('tab', name)" in r.text
    assert "window.localStorage.getItem(TAB_KEY)" in r.text

def test_ui_home_has_no_tab_hint_row(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert 'id="tab-hint"' not in r.text

def test_favicon_status(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200


def test_openapi_every_route_is_documented(client):
    doc = client.get("/openapi.json").json()
    for path, methods in doc.get("paths", {}).items():
        for method, op in methods.items():
            where = (method, path)
            summary = (op.get("summary") or "").strip()
            description = (op.get("description") or "").strip()
            tags = op.get("tags") or []
            assert summary, where
            assert description, where
            assert tags, where
            _assert_no_banned(f"{summary} {description}", where)
            _assert_no_banned(" ".join(tags), where)


def test_openapi_schema_fields_documented_with_examples(client):
    doc = client.get("/openapi.json").json()
    component_schemas = doc.get("components", {}).get("schemas", {})
    ours = _our_schema_names()

    checked = 0
    for name, schema in component_schemas.items():
        if name not in ours:
            continue
        props = schema.get("properties", {})
        assert props, name
        has_example = False
        for prop_name, prop in props.items():
            desc = (prop.get("description") or "").strip()
            assert desc, (name, prop_name)
            _assert_no_banned(desc, (name, prop_name))
            if "examples" in prop or "example" in prop:
                has_example = True
        assert has_example, name
        checked += 1

    assert checked, "no pave schemas found in OpenAPI components"
