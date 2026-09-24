# AGENTS.md

Guidance for AI coding agents working in this repository. Read this before making
changes; it is the map, not a tutorial.

## What MidWare is

A small, self-hosted LLM proxy. Clients point their OpenAI-compatible base URL at
MidWare (`http://host:5000/v1`) and send a MidWare key. MidWare swaps in the real
upstream key, forwards the request, and records token usage per client key. It also:

- supports multiple upstream hosts at once via a `[Host]model` prefix,
- renders captured request/response bodies as a readable conversation,
- serves a merged, deduplicated model catalogue at `GET /v1/models`.

Design bias: **stay small**. No ORM, no migrations framework, no frontend build step,
no background worker. The Flask dev server is used on purpose (it is `threaded=True`
and the app is not meant to face the public internet directly).

## Commands

Windows / PowerShell. The virtualenv lives at `.venv`; invoke it explicitly:

```powershell
.\.venv\Scripts\python.exe -m pytest -q          # full test suite (this is the lint gate)
.\.venv\Scripts\python.exe -m pytest tests/test_proxy.py::test_name -q
.\.venv\Scripts\python.exe run.py                # dev server, HOST/PORT env vars
.\.venv\Scripts\python.exe wsgi.py               # production-style entrypoint
docker compose up -d --build
```

There is **no** ruff/flake8/black/mypy config in this repo. Tests are the only
automated check; always run the full suite before finishing. `python`, `py`, `rg`,
`node` and `docker` may be absent on a given machine — `docker` is unavailable in
this development environment, so Docker changes are verified by reading only.

## Layout

| Path | Responsibility |
| --- | --- |
| `midware/__init__.py` | `create_app()` factory: config, DB, shared `httpx.Client`, blueprint registration, `before_request` model-refresh hook. |
| `midware/config.py` | Every setting, overridable by a same-named `MIDWARE_*` env var. |
| `midware/db.py` | All persistence. Raw `sqlite3`, hand-written SQL, per-thread connections, schema + `_migrate()`. |
| `midware/auth.py` | Client token extraction (`Authorization: Bearer` / `x-api-key`) + `authenticate()`; `get_db()` helper. |
| `midware/adminauth.py` | HTTP Basic login for the human-facing pages: salted hashing, the admin guard/`admin_required`, `messages_locked()`, env-var override. |
| `midware/usage.py` | Parse token usage from JSON (`usage_from_response`) and SSE (`usage_from_stream`/`parse_sse_events`); `truncate`, `strip_ansi`, `model_from_request_body`. |
| `midware/conversation.py` | Normalize request/response bodies (JSON or streamed) into display blocks for the viewer. |
| `midware/models.py` | Fetch/dedupe/refresh upstream model catalogues; `probe_route` for connection tests. |
| `midware/premodels.py` | Premodel overlays: slugging, SillyTavern preset normalization, MWVAR macros, message merging. |
| `midware/tokens.py` | Local token estimation fallback (tiktoken or char heuristic) when upstream sends no `usage`. |
| `midware/msq.py` | Model Service Quality: probe scoring, symbol/slop detection, probe runner, in-process scheduler, Working/Failing/Degraded evaluation. |
| `midware/msq_checks.py` | MSQ hardcore checks: incremental SSE audit, text forensics (repetition/glitch/template), long-context needle builder + grading. |
| `midware/jinja.py` | Template filters (`tokens`, `comma`, `datetime`, `relative`, `ms`, `preview`) + `app_version`. |
| `midware/blueprints/proxy.py` | The proxy surface: `/v1/*`, `/proxy/*`; auth, routing, forwarding, teeing, persisting. |
| `midware/blueprints/admin.py` | Write surfaces: `/admin/setup`, `/admin/routes`, `/admin/keys` (+ `new`/`edit`), `/admin/settings`. |
| `midware/blueprints/msq.py` | MSQ control panel: `/admin/msq` list + `new`/`edit`/`delete`/`run`. |
| `midware/blueprints/dashboard.py` | Read-only UI: `/`, `/activity`, `/requests`, request detail + `messages.json`. |
| `midware/templates/`, `midware/static/` | Jinja2 templates (PicoCSS v2 from CDN) and `style.css` / `request_detail.js`. |
| `tests/` | pytest suite. See "Testing" below before adding tests. |
| `run.py`, `wsgi.py` | Entrypoints; both read `HOST`/`PORT`, `wsgi.py` is used in the container. |
| `Dockerfile`, `docker-compose.yml` | Container build; DB lives in the `/data` volume. |

## Request lifecycle (`proxy.py`)

1. `authenticate()` resolves the MidWare key and a fallback active route, or returns
   a `(payload, status)` error to return verbatim.
2. `GET /v1/models` is intercepted here and served from the DB (`_models_catalogue`),
   **not** proxied.
3. Only `POST` is forwarded; anything else is `405`.
4. `_forward()` parses the body, extracts an optional `[Host]` prefix from `model`,
   resolves the route, strips the prefix, and rebuilds the upstream URL.
5. The response is streamed back while being tee'd into a bytearray capped at
   `MAX_CAPTURE_BYTES` (8 MiB default).
6. `_persist()` writes the request row and prunes the per-key log to
   `request_log_limit`. For streams this runs inside the generator once the stream
   ends, so bookkeeping errors must never escape into the response.

## Data model (`db.py`)

- `api_keys(id, name, description, token UNIQUE, cors_allow_origin,
  allow_premodels, restrict_premodels, allowed_premodel_ids, restrict_providers,
  allowed_route_ids, restrict_models, allowed_model_ids, is_system, created_at,
  last_used_at)` — `is_system` rows (the auto-created `MSQ Measurements` owner) are
  skipped by `list_api_keys`/`count_api_keys` but still name rows in the request log.
  `allowed_premodel_ids`/`allowed_route_ids` hold a JSON array of integer ids
  (`db.dump_id_list` / `db.parse_id_list`); `allowed_model_ids` holds a JSON array of
  `"<route_id>:<model_id>"` strings (`db.dump_model_list` / `db.parse_model_list`),
  because a model id alone is not unique across providers.
- `routes(id, name, target_host, upstream_key, api_key_id, is_active, created_at)`
- `requests(id, created_at, request_path, method, model, model_raw, host_prefix,
  status_code, latency_ms, streamed, prompt_tokens, completion_tokens, total_tokens,
  token_source, upstream_request_id, error, request_body, response_body, route_id,
  api_key_id)`
- `settings(key, value)` — live-editable values (e.g. `request_log_limit`).
- `meta(key, value)` — bookkeeping (`configured_at`, `models_refreshed_at`,
  `models_refresh_error`) and the admin login (`admin_username`,
  `admin_password_hash` — a salted scrypt hash — and the one-time `admin_setup_code`,
  see `adminauth.py`).
- `models(id, route_id FK CASCADE, model_id, fetched_at, UNIQUE(route_id, model_id))`
- `msq_recorders(id, name, route_id FK SET NULL, model, system_prompt, user_prompt,
  interval_seconds, enabled, penalize_symbols, slop_list, max_ttft_ms, max_total_ms,
  hardcore_json, collect_requests, ignore_thinking, created_at, updated_at, last_run_at,
  next_run_at)` — one scheduled probe of a model. `max_ttft_ms`/`max_total_ms` are `-1`
  to disable a budget; `hardcore_json` is `{"stream_integrity", "text_forensics",
  "needle", "needle_context_chars", "collect_needle"}`, each check absent/off by
  default; `collect_requests` files each probe as a normal request row under the hidden
  `MSQ Measurements` system key, and `collect_needle` files the needle probe too (only
  when request recording is on); `ignore_thinking` (default on) scores only the visible
  answer, so reasoning models are not penalised for drafting themselves in their think
  block.
- `msq_checks(id, recorder_id FK CASCADE, created_at, status_code, ok, ttft_ms,
  total_ms, generation_ms, completion_tokens, tokens_per_second, score, hard_fail,
  penalties JSON, response_excerpt, error)` — one probe result; `penalties` is a JSON
  list of `{"reason", "points", "kind", "passed"}` (the checked-to-be-clean cases are
  stored with `passed: true`, `points: 0`) that the chart tooltip reads back verbatim.

`init_schema()` calls `_migrate()` **before** `executescript(SCHEMA)` because
`CREATE TABLE IF NOT EXISTS` will not reshape an old `requests` table. To add a
column, append it to the `added` dict in `_migrate()` (with a `DEFAULT` if the column
is `NOT NULL`) and to the `SCHEMA` string, then update `record_request`.

## Key behaviours and invariants

Do not break these; several have regression tests.

- **Prefix routing** (`_resolve_target`): explicit `[#id]` wins; then the active
  route's name; then any route's name; unknown name is `404 unknown_host` (never
  silently misrouted). Names are matched case-insensitively and whitespace-collapsed.
  `[Name]` routes to an inactive route fine; it just does not change which route is
  the default.
- **`target_host` `/v1` handling** (`_split_target`): a configured host ending in
  `/v1` is not double-prefixed when building the upstream URL.
- **API key permissions** (`proxy._key_limits`): each key carries its own CORS
  allowlist, an `allow_premodels` flag, and optional premodel/provider/model
  allowlists (route and premodel ids, `"<route_id>:<model_id>"` keys). The final
  route and upstream model are checked **after** `[Host]` / `<p>-` routing, so none
  of them can bypass a provider or model limit. A denied premodel, provider, model
  or browser origin is a hard `403` (`premodel_not_allowed` /
  `provider_not_allowed` / `model_not_allowed` / `origin_not_allowed`), never a
  silent fallback. `GET /v1/models` is filtered to the key's allowed
  providers/premodels/models. Per-key CORS lives in `cors.py` (admin and dashboard
  responses send none); an `OPTIONS` preflight cannot identify the key, so it is
  answered permissively and the *actual* request is rejected when its `Origin` is
  not allowed. A request with no `Origin` (non-browser) is always allowed.
- **Token accounting** (`_persist`): prefer real upstream `usage`; if it is missing or
  all-zero on a non-error response, fall back to `tokens.estimate_usage` and record
  `token_source` (`upstream` / `tiktoken` / `chars` / `none`). Error responses are
  **never** given estimated tokens. `token_source` is surfaced in the request UI.
- **SSE parsing** (`parse_sse_events`): events end at blank lines. Captured bodies
  sometimes lose separators, so a `data:` line inside an event only starts a new
  event when the previous payload is complete JSON, or the previous payload was not a
  valid JSON prefix completed by the new line. Reasoning mirrored across
  `reasoning`/`reasoning_details`/`reasoning_content` is de-duplicated in
  `conversation._from_sse` — do not reintroduce naive concatenation.
- **Model catalogue** (`models.py`): routes sharing a host (ignoring case, trailing
  slash, `/v1`) are fetched once per refresh via `normalize_host`; results are applied
  to every route in the group. Slugs are computed at **read** time (`db.list_models`):
  bare for the default route, `[Name]model` for everyone else, so changing the default
  re-slugs instantly. Default-route detection uses `routes.is_active`.
- **Refresh is background-only.** `refresh_all` does network I/O and must never run
  synchronously on the request path (this was a real hang bug). Admin actions spawn
  `refresh_in_background`; a stale catalogue is refreshed via `refresh_if_stale` in a
  `before_request` hook (skipped when `TESTING`), and a first-proxied-request /
  upstream-401 path also triggers one if the catalogue is empty.
- **Connection test** (`probe_route` + `admin._connection_test`): on by default when
  adding a route or during setup. A failed probe means the route is **not saved**.
  A host that answers with non-JSON is treated as reachable (accepted + warning).
  The edit drawer tests only when its checkbox is ticked. Unchecking the
  create/setup box skips the probe **and** the `/v1/models` refresh; those forms
  carry a hidden `test_connection=0` because an unchecked checkbox submits nothing.
- **Premodels** (`premodels.py`): a premodel bundles a route, a model and a prompt
  preset, addressed as `<p>-slug` in `model`. The prefix is parsed before
  `[Host]`, so `<p>-slug[Host]model` lets the host win while the preset still
  applies; trailing text after the slug overrides the premodel's model. Prompt
  merge modes are `append` (default: premodel system prompt then caller's),
  `premodel` (drop caller system prompts) and `caller` (ignore the preset when the
  caller sent a system prompt). MWVAR messages (`!!!MWVAR!!!` + `@name=value`)
  are read only when `accept_mwvars` is on; the marker is `!!!MWVAR!!!` or
  `!!!MWVARS!!!` and any role may carry it (clients often use a system message).
  The defining message and every later MWVAR message are removed before
  substitution, and each definition also exposes capitalized/uppercased spellings
  of the value. Macros expand in both the `{{ name }}` and `@@name@@` forms. When
  `params_enabled` is set, `temperature`,
  `top_p` and `max_tokens` fill in any value the caller omitted. Premodels are
  merged into `GET /v1/models` as `<p>-slug`. Admin CRUD lives at
  `/admin/premodels`.
- **MSQ** (`msq.py`): a recorder periodically streams a fixed prompt to its route's
  model, measuring time-to-first-token (the incremental `StreamAudit` in
  `msq_checks.py`), total time, tokens/s, then scores 0-100 (`score_check`): 100 minus
  penalties for over-budget TTFT (max 40) and total time (max 30), non-Latin symbols
  (max 25) and configured slop words (max 25). A failed/empty check is an automatic 0.
  Scoring looks at the visible answer, rebuilt by `split_response_text`; with
  `ignore_thinking` off the reasoning stream is appended, so drafting is only scored
  when explicitly requested.
  Opt-in **hardcore checks** (per-recorder, all off by default) add `stream_integrity`
  (malformed chunks, a missing completion marker, provider error events, inter-token
  stalls), `text_forensics` (repetition loops, glitch/mojibake, leaked chat-template
  tokens) and `needle` (a code hidden in filler context). A hard failure sets
  `hard_fail` and the recorder reports `failing` immediately, before the examination
  window. `evaluate_recorder` otherwise reports `examining` until the recorder is
  `MSQ_EXAMINE_HOURS` old and has `MSQ_MIN_CHECKS` samples, then `degraded` when the
  window average is below `MSQ_DEGRADED_THRESHOLD`, else `working`. A single daemon
  thread (`start_scheduler`, skipped under `TESTING`) wakes every
  `MSQ_SCHEDULER_INTERVAL` and runs due recorders; `claim_msq_recorder` pushes
  `next_run_at` forward atomically so a run is never duplicated. With
  `collect_requests` the probe is also filed via `_maybe_log_probe` as a normal
  request row under a lazily created, hidden `MSQ Measurements` system key
  (`get_or_create_api_key`), so it shows in history and token totals; that logging is
  best-effort and must never break a probe. The panel at
  `/admin/msq` is admin-guarded and renders one card + hand-rolled SVG chart per
  recorder (`static/msq.js`), with hover tooltips reading the stored findings.
- **Conversation viewer**: table for request metadata excluding messages; messages are
  numbered, collapsible, and only the last user + last assistant start open. The
  client-side **Fetch** button re-requests `/requests/<id>/messages.json`.
- **Admin login** (`adminauth.py`): HTTP Basic on every `/admin/*` route and on the
  request-message surfaces (`/requests/<id>?messages=1`, `messages.json`). The
  dashboard (`/`, `/activity`, `/requests`, and a detail page's metadata) stays
  public; only message bodies and raw payloads are hidden (`messages_locked()`).
  With no credentials configured, all admin pages redirect to `/admin/setup`, which
  is the only open page and where the login is chosen (fresh install *and*
  migration). Claiming the instance requires the 32-char `admin_setup_code`: a
  startup banner prints it to the server console, and only someone who can read the
  logs (i.e. controls the host) will pass. Setup can only ever *create* the login:
  once credentials exist it never renders or accepts the account form, and with a
  route present it redirects to `/admin/routes`. Changing the login is exclusively
  `/admin/settings`, which demands the current password (this closes a cached-Basic
  / CSRF account-takeover hole). Passwords are salted scrypt hashes in `meta`.
  `MIDWARE_ADMIN_USERNAME` /
  `MIDWARE_ADMIN_PASSWORD` override the database (headless provisioning + lockout
  recovery) and skip the claim step. The setup and settings login forms also show a
  client-side warning when the page is served over plain HTTP. The proxy surface is
  unaffected and never emits a `WWW-Authenticate` header. The detail route returns a
  `401` + `WWW-Authenticate` on `?messages=1` so the browser prompts, falling back to
  the locked page with a note when the prompt is cancelled.

## Testing

- Fixtures in `tests/conftest.py`: `app`, `client`, `db`, `configured`,
  `configured_client`, `multi_host`, plus `secured_app`/`secured_client` for the
  login. `app` installs a `_ProbeClient` so admin connection tests never hit the
  network, and sets `ADMIN_AUTH_ENABLED=False` so unrelated tests skip the login;
  `secured_app` is the same app with auth on (`tests/test_admin_auth.py`).
- Proxy tests use the hand-rolled `StubClient`/`StubResponse` and `install_stub()`
  from `tests/test_proxy.py`; import them from there in other test modules.
- Streaming tests must consume the response body, e.g.
  `b"".join(response.response)`, otherwise the generator never runs and no request
  row is written (a confusing `IndexError`).
- Tests run against `:memory:` SQLite with `TESTING=True`, which disables the
  model-refresh hooks and the MSQ scheduler. Drive probes directly with
  `msq.run_recorder` / `msq.run_due` and a stubbed HTTP client.
- Keep the suite green and add a test for every behaviour change. Current baseline:
  `190 passed` — **update this number whenever the test count changes.**

## Conventions

- Type hints and `from __future__ import annotations` in modules; docstrings explain
  *why*, not *what*.
- Snake_case functions, small helpers prefixed `_` for module-private.
- No comments that restate code. No emojis.
- Errors returned to clients follow the OpenAI error shape
  (`{"error": {"message", "type", "code"}}`).
- Never log or store upstream secrets anywhere except the `routes.upstream_key`
  column; client keys have an `mw-` prefix.
