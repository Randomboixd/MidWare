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
| `midware/usage.py` | Parse token usage from JSON (`usage_from_response`) and SSE (`usage_from_stream`/`parse_sse_events`); multipart text fields (`parse_multipart_fields`); `truncate`, `strip_ansi`, `model_from_request_body`. |
| `midware/conversation.py` | Normalize request/response bodies (JSON, multipart or streamed) into display blocks for the viewer, including image/embedding `data` arrays. |
| `midware/models.py` | Fetch/dedupe/refresh upstream model catalogues; `probe_route` for connection tests. |
| `midware/signin.py` | Sign-in-with-service flows for the route form: Nano-GPT device login (pay-as-you-go + subscription presets) and OpenRouter OAuth PKCE. |
| `midware/balance.py` | Provider balance lookups for sign-in-tagged routes (NanoGPT check-balance, OpenRouter credits/key), with a short in-memory cache. |
| `midware/premodels.py` | Premodel overlays: slugging, configurable prefix parsing (`prefix_regex`/`normalize_prefix`), SillyTavern preset normalization, MWVAR macros, message merging. |
| `midware/workflow.py` | Premodel workflows: `normalize_workflow` validation, `WorkflowScope` variable expansion, `run_workflow` sub-call chain (Origin → API Request nodes → Final Request). |
| `midware/upstream.py` | Shared upstream wire helpers: `HOP_BY_HOP`, `split_target`, `upstream_url` (used by proxy + workflow; keeps `workflow.py` out of an import cycle with the blueprint). |
| `midware/tokens.py` | Local token estimation fallback (tiktoken or char heuristic) when upstream sends no `usage`; `prompt_estimate` for prompt-only counting. |
| `midware/images.py` | Server-side image storage for URL-mode keys: magic-byte sniffing, `store_image`/`load_image`/`sweep`, and the `b64_json` → `/img/` URL response rewrite. |
| `midware/msq.py` | Model Service Quality: probe scoring, symbol/slop detection, probe runner, in-process scheduler, Working/Failing/Degraded evaluation. |
| `midware/msq_checks.py` | MSQ hardcore checks: incremental SSE audit, text forensics (repetition/glitch/template), long-context needle builder + grading. |
| `midware/jinja.py` | Template filters (`tokens`, `comma`, `datetime`, `relative`, `ms`, `preview`) + `app_version`. |
| `midware/blueprints/proxy.py` | The proxy surface: `/v1/*`, `/proxy/*`; auth, routing, forwarding, teeing, persisting. |
| `midware/blueprints/admin.py` | Write surfaces: `/admin/setup`, `/admin/routes`, `/admin/keys` (+ `new`/`edit`), `/admin/settings`. |
| `midware/blueprints/msq.py` | MSQ control panel: `/admin/msq` list + `new`/`edit`/`delete`/`run`. |
| `midware/blueprints/dashboard.py` | Read-only UI: `/`, `/activity`, `/requests`, request detail + `messages.json`. |
| `midware/templates/`, `midware/static/` | Jinja2 templates (PicoCSS v2 from CDN) and `style.css` / `request_detail.js` / `workflow.js`. |
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
   resolves the route, strips the prefix, and rebuilds the upstream URL. Premodel
   resolution happens first (`<p>-slug` → route/model rewrite, or in workflow mode
   the chain runs **after** the rate-limit claims and before the final forward).
   A `multipart/form-data` body (image edits) is read once via `get_data()` and
   routed through a stand-in `{"model": …}` dict built from the form field; it is
   forwarded byte-for-byte unless routing rewrote the model, in which case it is
   re-serialized with a fresh boundary (`_rebuild_multipart`).
5. The response is streamed back while being tee'd into a bytearray capped at
   `MAX_CAPTURE_BYTES` (8 MiB default).
6. `_persist()` writes the request row and prunes the per-key log to
   `request_log_limit`. For streams this runs inside the generator once the stream
   ends, so bookkeeping errors must never escape into the response.

## Data model (`db.py`)

- `api_keys(id, name, description, token UNIQUE, cors_allow_origin,
  allow_premodels, restrict_premodels, allowed_premodel_ids, restrict_providers,
  allowed_route_ids, restrict_models, allowed_model_ids, is_system,
  limit_tokens_enabled, limit_tokens_{hour,day,week}, max_tokens_{hour,day,week},
  limit_requests_enabled, limit_requests_{minute,hour,day,week},
  max_requests_{minute,hour,day,week}, image_response_mode, image_retention_days,
  image_url_https, log_requests, log_message_contents, created_at, last_used_at)` —
  `is_system`
  rows (the auto-created `MSQ Measurements` owner) are skipped by
  `list_api_keys`/`count_api_keys` but still name rows in the request log.
  `allowed_premodel_ids`/`allowed_route_ids` hold a JSON array of integer ids
  (`db.dump_id_list` / `db.parse_id_list`); `allowed_model_ids` holds a JSON array of
  `"<route_id>:<model_id>"` strings (`db.dump_model_list` / `db.parse_model_list`),
  because a model id alone is not unique across providers. The rate-limit columns
  are all `INTEGER NOT NULL DEFAULT 0`: `limit_*_enabled` is the master switch,
  `limit_*_<window>` ticks a child window, and `max_*_<window>` is its cap (`0` =
  unlimited for that window). `image_response_mode` is `b64` (default, inline
  base64 passthrough) or `url` (server-stored, see `images.py`);
  `image_retention_days` is `0` = keep forever; `image_url_https` advertises
  stored image URLs as `https://` (TLS terminating in front of MidWare).
  `log_requests`/`log_message_contents` (`INTEGER NOT NULL DEFAULT 1`) are the
  per-key logging switches (see "Per-key logging" below).
- `routes(id, name, target_host, upstream_key, api_key_id, is_active, provider, created_at)`
  — `provider` is the sign-in tag (`nanogpt` / `nanogpt-sub` / `openrouter`,
  `''` when hand-configured); it is what later proves which service's balance
  API a route may be queried against.
- `requests(id, created_at, request_path, method, model, model_raw, host_prefix,
  status_code, latency_ms, streamed, prompt_tokens, completion_tokens, total_tokens,
  token_source, upstream_request_id, error, request_body, response_body, route_id,
  api_key_id)`
- `settings(key, value)` — live-editable values (`request_log_limit`,
  `premodel_prefix`; each falls back to its `DEFAULT_*` config constant).
- `meta(key, value)` — bookkeeping (`configured_at`, `models_refreshed_at`,
  `models_refresh_error`) and the admin login (`admin_username`,
  `admin_password_hash` — a salted scrypt hash — and the one-time `admin_setup_code`,
  see `adminauth.py`).
- `models(id, route_id FK CASCADE, model_id, fetched_at, UNIQUE(route_id, model_id))`
- `premodels(id, name, slug UNIQUE, description, route_id FK SET NULL, model,
  merge_mode, accept_mwvars, prompt_mode, system_prompt, preset_json, params_enabled,
  params_json, use_workflow, workflow_json, *LIMIT_FLAG_FIELDS, *LIMIT_VALUE_FIELDS,
  created_at, updated_at)` — the limit columns mirror `api_keys` (same names/semantics)
  and back the per-premodel rate limits. `use_workflow` switches the premodel from
  prompt-overlay mode to a workflow chain; `workflow_json` holds the canonical
  `{"version": 1, "final": {"messages": [...], "model_override": ""}, "nodes": [...]}`
  graph, validated by `workflow.normalize_workflow` at save time and re-validated
  defensively by `load_workflow` at request time.
- `premodel_rate_usage(premodel_id FK CASCADE, kind, window, window_start, amount,
  PK(premodel_id, kind, window, window_start))` — per-premodel rate-limit counters,
  a twin of `rate_usage`; both are driven by the shared `_rate_usage_*` helpers in
  `db.py` (keyed by `Database._RATE_TABLES`).
- `stored_images(id PK, api_key_id FK CASCADE, ext, created_at, expires_at)` —
  one server-stored image for a URL-mode key. `expires_at` `NULL` = keep forever
  (`image_retention_days` `0`); files live in `IMAGES_DIR` (defaults next to the
  database file) as `<id>.<ext>` and are removed lazily — on serve when expired,
  on key delete (`delete_api_key` returns the rows, the admin route unlinks), and
  by `images.sweep()` after every store.
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
  block. The panel chart draws only the newest `MSQ_CHART_LIMIT` (10) checks, while
  evaluation still reads the full `MSQ_HISTORY_LIMIT` window.
- `msq_checks(id, recorder_id FK CASCADE, created_at, status_code, ok, ttft_ms,
  total_ms, generation_ms, completion_tokens, tokens_per_second, score, hard_fail,
  penalties JSON, response_excerpt, error)` — one probe result; `penalties` is a JSON
  list of `{"reason", "points", "kind", "passed"}` (the checked-to-be-clean cases are
  stored with `passed: true`, `points: 0`) that the chart tooltip reads back verbatim.
- `usage_daily(day, api_key_id, route_id, requests, prompt_tokens, completion_tokens,
  total_tokens, latency_ms_total, PK(day, api_key_id, route_id))` — the permanent
  usage ledger, upserted inside `record_request`. `api_key_id`/`route_id` use `-1`
  for "none" because SQLite treats `NULL`s as distinct in a PRIMARY KEY. Pruning the
  `requests` log never touches it, so totals only ever grow.
- `rate_usage(api_key_id FK CASCADE, kind, window, window_start, amount,
  PK(api_key_id, kind, window, window_start))` — fixed-window rate-limit counters.
  `kind` is `requests` or `tokens`; `window` is `minute`/`hour`/`day`/`week` floored
  to UTC (`rate_window_start`, week = Monday); `amount` is the consumption in the
  current window only. Rows older than 14 days are pruned on write.

`init_schema()` calls `_migrate()` **before** `executescript(SCHEMA)` because
`CREATE TABLE IF NOT EXISTS` will not reshape an old `requests` table. To add a
column, append it to the `added` dict in `_migrate()` (with a `DEFAULT` if the column
is `NOT NULL`) and to the `SCHEMA` string, then update `record_request`. Afterwards
`_backfill_usage_daily()` seeds the ledger from whatever rows survived pruning — but
only while the ledger is empty, so restarts never double-count.

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
  Headline totals (keys page `request_count`/`token_count`, `usage_totals`, `_all_time`,
  `daily_usage`/heatmap) are summed from the permanent `usage_daily` ledger, never from
  `requests`, so hitting `request_log_limit` cannot freeze a counter. `model_usage`
  and `host_usage` still read `requests` and therefore describe only the retained
  window. `purge_requests` clears the log and leaves the ledger alone.
- **Per-key logging** (`log_requests` / `log_message_contents`, `_persist`): each
  key carries a master **Log requests** switch and a child **Log message contents**
  switch, both on by default and edited in the key form's *Logging settings*
  section. The child is disabled/unchecked in the UI while the master is off, and
  `_read_key_form` forces `log_message_contents` off whenever `log_requests` is
  off, so the nested invariant holds even on a forged submit. At request time
  `_persist` still settles rate-limit counters and bumps `usage_daily`, so
  accounting never stops; `log_requests` off only skips the `requests` row, and
  `log_message_contents` off stores `request_body`/`response_body` as `NULL` while
  keeping the row's metadata (model, status, tokens, latency, error). Locks in
  `tests/test_logging_settings.py`.
- **Image generation & embeddings** (`/v1/images/generations|edits|variations`,
  `/v1/embeddings`): already-forwarded endpoints made first-class by
  `_endpoint_family(path)` (`IMAGE_PATH_RE` / `embeddings` / default `chat`).
  When a non-chat family's upstream `usage` is missing or all-zero, the local
  fallback is `tokens.prompt_estimate` (prompt side only — URLs, base64 blobs and
  float vectors are never counted, and multipart bodies count their form fields,
  not the uploaded image bytes via `usage.parse_multipart_fields`). The viewer
  renders a `prompt` request block and a `data` response block (`_data_items_text`
  → URL / `[image] base64, N characters` / `[embedding #i: N dimensions]`), and
  `_meta` carries `n`/`size`/`quality`/… . **Premodels are chat-only**: a
  `<p>-slug` on a non-chat endpoint is a hard `403 premodel_chat_only`, a premodel
  prefix on a multipart body is `400 premodel_body_not_json`, and the admin form
  refuses non-chat base models (`premodels.is_non_chat_model`, substring markers —
  "embed", "dall-e", "gpt-image", "flux", …) so an image/embedding model can never
  back a premodel. `tests/test_image_embed.py` locks all of this.
  **Per-key URL mode** (`image_response_mode`): a key set to `url` gets its
  `b64_json` items replaced by MidWare-hosted `GET /img/<id>` URLs — the bytes
  are written to `IMAGES_DIR`, registered in `stored_images` with the key's
  `image_retention_days` expiry (`0` = forever), and the rewritten (small) body
  is what both the client and the `requests` log see. The rewrite runs in
  `_forward`'s non-streamed branch only for 2xx image-family responses; upstream
  `url` items, chat/embedding responses, and error bodies pass through
  untouched. `/img/<id>` is deliberately public (the 128-bit id is the
  capability, like an upstream CDN URL), sniffs the file's magic bytes for its
  content type, serves expired/unknown ids as 404 with lazy cleanup, sets a
  week-long `Cache-Control`, and always sends `Access-Control-Allow-Origin: *`
  (static wildcard — deliberately not per-key CORS, since possession of the URL
  is the capability). There is no background worker: expiry happens on
  serve, on key delete, and in the `images.sweep()` run after each store.
  The key form's *Image responses* panel (checkbox + retention days + a *Send a
  HTTPS based URL* switch) drives the `image_urls`/`image_retention_days`/
  `image_https` form fields; `image_url_https` rewrites `http://` to `https://`
  in the advertised URL only.
- **Per-key rate limits** (`proxy._rate_limit_check`): opt-in per key — the
  `limit_*_enabled` master switch must be on *and* a child window ticked *and* its
  `max_*` above `0`. The check runs in `_forward` after the route/model permission
  checks and **before** any upstream I/O, for both `/v1/*` and `/proxy/*`;
  `GET /v1/models` is exempt. Request quotas are claimed with
  `db.rate_usage_claim` — one transaction bumps every enabled window, and any
  window landing over its cap rolls the whole claim back, so a rejected attempt
  never eats quota. Token quotas are read at request time and settled afterwards
  in `_persist` from the final `total_tokens` (0 on error responses), so a single
  response may slightly overshoot a token cap before the 429s start. Over-quota
  gets `429` + `Retry-After` (seconds to the window end) with codes
  `request_limit_exceeded` / `token_limit_exceeded`
  (`type: midware_rate_limit_error`), and the rejection is still logged as a
  `429` request row (`error = "rate_limited: <code>"`). Keys with no limits in
  force do zero counter writes.
- **Per-premodel rate limits**: premodels carry the same `limit_*`/`max_*` columns
  and are checked immediately after the key in `_forward`, with their own
  `premodel_rate_usage` counters (`db.premodel_rate_usage_*` helpers). Identical
  semantics — master switch + ticked window + cap above `0`; request claims are
  all-or-nothing; tokens settle in `_persist`; `429` codes are shared with key
  limits. A key-limit rejection rolls back before the premodel claim runs, so it
  never eats premodel quota; plain (non-premodel) calls touch no premodel counters.
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
- **Sign in with service** (`signin.py`): the route forms (create, edit drawer and
  first-run setup) offer a "Sign in with service" dropdown — a Nano-GPT device
  login (`client_name` = `NANOGPT_CLIENT_NAME`, default "Midware Request
  Middleware") under two route presets (pay-as-you-go `nanogpt`, host
  `https://api.nano-gpt.com/api/v1`, and `nanogpt-sub`, host
  `https://nano-gpt.com/subscription/v1` — same handshake, different prefill)
  or an OpenRouter OAuth PKCE popup. Both flows **open their approval page in a
  tab automatically** (popup opened synchronously, aimed once the server hands
  back a URL; the manual link stays as the popup-blocked fallback). Half-finished
  flows live in a
  module-level `_pending` table keyed by an unguessable token with a 10-minute
  TTL; the three `/admin/signin/*` endpoints bypass the login guard **only while
  no admin account exists** (the bootstrap setup page offers them too) and demand
  Basic once claimed. The acquired key never touches the server: `signin.js`
  fills the route form (key, provider host, and a suggested route name —
  `OpenRouter (OAuth)` / `NanoGPT` / `NanoGPT (Subscription)`, suffixed ` (n)` on
  collision, computed
  server-side in `admin._suggest_route_name` while excluding the route being
  edited), writes the hidden `provider` field with the provider id, and
  **auto-submits the form** through the normal create/update path
  (so `test_connection` still applies; skipped only when required fields like
  the setup code are still empty). OpenRouter's popup
  hands the key back via a `localStorage` entry keyed by the flow's state (its
  auth page severs `window.opener`, so postMessage alone is not enough), with
  postMessage as a bonus channel. The result page itself never renders the
  key — it is a "You can close this tab now" confirmation, and the key travels
  only inside the payload the form consumes.
  The routes table's **Auth Type** column turns the tag into a blue
  `OAuth · <service>` pill (hand-configured routes show green `API key`,
  keyless ones yellow `none`).
- **Balance lines** (`balance.py` + `GET /admin/routes/<id>/balance`): the routes
  page renders a small money-left line under each **tagged** route's name
  (`static/routes.js` fetches them async, so one slow provider cannot block the
  page). Tagging is the trust anchor — only sign-in-obtained routes are ever
  queried: NanoGPT pay-as-you-go routes answer `POST /api/check-balance`
  (`usd_balance`), `nanogpt-sub` routes read `GET /api/subscription/v1/usage`
  for the daily (then weekly) remaining-token quota and fall back to the cash
  line when no quota applies, and OpenRouter tries `GET /api/v1/credits`
  (management keys) before falling back to `GET /api/v1/key`
  (`limit_remaining`). Failures and untagged routes show
  nothing (`{"ok": false}`), never an error; results are cached for 60 s keyed
  by a digest of the upstream key.
- **Premodels** (`premodels.py`): a premodel bundles a route, a model and a prompt
  preset, addressed as `<p>-slug` in `model`. The prefix is **configurable** — the
  `premodel_prefix` setting (`MIDWARE_PREMODEL_PREFIX` changes the default; the
  settings page has a reset button) — and read per request via
  `db.premodel_prefix()`; `extract_premodel_prefix(model, prefix)` builds its regex
  with `prefix_regex` (escaped, cached). The prefix is parsed before
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
  merged into `GET /v1/models` as `<prefix>slug`. Each premodel also carries
  optional token/request rate limits (the collapsed `Premodel limits` panel of the
  form, same master-switch semantics as key limits). Admin CRUD lives at
  `/admin/premodels`.
- **Workflows** (`workflow.py`): a premodel with `use_workflow` set replaces the
  prompt overlay with a linear chain — an undeletable **Origin** publishes
  `core_context` (the caller's `messages`, JSON-encoded) and `system_request`
  (method, path, headers minus credentials); up to `WORKFLOW_MAX_INTERMEDIATE` (8)
  nodes each make nested, non-streaming
  `POST .../v1/chat/completions` via the shared `httpx.Client` with their own
  route/model (defaulting to the premodel's) and a per-node timeout. Node types:
  **API Request** (optional `expect_json` toggle — parses the reply, code fences
  stripped, and exposes keys as `<node>.<key>`; raw text is always
  `<node>.content`) and   **Image Captioner** (`image_mode` `batch` = all images in
  one request, `individual` = one request per image; grabs every `image_url`
  content part from the caller's messages, exposes `{{ node.content }}` and
  `{{ node.count }}`, and is silently skipped when the request carries no
  images). A captioner node with `strip_images` set (opt-in checkbox, default
  off) **strips images from core context** after it consumes them
  (`strip_images`) — the final splice and later nodes' `{{ core_context }}`
  never re-send them, and messages left with no parts are dropped.
  **Environment** nodes make no request — each configured action publishes
  variables; currently `time` (`Declare Time`) exposes `<node>.time.unix` and
  `<node>.time.formatted("YY-MM-DD")` via the **call syntax**
  `{{ path("arg") }}` / `@@path("arg")@@` (`CALL_MACRO_RE` /
  `CALL_FALLBACK_RE` run before the plain macro pass; a callable referenced
  without a call stays literal). Timestamp tokens: `YY`/`YYYY`, `MM` (month),
  `DD`, `HH`, `MN` (minute — not month), `SE`; other characters pass through,
  and a token is only
  substituted at non-letter boundaries so words like `SECONDS` stay literal.
  The node's **Use system timezone when formatting is called** checkbox
  (`use_system_timezone`, default on; old blobs without the key default to on)
  picks `time.localtime`; unticked means UTC via `time.gmtime`.
  **Context** nodes make no request — they publish conversation statistics for
  inspection/trimming: `<node>.count`, `.user_count`, `.assistant_count`,
  `.image_count`, `.chars`, `.text` (flattened with images as `[image]`),
  `.first_user`, `.last_user`, `.last_assistant` and `.trimmed` (a JSON string).
  Options: `trim_count` (keep the last N messages, 0–1000, 0 = keep all),
  `trim_drop_system`, `trim_drop_images` (both default off; drop-images reuses
  `conversation.strip_images` semantics — messages left with text survive, fully
  emptied messages are dropped). The node's reasoning entry is
  `Inspected context: N messages (X user / Y assistant, Z images)`.
  **JavaScript** nodes run the script in a sandboxed V8 isolate via the
  `mini-racer` package (`mini-racer>=0.14,<0.15` — the pin matters: the sync
  `_bind_py_function` driver touches the private `mr._ctx.event_loop`; the
  module import is `py_mini_racer`; it is a hard dependency in `requirements.txt`,
  and `_load_mini_racer` still raises a clear `WorkflowNodeError` if somehow
  missing). The script
  must define `function run(midware)` (may be async) with access to
  `midware.messages`, `midware.vars` (a snapshot of earlier nodes' vars merged
  with this node's `set()`s so far), `midware.systemRequest`,
  `midware.set(k, v)` (→ `<node>.k`), `midware.setMessages(array)` (rewrites
  context exactly like the captioner strip — re-publishes `core_context` and
  refreshes `images`), `console.log` (circular-safe: the stringify is wrapped
  so a cycle degrades to "[Unserializable]" instead of failing the node),
  `await midware.fetch(url, opts)`
  (returns `{status, ok, body, headers}`; http(s) only, 10 s timeout, 1 MB body
  cap, gated by the per-node `allow_network` checkbox, default off) and
  `await midware.chat({messages, model, ...})` — a **server-side** chat call
  through the node's connection (`_make_js_chat`). Unlike `midware.fetch`, chat
  does **not** require `allow_network`: like an API Request node it is MidWare
  routing (the route's host + upstream key never reach the sandbox), the model
  falls back `params.model` (must be a string when given) → node `model` →
  premodel model, well-known sampling knobs
  (`temperature`, `top_p`, `max_tokens`, `max_completion_tokens`,
  `response_format`, `tools`, `tool_choice`, …, `_CHAT_PARAM_KEYS`) pass
  through, and usage folds into the request's totals — **multiple chat calls in
  one node accumulate**. The `messages` array is validated before sending: every
  entry must be an object with a non-empty string `role` and a string-or-array
  `content`; a non-dict JSON response (a bare list/number) is refused rather
  than silently returning `""` from `ask()`. A `route_id`/`model` may
  be set on the node (the editor shows Connection + Model); with neither a node
  nor premodel route, `midware.chat` fails with a clear "no connection" error.
  A built-in **`APIRequest`** class (defined at top level, so scripts can
  `class GLMRequest extends APIRequest { … }` once outside `run`) wraps the
  bridge into an OpenAI-like API: `create(params)` merges constructor defaults
  (`this.model`, `this.temperature`, `this.max_tokens`) with per-call params and
  returns the full parsed response; `ask(text, params)` is sugar that sends a
  single user message and returns the assistant text. A sibling
  **`APIImageRequest`** class mirrors it for image generation: `create(params)`
  POSTs `.../v1/images/generations` through the node's connection
  (`_make_js_image`, model falls back `params.model` → node `model` — never the
  premodel's, which is chat-only — and image knobs like `size`/`n`/`quality`
  pass through via `_IMAGE_PARAM_KEYS`), `ask(prompt, params)` returns the first
  image's `url` (or `b64_json`); usage (`input_tokens`/`output_tokens` aliases
  included) folds like chat usage, and there is deliberately **no**
  `midware.image()` — the class is the only way in. The script
  body is stored in the node's `prompt` field (required, ≤
  `WORKFLOW_MAX_CONTENT`), `timeout_seconds` clamps 1–30 (default 5), and V8 is
  capped at 16 MB with no filesystem and no access to MidWare secrets. The
  sandbox swallows no errors: a throw, a missing `run`, a timeout or a rejected
  fetch all surface as `WorkflowNodeError` → `502 workflow_node_failed`.
  `WorkflowNodeError` carries an optional `public_detail` the client sees
  instead of the raw detail — `_public_error_detail` scrubs `HTTP \d{3}` to
  "an HTTP error" and exception class names (`*Error`/`*Exception`/`*Timeout`)
  to "an error", so upstream status codes and Python internals stay in the DB
  row (`error` column) but never leak to the caller.
  Two bridge quirks shaped the design (regression-locked): everything crossing
  Python↔JS must be a JSON string (mini-racer only converts primitives/handles
  on calls), and Python exceptions raised inside `wrap_py_function` come back
  masked as "Error running Python function" — so `midware.fetch`/`midware.chat`
  failures are **returned** as `{"error": "..."}` JSON and re-thrown on the JS
  side. The
  bootstrap defines `console` and `APIRequest` at top level (user code is
  concatenated between head and tail, so their `run` and subclasses close over
  the same scope), and mini-racer's
  context managers can consume exceptions raised in the body — `_run_javascript`
  uses the inside-catch outcome-dict pattern so nothing escapes the `with`.
  Because scripts are code and not templates, the type checker **skips their
  scan** and, like Expect JSON nodes, **lifts** the check for any dotted path
  under a javascript node's name. `tests/test_workflows_js.py` is the JS suite
  and `importorskip`s `py_mini_racer` — engine-missing behaviour is locked by
  monkeypatching `sys.modules`.
  **Assure** nodes are guard
  clauses: two templated operands (left, right) plus an operator from
  `ASSURE_OPERATORS` (`equals`, `not_equals`, `contains`, `not_contains`,
  `starts_with`, `ends_with`, `greater`, `less`, unary `empty`/`not_empty` —
  `greater`/`less` compare numerically when both sides parse as floats, else as
  strings); a failed check raises `WorkflowNodeError` and the whole workflow
  aborts before the Final Request, a passed one logs
  `Passed <name> Assurance Test`. Neither type needs a prompt, model or
  connection; `normalize_workflow` stores the unused request fields with
  defaults so the editor can switch types losslessly.
  The undeletable **Final Request** builds the forwarded message list
  from text blocks (templated with every variable) and core-context blocks
  (splice the caller's messages), with an optional non-templated
  `model_override`. Templates expand through
  `WorkflowScope` (arbitrary nesting depth — `node.key.subkey`; unknown names
  stay literal, matching MWVAR). Save-time **type checking**
  (`_validate_references`, run by `normalize_workflow`): every `{{ path }}` in
  a node's prompt/system prompt/model/assure operands or in a Final text block
  must exist *at that position* — origin vars (`core_context`,
  `system_request`) always, plus the paths published by **earlier** nodes
  (`<n>`, `<n>.content`, `<n>.reasoning`, captioner `<n>.count`, environment
  `<n>.time`/`.unix`/`.formatted`, context `<n>.count`/`.trimmed`/`.text`/...);
  Expect JSON and JavaScript nodes lift the check for any dotted path under
  their name (their keys are unknowable in advance), and forward references are
  rejected. Unknown names still stay literal at *runtime*, but now cannot be
  saved.
  **Sub-calls intentionally bypass the calling key's provider/model allowlists** —
  they are server-side machinery the admin configured; only the final forwarded
  model is checked (locked by `test_workflow_final_model_still_gated_by_allowlist`).
  Execution happens in `_forward` **after both rate-limit claims**, so a rejected
  request never spends upstream tokens; any node failure returns
  `502 workflow_node_failed` (`workflow_invalid` when the stored blob cannot load)
  and never forwards. Sub-call token usage is **folded into the single request
  row** and premodel/key token limits settle on the folded total; error rows stay
  at zero tokens. A node failure still settles the rate-limit counters on the
  sub-call tokens already spent: `run_workflow` attaches the partial `usage` to
  the exception (`exc.usage`), the proxy passes it to `_persist` as
  `rate_tokens`, and `_persist` uses `max(0, rate_tokens)` for the counter writes
  while the row itself records 0 tokens (locked by
  `test_js_partial_usage_settles_rate_limit_on_failure`). The optional **reasoning switch** (`"reasoning": true` on the
  graph, checkbox in the editor) collects any sub-call reasoning (via
  `conversation._collect_reasoning`) and prefixes it to the reasoning the client
  receives, one logged block per node in the form
  `Started constructing node: <name>` + `<name> logs:` + the text in a fenced
  block — non-streamed
  responses are merged into `choices[0].message.reasoning_content` (upstream's
  own reasoning kept after the prefix; `_merge_reasoning_body`), streamed
  responses get a synthetic first SSE delta chunk (`id: midware-workflow`), and
  nodes may reference `<node>.reasoning` in later templates. `system_prompt`,
  `preset_json`, `merge_mode` and `accept_mwvars`
  are ignored in workflow mode; sampling params, rate limits, `[Host]` ordering
  and `/v1/models` listing still apply. The form editor (`static/workflow.js`) is
  a Shortcuts-style vertical card stack with native HTML5 drag-and-drop
  reordering (Origin pinned top, Final pinned bottom, ↑/↓ buttons as fallback),
  a per-card node-type selector, nodes added through an **Add node** picker
  modal, per-node collapse (chevron toggles the body; a one-line peek shows
  name/model/prompt), prompt sections for Image Captioner nodes
  tucked behind a **Prompts** button, the reasoning switch, and Final Request
  blocks edited in a modal (the card only shows a block summary + **Edit
  blocks** button; modals are document.body overlays, close on Escape /
  overlay click, and keep state across re-renders via `redraw()`).
  New JavaScript nodes seed `JS_SCRIPT_STARTER` (a minimal
  `async function run(midware)` skeleton) when the prompt is empty — in
  `cloneDefaults` and on the type-picker switch to `javascript`.
  The JS node's script box (`scriptEditor`) lazily upgrades a plain
  `<textarea class="wf-script">` to a **CodeMirror 6** editor: the first
  render kicks off `import()`s from esm.sh with exact-version pins
  (`?deps=` forces one `@codemirror/state` instance across packages —
  without it the mount throws "Unrecognized extension value"); the textarea
  stays the source of truth until the editor mounts, then is removed. Any
  import failure, mount throw, or mid-import redraw (`!fallback.isConnected`)
  keeps the textarea, logs `console.warn`, and calls `degrade()` so the card
  shows a one-line "Plain editor (CodeMirror unavailable)" note — the editor
  is optional and never blocks save (a failed import resets `cmLoadPromise`,
  so the next render retries). A **collapsed** JS card renders the plain
  textarea without importing anything (`scriptEditor(node, collapsed)`), so
  opening a stack costs no CDN round-trips. Completions come from
  `makeJsCompletionSource`
  fed through `jsSupport.language.data.of` (NOT a second
  `autocompletion()` call — `basicSetup` already wires one). The source
  resolves in three steps before falling back to top-level identifiers:
  **object-literal context** (`scanObjectContext` — string/comment-aware, a
  key position offers chat-param keys inside `chat({…}`, message keys inside
  `messages: [{…}`, and chat/fetch response keys after `:`; `regexEscape`
  keeps regex-based anchors safe), **member chains** (`extractMemberChain` +
  `resolveChainOptions` — `resp.choices[0].message.` → message keys,
  `.usage.` → usage keys, `r.` on a fetch/APIRequest response → its keys,
  `mw.`/`midware.` → the bridge methods, `this.` → `APIRequest` members), then
  top-level names: member-aware for `midware.*` / `console.*` / `this.*` (the
  last only when the script contains `extends APIRequest`), plus snippets for
  `run` and an
  `APIRequest` subclass, sandbox-accurate globals from the mini-racer probe,
  and the ABSENT APIs (`URL`, `TextEncoder`, `fetch`, …) offered with an
  "Not available in this sandbox" info tooltip. Theme and syntax colours
  come from Pico CSS variables (`picoEditorTheme`), so light/dark follow
  the site — including the search panel (`cm-panels`/`cm-search`),
  matching/non-matching bracket marks, and search-match highlights.
  `makeDraggable` drops the card's `draggable` flag on mousedown
  over any
  `input, textarea, select, button, label, .cm-content, .cm-gutters` (plus
  `.cm-editor`/`.cm-scroller`/`.cm-tooltip`/`.cm-panels`) — a
  draggable ancestor would
  otherwise swallow mouse carets inside the prompt textareas / CM surface
  (keyboard focus
  kept working, mouse placement did not).
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
- The provider fake in `tests/test_signin.py` (`FakeUpstream`/`install`) also
  serves `tests/test_balance.py`; its balance attributes answer with a body or
  `None` to simulate a refusal.
- Streaming tests must consume the response body, e.g.
  `b"".join(response.response)`, otherwise the generator never runs and no request
  row is written (a confusing `IndexError`).
- Tests run against `:memory:` SQLite with `TESTING=True`, which disables the
  model-refresh hooks and the MSQ scheduler. Drive probes directly with
  `msq.run_recorder` / `msq.run_due` and a stubbed HTTP client.
- Workflow JavaScript-node tests live in `tests/test_workflows_js.py` and
  `importorskip("py_mini_racer")` — the whole file skips when the optional
  engine is not installed (it is now in `requirements.txt`, but the skip keeps
  a stripped install working); keep engine-missing behaviour covered by
  `test_js_missing_engine_reports_node_error`, and keep the documented sandbox
  surface honest via `test_js_sandbox_globals_match_probe` (a `typeof` sweep of
  every present/absent global — update the lists together with the
  `midware.*` docs above).
- Image generation / embedding coverage lives in `tests/test_image_embed.py`
  (endpoint families, multipart edits, premodel chat-only, viewer blocks); its
  `image_response` / `embedding_response` helpers build `StubResponse`s with
  **body bytes** (`content=`), because `_persist` reads the raw upstream body —
  a `json_body=`-only stub looks like "no usage".
- Per-key request/message logging coverage lives in
  `tests/test_logging_settings.py` (skipped-row accounting, metadata-only rows,
  form round-trip); the DB-level switches are in `tests/test_db.py`.
- Keep the suite green and add a test for every behaviour change. Current baseline:
  `441 passed` — **update this number whenever the test count changes.**

## Conventions

- Type hints and `from __future__ import annotations` in modules; docstrings explain
  *why*, not *what*.
- Snake_case functions, small helpers prefixed `_` for module-private.
- No comments that restate code. No emojis.
- Errors returned to clients follow the OpenAI error shape
  (`{"error": {"message", "type", "code"}}`).
- Never log or store upstream secrets anywhere except the `routes.upstream_key`
  column; client keys have an `mw-` prefix.
