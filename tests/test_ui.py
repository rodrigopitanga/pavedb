# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.main import VERSION

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
    data_tags = [tag["name"] for tag in data.json().get("tags", [])]
    admin_tags = [tag["name"] for tag in admin.json().get("tags", [])]
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
    assert "/v1/admin/tenants" in admin_paths
    assert "/v1/admin/queries/{query_id}" in admin_paths
    assert (
        "/v1/collections/{tenant}/{name}/queries/{query_id}/replay"
        in admin_paths
    )
    assert "/v1/collections/{tenant}/{collection}/chunks/{rid}" in data_paths
    assert search_tags == ["Scoped Search", "Global Search"]
    assert data_tags == [
        "Documents",
        "Chunk Inspection",
        "Collection Catalog",
    ]
    assert admin_tags == [
        "Query Inspection",
        "Instance Admin",
        "Query Admin",
    ]
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

def test_ui_home_has_tab_microcopy(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert 'id="tab-hint"' in r.text
    assert "Run scoped and global searches." in r.text
    assert "Ingest documents and inspect chunks and collections." in r.text
    assert "Inspect query history and use instance controls." in r.text
def test_favicon_status(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
