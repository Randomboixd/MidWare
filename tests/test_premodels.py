from __future__ import annotations

import json

from midware import premodels as pm
from test_proxy import StubResponse, auth_header, install_stub


def premodel_row(**overrides) -> dict:
    row = {
        "prompt_mode": "simple",
        "system_prompt": "",
        "preset_json": None,
        "merge_mode": "append",
        "accept_mwvars": False,
        "params_enabled": False,
        "params_json": None,
    }
    row.update(overrides)
    return row


def create_premodel(db, **overrides) -> int:
    route = db.active_route()
    fields = {
        "name": "Autistic GLM",
        "slug": "autistic-glm",
        "description": "A test premodel",
        "route_id": route["id"],
        "model": "xiaomi/mimo",
        "merge_mode": "append",
        "accept_mwvars": True,
        "prompt_mode": "simple",
        "system_prompt": "You are {{persona}}, {{Persona}}, {{PERSONA}}.",
        "preset_json": None,
    }
    fields.update(overrides)
    return db.create_premodel(**fields)


# -- primitives --------------------------------------------------------------


def test_slugify_and_prefix_parsing():
    assert pm.slugify("Autistic GLM!") == "autistic-glm"
    assert pm.slugify("my_preset") == "my-preset"
    assert pm.normalize_slug("", "Nice Name") == "nice-name"

    assert pm.extract_premodel_prefix("<p>-autistic-glm") == ("autistic-glm", "")
    assert pm.extract_premodel_prefix("<p>-autistic-glm[Host]m") == ("autistic-glm", "[Host]m")
    assert pm.extract_premodel_prefix("gpt-4o") == (None, "gpt-4o")


def test_normalize_preset_honors_prompt_order():
    preset = {
        "prompts": [
            {"identifier": "main", "name": "Main", "system_prompt": True, "role": "system", "content": "MAIN"},
            {"identifier": "off", "name": "Off", "role": "user", "content": "OFF"},
            {"identifier": "inj", "name": "Inj", "role": "user", "content": "INJ", "injection_depth": 2},
            {"marker": True, "identifier": "marker", "name": "Marker", "content": ""},
        ],
        "prompt_order": [
            {
                "character_id": 100000,
                "order": [
                    {"identifier": "main", "enabled": True},
                    {"identifier": "off", "enabled": False},
                    {"identifier": "inj", "enabled": True},
                    {"identifier": "marker", "enabled": False},
                ],
            }
        ],
    }
    normalized = pm.normalize_preset(preset)
    enabled = {p["identifier"]: p["enabled"] for p in normalized["prompts"]}
    assert enabled == {"main": True, "off": False, "inj": True, "marker": False}

    without_order = pm.normalize_preset({"prompts": [{"identifier": "m", "content": "x", "marker": True}]})
    assert without_order["prompts"][0]["enabled"] is False


def test_mwvar_variants_and_message_deletion():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "!!!MWVAR!!!\n@speaker = alice \n@mood=happy"},
        {"role": "user", "content": "!!!MWVAR!!!\n@ignored=yes"},
        {"role": "user", "content": "talk"},
    ]
    definitions, cleaned = pm.extract_mwvars(messages)
    assert set(definitions) == {"speaker", "mood"}
    assert [m["content"] for m in cleaned] == ["hello", "talk"]

    row = premodel_row(
        prompt_mode="simple",
        system_prompt="{{speaker}} / {{Speaker}} / {{SPEAKER}} / {{mood}} / {{unknown}}",
        accept_mwvars=True,
    )
    merged = pm.apply_premodel_messages(row, messages)
    assert merged[0]["content"] == "alice / Alice / ALICE / happy / {{unknown}}"


def test_mwvar_ignored_when_not_accepted():
    messages = [{"role": "user", "content": "!!!MWVAR!!!\n@speaker=alice"}]
    row = premodel_row(prompt_mode="simple", system_prompt="{{speaker}}", accept_mwvars=False)
    merged = pm.apply_premodel_messages(row, messages)
    assert merged[-1]["content"].startswith("!!!MWVAR!!!")


def test_mwvar_accepts_system_role_and_plural_marker():
    messages = [
        {"role": "system", "content": "You are talking to {{user}}."},
        {"role": "system", "content": "!!!MWVARS!!!\n@user=Catto "},
        {"role": "user", "content": "Hiiii"},
    ]
    row = premodel_row(
        prompt_mode="simple",
        system_prompt="Rules for {{user}} / {{User}} / {{USER}}.",
        accept_mwvars=True,
    )
    merged = pm.apply_premodel_messages(row, messages)
    texts = [m["content"] for m in merged]
    assert "Rules for Catto / Catto / CATTO." in texts
    assert "You are talking to Catto." in texts
    assert all("MWVAR" not in text for text in texts)
    assert merged[-1] == {"role": "user", "content": "Hiiii"}


def test_fallback_macro_syntax():
    messages = [{"role": "user", "content": "!!!MWVAR!!!\n@name=skibidi"}]
    row = premodel_row(
        prompt_mode="simple",
        system_prompt="@@name@@ / @@Name@@ / @@NAME@@ / {{name}} / @@unknown@@",
        accept_mwvars=True,
    )
    merged = pm.apply_premodel_messages(row, messages)
    assert merged[0]["content"] == "skibidi / Skibidi / SKIBIDI / skibidi / @@unknown@@"


def test_parse_and_apply_sampling_params_only_fill_missing():
    assert pm.parse_params('{"temperature":"0.7","max_tokens":"512","bogus":1}') == {
        "temperature": 0.7,
        "max_tokens": 512,
    }
    assert pm.parse_params(None) == {}

    row = premodel_row(
        params_enabled=True,
        params_json=json.dumps({"temperature": 0.5, "top_p": 0.9, "max_tokens": 256}),
    )
    payload = {"model": "m", "temperature": 0.1}
    pm.apply_sampling_params(payload, row)
    assert payload["temperature"] == 0.1
    assert payload["top_p"] == 0.9
    assert payload["max_tokens"] == 256

    disabled = premodel_row(params_enabled=False, params_json=json.dumps({"temperature": 0.5}))
    untouched = {"model": "m"}
    pm.apply_sampling_params(untouched, disabled)
    assert "temperature" not in untouched


# -- merging -----------------------------------------------------------------


def test_merge_modes():
    caller = [
        {"role": "system", "content": "CALLER"},
        {"role": "user", "content": "u"},
    ]
    preset = [
        {
            "role": "system",
            "content": "PREM",
            "system_prompt": True,
            "enabled": True,
            "injection_position": 0,
            "injection_depth": 0,
            "injection_order": 100,
        }
    ]

    append = pm.merge_messages(caller, preset, "append")
    assert [(m["role"], m["content"]) for m in append] == [("system", "PREM"), ("system", "CALLER"), ("user", "u")]

    premodel = pm.merge_messages(caller, preset, "premodel")
    assert [(m["role"], m["content"]) for m in premodel] == [("system", "PREM"), ("user", "u")]

    caller_wins = pm.merge_messages(caller, preset, "caller")
    assert caller_wins == caller


def test_caller_mode_without_system_falls_back_to_premodel():
    caller = [{"role": "user", "content": "u"}]
    preset = [
        {
            "role": "system",
            "content": "PREM",
            "system_prompt": True,
            "enabled": True,
            "injection_position": 0,
            "injection_depth": 0,
            "injection_order": 100,
        }
    ]
    merged = pm.merge_messages(caller, preset, "caller")
    assert merged[0] == {"role": "system", "content": "PREM"}


def test_injection_depth_positions():
    caller = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}]
    preset = [
        {
            "role": "user",
            "content": "DEEP",
            "system_prompt": False,
            "enabled": True,
            "injection_position": 0,
            "injection_depth": 2,
            "injection_order": 100,
        },
        {
            "role": "user",
            "content": "SHALLOW",
            "system_prompt": False,
            "enabled": True,
            "injection_position": 0,
            "injection_depth": 0,
            "injection_order": 50,
        },
    ]
    merged = pm.merge_messages(caller, preset, "append")
    assert [m["content"] for m in merged] == ["DEEP", "u1", "a1", "SHALLOW"]


# -- proxy integration -------------------------------------------------------


def test_premodel_prefix_routes_and_applies_overlay(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_premodel(db)
    client = app.test_client()
    body = json.dumps({"id": "x", "model": "xiaomi/mimo", "usage": {"total_tokens": 3}}).encode()
    stub = install_stub(app, StubResponse(content=body))

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "<p>-autistic-glm",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "user", "content": "!!!MWVAR!!!\n@persona=Merlin"},
            ],
        },
        headers=auth_header(token),
    )

    assert response.status_code == 200
    upstream = json.loads(stub.sent[0][0]["content"])
    assert upstream["model"] == "xiaomi/mimo"
    assert upstream["messages"][0] == {"role": "system", "content": "You are Merlin, Merlin, MERLIN."}
    assert upstream["messages"][1] == {"role": "user", "content": "hello"}
    assert len(upstream["messages"]) == 2

    row = db.list_requests(limit=1)[0]
    assert row["model"] == "xiaomi/mimo"
    assert row["model_raw"] == "<p>-autistic-glm"


def test_premodel_prefix_can_override_model_and_host(multi_host):
    app, token = multi_host
    db = app.extensions["midware_db"]
    create_premodel(db)
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=json.dumps({"model": "m"}).encode()))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-autistic-glm[NanoGPT]override-model", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    upstream = json.loads(stub.sent[0][0]["content"])
    assert upstream["model"] == "override-model"
    assert stub.sent[0][0]["url"] == "https://nano-gpt.com/subscription/v1/chat/completions"


def test_system_mwvar_message_is_expanded(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_premodel(
        db,
        prompt_mode="simple",
        system_prompt="You are {{user}}.",
        accept_mwvars=True,
    )
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=b'{"model":"m"}'))

    client.post(
        "/v1/chat/completions",
        json={
            "model": "<p>-autistic-glm",
            "messages": [
                {"role": "system", "content": "!!!MWVARS!!!\n@user=Catto "},
                {"role": "user", "content": "Hiiii"},
            ],
        },
        headers=auth_header(token),
    )

    body = json.loads(stub.sent[0][0]["content"])
    assert body["messages"] == [
        {"role": "system", "content": "You are Catto."},
        {"role": "user", "content": "Hiiii"},
    ]


def test_premodel_sampling_params_reach_upstream(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_premodel(
        db,
        params_enabled=True,
        params_json=json.dumps({"temperature": 0.3, "top_p": 0.8}),
    )
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=b'{"model":"m"}'))

    client.post(
        "/v1/chat/completions",
        json={"model": "<p>-autistic-glm", "messages": [], "temperature": 0.9},
        headers=auth_header(token),
    )

    body = json.loads(stub.sent[0][0]["content"])
    assert body["temperature"] == 0.9
    assert body["top_p"] == 0.8


def test_unknown_premodel_is_404(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_premodel(db)
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=b"{}"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-nope", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "unknown_premodel"
    assert stub.sent == []
    assert db.count_requests() == 0


def test_models_catalogue_includes_premodels(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_premodel(db)
    client = app.test_client()

    payload = client.get("/v1/models", headers=auth_header(token)).get_json()
    slugs = [entry["id"] for entry in payload["data"]]
    assert "<p>-autistic-glm" in slugs


# -- admin -------------------------------------------------------------------


def test_premodel_admin_crud(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    route = db.active_route()
    client = app.test_client()

    response = client.post(
        "/admin/premodels/new",
        data={
            "name": "Friendly Writer",
            "slug": "",
            "description": "Be nice",
            "route_id": route["id"],
            "model": "gpt-4o",
            "merge_mode": "caller",
            "prompt_mode": "simple",
            "system_prompt": "You are kind.",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Premodel" in response.data

    created = db.get_premodel_by_slug("friendly-writer")
    assert created is not None
    assert created["model"] == "gpt-4o"
    assert created["merge_mode"] == "caller"

    html = client.get("/admin/premodels").get_data(as_text=True)
    assert "friendly-writer" in html
    assert "Be nice" in html

    client.post(
        f"/admin/premodels/{created['id']}/edit",
        data={
            "name": "Friendly Writer",
            "slug": "friendly-writer",
            "description": "Be kind",
            "route_id": route["id"],
            "model": "gpt-4o-mini",
            "merge_mode": "append",
            "prompt_mode": "simple",
            "system_prompt": "You are kind.",
        },
    )
    updated = db.get_premodel(created["id"])
    assert updated["model"] == "gpt-4o-mini"
    assert updated["description"] == "Be kind"

    client.post("/admin/premodels", data={"action": "delete", "premodel_id": created["id"]})
    assert db.get_premodel(created["id"]) is None


def test_premodel_admin_validates_input(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    route = db.active_route()
    client = app.test_client()

    response = client.post(
        "/admin/premodels/new",
        data={
            "name": "Broken",
            "slug": "broken",
            "route_id": route["id"],
            "model": "",
            "prompt_mode": "simple",
        },
    )
    assert response.status_code == 200
    assert b"model" in response.data.lower()
    assert db.get_premodel_by_slug("broken") is None


def test_premodel_admin_parses_sillytavern_preset(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    route = db.active_route()
    client = app.test_client()

    preset = {
        "prompts": [
            {"identifier": "main", "name": "Main", "system_prompt": True, "role": "system", "content": "MAIN"},
            {"identifier": "off", "name": "Off", "role": "user", "content": "OFF"},
        ],
        "prompt_order": [{"order": [{"identifier": "main", "enabled": True}, {"identifier": "off", "enabled": False}]}],
    }

    client.post(
        "/admin/premodels/new",
        data={
            "name": "Preset",
            "slug": "preset",
            "route_id": route["id"],
            "model": "gpt-4o",
            "prompt_mode": "sillytavern",
            "preset_json": json.dumps(preset),
        },
    )

    created = db.get_premodel_by_slug("preset")
    assert created is not None
    stored = json.loads(created["preset_json"])
    assert [p["identifier"] for p in stored["prompts"]] == ["main", "off"]
    assert stored["prompts"][1]["enabled"] is False


def test_premodel_admin_stores_sampling(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    route = db.active_route()
    client = app.test_client()

    client.post(
        "/admin/premodels/new",
        data={
            "name": "Sampled",
            "slug": "sampled",
            "route_id": route["id"],
            "model": "gpt-4o",
            "prompt_mode": "simple",
            "system_prompt": "hi",
            "params_enabled": "1",
            "temperature": "0.4",
            "top_p": "0.9",
        },
    )

    created = db.get_premodel_by_slug("sampled")
    assert created is not None
    assert created["params_enabled"] == 1
    assert json.loads(created["params_json"]) == {"temperature": 0.4, "top_p": 0.9}


def test_premodel_admin_rejects_bad_param(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    route = db.active_route()
    client = app.test_client()

    response = client.post(
        "/admin/premodels/new",
        data={
            "name": "Bad Params",
            "slug": "bad-params",
            "route_id": route["id"],
            "model": "gpt-4o",
            "prompt_mode": "simple",
            "temperature": "hot",
        },
    )
    assert response.status_code == 200
    assert b"must be a number" in response.data
    assert db.get_premodel_by_slug("bad-params") is None


def test_new_premodel_page_renders(configured):
    app, token = configured
    client = app.test_client()
    html = client.get("/admin/premodels/new").get_data(as_text=True)
    assert "Accept MWVARS" in html
    assert "Sampling overrides" in html
    assert "params-panel" in html
    assert "premodels.js" in html


def test_premodels_table_created_on_legacy_db(tmp_path):
    import sqlite3

    from midware.db import Database

    legacy = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            request_path TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT 'POST',
            model TEXT,
            status_code INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.commit()
    connection.close()

    db = Database(str(legacy))
    db.init_schema()
    tables = {row["name"] for row in db._rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "premodels" in tables


def test_migration_adds_premodel_params_columns(tmp_path):
    import sqlite3

    from midware.db import Database

    legacy = tmp_path / "legacy_premodels.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            request_path TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT 'POST',
            model TEXT,
            status_code INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE premodels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            route_id INTEGER,
            model TEXT NOT NULL DEFAULT '',
            merge_mode TEXT NOT NULL DEFAULT 'append',
            accept_mwvars INTEGER NOT NULL DEFAULT 0,
            prompt_mode TEXT NOT NULL DEFAULT 'simple',
            system_prompt TEXT NOT NULL DEFAULT '',
            preset_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    db = Database(str(legacy))
    db.init_schema()
    columns = {row["name"] for row in db._rows("PRAGMA table_info(premodels)")}
    assert {"params_enabled", "params_json"} <= columns
