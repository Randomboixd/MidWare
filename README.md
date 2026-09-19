# MidWare

A self-hosted, asynchronous LLM proxy that does two things well:

1. **Tracks every token** that flows through it — per day, month, year, all time, per
   model and per client key, with a GitHub-style contribution heatmap.
2. **Centralizes your API key** — your real provider credential lives on the server,
   clients only ever see a MidWare key.

Built with Flask, `httpx`, Jinja2 templates and [Pico CSS](https://picocss.com/).
Storage is a single SQLite file. No frontend framework, no build step.

## Features

- OpenAI-compatible proxy endpoint (`/v1/chat/completions`, `/v1/embeddings`, …) that
  transparently forwards **and** streams responses chunk-by-chunk.
- Token accounting for both **JSON** and **SSE streaming** responses, so `usage` is
  recorded even when the client asked for `stream: true`.
- Understands OpenAI (`prompt_tokens`/`completion_tokens`) *and* Anthropic-style
  (`input_tokens`/`output_tokens`) usage blocks.
- Multiple routes with exactly one default upstream; the rest are reached with a
  `[Host]` model prefix. Routes are editable (name, target, key) from the UI.
- Aggregated **model catalogue**: every route's `/v1/models` is fetched on route
  changes and roughly daily, then served back at MidWare's own `/v1/models` as a
  single deduplicated list — default-route models bare, all others prefixed with
  `[Name]`. Routes sharing a `target_host` are only fetched once.
- Multiple client keys, each with its own usage totals.
- Every request is logged with the resolved host, model, status, latency, token counts
  and optional request/response bodies. The log is capped at **N requests per key**
  (default `10`, editable in the control panel).
- Dashboard with day/month/year/all-time totals, top hosts, top models, recent requests.
- GitHub-style 52-week activity heatmap plus a day-by-day table.
- Dark theme, server-rendered, works over plain HTTP.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Then open <http://127.0.0.1:5000>. The first visit redirects to `/admin/setup`.

Bind address is configurable:

```powershell
$env:HOST="0.0.0.0"; $env:PORT="8080"; python run.py
```

## Docker

```bash
docker compose up -d --build
```

Then open <http://127.0.0.1:5000>. The SQLite database lives on the named volume
`midware-data`, so the container can be recreated without losing routes, keys or
usage history.

Change the published port (and the port the app binds inside the container) with
`PORT`:

```bash
HOST=0.0.0.0 PORT=80 docker compose up -d --build
```

Or run the image directly:

```bash
docker build -t midware .
docker run -d --name midware -p 80:80 \
  -e HOST=0.0.0.0 -e PORT=80 \
  -e MIDWARE_SECRET_KEY="$(openssl rand -hex 32)" \
  -v midware-data:/data midware
```

`HOST`/`PORT` are read by `wsgi.py`. The container defaults to `0.0.0.0:5000` and
`MIDWARE_DB=/data/midware.db`.

### Configure

| Field | Meaning |
| --- | --- |
| Target host | Upstream base URL, e.g. `https://api.openai.com` or `https://api.anthropic.com` |
| Upstream API key | Your real provider key. Stored in SQLite, injected on every upstream call |
| First MidWare key | The key *you* will send as a client |
| Saved requests per key | How many request/response bodies to retain (`0` = none) |

### Use it

```bash
export OPENAI_BASE_URL="http://127.0.0.1:5000/v1"
export OPENAI_API_KEY="mw-..."        # the MidWare key created during setup

curl "$OPENAI_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

Anthropic-style clients can send `x-api-key: mw-...` instead. Responses are passed
through untouched, so streaming, tool calls and every other feature keep working.

## How routing works

Requests to `/v1/<path>` (and the explicit `/proxy/<path>`) are forwarded to
`<target_host>/<path>`. A trailing `/v1` on the configured target host is not
duplicated, so both `https://api.openai.com` and `https://api.openai.com/v1` are valid.

### Multiple hosts (model prefix routing)

Every route is addressable from the request itself, so one MidWare key can reach
several providers. Prefix the `model` field with the route name in square brackets:

```json
{ "model": "[NanoGPT]xiaomi/mimo-v2.5-pro:thinking", "messages": [] }
```

MidWare strips `[NanoGPT]`, routes to that host, and forwards `xiaomi/mimo-v2.5-pro:thinking`
unchanged. Without a prefix the **default** (active) route is used.

| Form | Resolves to |
| --- | --- |
| `gpt-4o` | the default route |
| `[NanoGPT]gpt-4o` | the route named `NanoGPT` (case/spacing insensitive) |
| `[#3]gpt-4o` | route id `3`, for disambiguating duplicate names |
| `[NanoGPT#3]gpt-4o` | name + id, id wins |
| `[Nope]gpt-4o` | `404 unknown_host` — traffic is never silently misrouted |

The prefix never changes which route is active, and inactive routes stay reachable
by prefix — so `[Local]llama3` can hit a local model while `NanoGPT` remains the
default. Prefixes are recorded per request (`host_prefix`, `model_raw`) and shown on
the dashboard, request log and request detail pages.

The caller's MidWare key is stripped and replaced with the resolved route's stored
upstream key (`Authorization: Bearer …`, or `x-api-key: …` for Anthropic targets).
If a route was saved **without** an upstream key, MidWare falls back to passing the
caller's credential through — handy for keyless local models like Ollama or LM Studio.

### The aggregated model list

Most providers expose a `GET /v1/models`. MidWare fetches each route's list — on route
create/edit, whenever the default route changes, and roughly once a day — and serves
one merged catalogue at its own `/v1/models`:

```json
{
  "object": "list",
  "data": [
    { "id": "gpt-4o-mini", "owned_by": "Default",  "midware": { "default": true } },
    { "id": "[NanoGPT]xiaomi/mimo-v2.5-pro", "owned_by": "NanoGPT" }
  ]
}
```

The default route's models are emitted **bare**, every other route's models are
prefixed with `[Name]`, so a slug copied straight from the response is also a valid
`model` value on the next request. The list is regenerated whenever the default route
changes, and two routes pointing at the same host are only ever fetched once. Refresh
on demand with **Refresh models** on the Routes page.

## Reading a captured request

`/requests/<id>` renders the captured bodies as a conversation instead of raw JSON.
Each request message and each assistant reply becomes a numbered, collapsible block.
Reasoning is pulled out of every spelling MidWare knows (`reasoning`,
`reasoning_content`, `thinking`, `thought`, `analysis`, `reasoning_details`, Anthropic
`thinking` content parts) into its own collapsible section, so presets that emit more
than one variant still show all of them. The last user message and the last assistant
message are open by default; **Expand all** / **Collapse all** override that.

The **Fetch** button re-requests the conversation from
`/requests/<id>/messages.json`, re-renders it and downloads the normalized JSON.
Raw bodies stay available under *Raw payloads*.

Reasoning is de-duplicated: providers that stream the same text through several
spellings at once (for example `reasoning` *and* `reasoning_details`, as NanoGPT
does) are collapsed to a single section instead of repeating every chunk.

### Token counting

Usage is taken from the upstream when it reports it. When a response carries no
`usage` block — common for streamed replies on some hosts — MidWare rebuilds the
prompt and the visible output from the captured bodies and counts tokens locally:
`o200k_base` via `tiktoken` for OpenAI-family models, and a character-ratio
heuristic for everything else. The request detail page labels the origin as
*reported by upstream* or *estimated locally*. Install `tiktoken` (it is in
`requirements.txt`) for exact OpenAI counts; without it MidWare silently falls
back to the heuristic.

## Pages

| Path | Purpose |
| --- | --- |
| `/` | Dashboard: totals, heatmap, top models, recent requests |
| `/activity` | Full 52-week heatmap + last 30 days breakdown |
| `/requests` | Paginated request log, filterable by key/route |
| `/requests/<id>` | Single request: readable conversation + raw bodies |
| `/requests/<id>/messages.json` | Normalized conversation JSON (used by the **Fetch** button) |
| `/v1/models` | Aggregated, deduplicated model catalogue across every route |
| `/admin/setup` | First-run wizard / add another route |
| `/admin/routes` | Add, edit, activate and delete routes |
| `/admin/keys` | Create and revoke MidWare client keys |
| `/admin/settings` | Request-log limit, purge, runtime state |

## Configuration

Environment variables override `midware/config.py`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Bind address for `run.py` / `wsgi.py` |
| `PORT` | `5000` | Bind port for `run.py` / `wsgi.py` |
| `MIDWARE_DB` | `./midware.db` | SQLite path (`:memory:` supported) |
| `MIDWARE_SECRET_KEY` | dev value | Flask secret key — **set this in production** |
| `MIDWARE_UPSTREAM_TIMEOUT` | `300` | Upstream read timeout, seconds |
| `MIDWARE_UPSTREAM_VERIFY_TLS` | `true` | Verify upstream TLS certs |
| `MIDWARE_REQUEST_LOG_LIMIT` | `10` | Initial saved-requests-per-key value |
| `MIDWARE_TARGET_HOST` | `https://api.openai.com` | Pre-filled setup target |
| `MIDWARE_MAX_CAPTURE_BYTES` | `8388608` | Max bytes captured per body |
| `MIDWARE_SQLITE_TIMEOUT` | `5` | SQLite busy timeout, seconds |

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite covers the token parsers (JSON + SSE + Anthropic), the SQLite layer
(including schema migration of older databases), statistics/heatmap aggregation,
host-prefix routing, the proxy (credential replacement, streaming, upstream failure)
and every HTML page.

## Notes and limitations

- Only `POST` is proxied; everything else returns a JSON error.
- Non-streamed responses are buffered so usage can be parsed; streamed responses are
  teed into a bounded buffer (`MIDWARE_MAX_CAPTURE_BYTES`) so memory stays flat.
- There is no auth on the admin UI yet — run MidWare on a trusted network or behind a
  reverse proxy with its own access control.
