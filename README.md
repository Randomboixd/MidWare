# MidWare

A gloriously uninteresting middleware for LLM APIs.

MidWare sits between your application and your LLM providers. It gives you one
OpenAI-compatible endpoint, while handling routing, API keys, model permissions,
logging, Premodels, token accounting, monitoring, and other fun little pieces of
infrastructure that you probably don't want to implement five times.

It's intentionally boring. One container. SQLite. No Kubernetes-powered
distributed microservice nightmare. Just middleware.

> **⚠️ This is a vibecoded project.**
>
> A large portion of MidWare was built with **DeepSeek Flash** through OpenCode.
> I originally picked it because I wanted to test the model properly, and
> honestly, it looked like a fucking good deal.
>
> At the time of writing, the total DeepSeek API cost of building this thing was
> roughly **$2.12 USD (~700 HUF)**.
>
> There are absolutely human touches in here, especially around the UI,
> architecture, security decisions, debugging, and the parts where I had to stop
> the AI from doing something stupid.
>
> So yes: this is AI-assisted software. Please keep that in mind when reading the
> code.

## What does MidWare actually do?

The basic flow is:

**Your application → MidWare → Route → LLM provider**

Your application only needs to know about MidWare. MidWare handles the annoying
shit in between.

Give an application a key that is allowed to use only certain models:

`mw-xxxxxxxx`

Then the application asks for:

`z-ai/glm-5.3-flash`

MidWare checks the key's permissions, works out where that model lives, forwards
the request with the private upstream credentials, records whatever it is
supposed to record, and sends the response back.

You can also pick a provider explicitly:

`[DeepSeek]deepseek-chat`

So one MidWare instance can expose models from several providers without every
client needing to know how those providers are configured. If two providers have
the same model name, the `[Name]` prefix is how you keep them apart. There is
also `[#3]` / `[Name#3]` for when names are ambiguous.

`GET /v1/models` returns MidWare's own merged catalogue, not one provider's list.
The default route's models are bare; everyone else's get a `[Name]` prefix. The
catalogue is cached, refreshed when routes change, and can be refreshed by hand
from the Routes page.

## Terminology

MidWare has a few terms that can get confusing because apparently naming things
wasn't enough suffering already.

**Route** — A configured connection to an upstream provider/API. For example, a
route might point at DeepSeek's API.

**Model** — The actual model identifier provided by a route, such as
`deepseek-chat` or `z-ai/glm-5.3-flash`.

**Host** — The upstream service behind a route. In some parts of the UI/code
you'll see these terms used together.

**Premodel** — A server-side model configuration that behaves like a model
itself. It can pick an underlying model, inject prompts and sampling settings,
carry its own rate limits, and expose a new slug such as `<p>-st-assistant-model`
(the prefix is configurable in Settings). With **Workflows** enabled, a premodel
instead runs a chain of AI sub-calls before forwarding the final request.

**Workflow** — A chain of server-side steps attached to a premodel: origin,
AI calls, image captioning, small utility nodes, guards, and a final request.

**API Key** — A MidWare-generated `mw-...` credential. Clients authenticate to
MidWare with these instead of receiving your actual upstream provider keys.

**MSQ** — Model Service Quality. MidWare's synthetic monitoring system. It
periodically sends real requests to a model and measures things like time to
first token, response time, tokens/sec, stream integrity, and optional quality
checks.

**Request** — A request passing through MidWare. Requests can be recorded and
inspected from the dashboard, subject to the configured logging behavior.

Yeah, some of this terminology is a little spaghetti. That's why you're reading
this section first. :3

## Features

**Proxy**

- OpenAI-compatible chat, models, image generation and embedding endpoints.
- Streaming responses, passed through as they arrive.
- Multiple upstream routes with one default.
- `[Host]` model prefixes so one client can reach every configured provider.
- A merged model catalogue with sensible, stable slugs.
- Provider sign-in helpers for Nano-GPT (pay-as-you-go and subscription) and
  OpenRouter, so you don't have to copy-paste an upstream key. Tagged routes
  show a small balance line where the provider allows it.

**Access control**

- Per-key provider, premodel and model allowlists.
- Per-key CORS allowlists for browser clients.
- Per-key request and token limits — from per-minute requests to per-week token
  budgets, as many or as few as you want.
- The same limit system on individual premodels, counted separately from the
  calling key.
- Keys can be switched off without deleting them. Usage, history and limits stay;
  the token just stops working until you turn it back on.
- Real upstream provider keys never leave the server.

**Accounting and history**

- Token accounting for normal and streamed responses.
- A permanent usage ledger, so pruning old request rows never makes your totals
  shrink.
- Dashboard totals for today, this month, this year and all time.
- A GitHub-style activity heatmap and a day-by-day usage table.
- Request history with status, latency, model, route, key and token counts.
- A request inspector that turns a captured request/response into a readable
  conversation, including streamed replies and image/embedding responses.
- Local token estimates when a provider forgets to send usage, clearly marked as
  estimates.
- Per-key logging switches: log nothing, log metadata only, or log full message
  contents. Accounting keeps working even when logging is off.

**Models and media**

- Premodels: prompt presets, sampling overrides, merge modes, MWVARS macros,
  SillyTavern preset import, their own limits, and their own model slugs.
- Workflows for Premodels: chained AI calls, image captioning, context helpers,
  JavaScript, guard clauses and a templated final request.
- Image responses can stay inline as base64, or be stored by MidWare and returned
  as `/img/...` URLs with configurable retention and an optional HTTPS URL mode.
- Premodels are chat-only, on purpose. Pointing one at an image or embedding
  model is refused instead of doing something weird.

**Quality and automation**

- MSQ probes with scoring, charts, history, stream-integrity checks,
  text-forensics checks and long-context needle tests.
- A signed, permissioned System Management API for automation.
- Docker deployment, SQLite storage and a private-friendly setup.

## Premodels

Premodels are MidWare's way of turning a model configuration into another model.

You can define something like:

> `<p>-st-assistant-model` → `z-ai/glm-5.3:thinking`

and configure how the Premodel interacts with the incoming request.

There are three merge modes:

- **Append** — keep everything; the caller's prompt follows the premodel's.
- **Caller wins** — if the caller sent a system prompt, ignore the premodel's.
- **Premodel wins** — drop the caller's system prompts and use the premodel's.

You can also let a premodel fill in sampling values the caller did not set
(temperature, top-p, max tokens), and accept MWVAR macros from the caller. MWVARs
are simple `@name=value` definitions that can then be used as `{{ name }}` or
`@@name@@` inside prompts.

Premodels support SillyTavern presets too. Upload a preset, tick the prompts you
want, and MidWare saves them as the premodel's prompt source.

Each premodel can carry its own token/request limits, independent of whatever
limits the calling key has.

Premodels are chat-only: their base model must be a chat model (the admin form
rejects image-generation and embedding ids), and a `<p>-...` prefix on
`/v1/images/*` or `/v1/embeddings` is rejected with `403 premodel_chat_only`.

## Workflows

Tick **Use Workflows** on a premodel and the prompt preset is replaced by a
Shortcuts-style chain of steps, edited as a vertical stack of cards. Drag cards
to reorder them, collapse the ones you don't need, and add new ones from the
**Add node** picker. There can be up to eight intermediate nodes between the
fixed start and end.

- **Origin** (fixed) — publishes the inbound request as template variables:
  `core_context` (the caller's messages) and `system_request` (method, path,
  headers minus credentials).
- **API Request** — makes a server-side chat call with its own connection and
  model. Turn on *Expect JSON* and the reply is parsed so keys become variables;
  the raw text is always available too.
- **Image Captioner** — grabs every image in the caller's messages and captions
  them with a vision model. Either all images in one request (faster) or one
  request per image (more compatible). Requests without images simply skip it.
  The optional **Strip images** checkbox removes captioned images from the rest
  of the chain, so the final request doesn't send the same image twice.
- **Environment** — no request, no tokens. Currently declares the time, with
  local or UTC formatting and the usual year/month/day/hour/minute/second tokens.
- **Context** — no request, no tokens. Publishes conversation statistics: message
  counts, character count, a flattened transcript, the first/last user and last
  assistant messages, and a trimmed copy of the conversation. Handy for
  summarising or shrinking a long history before the final call.
- **JavaScript** — a sandboxed escape hatch. Your script gets the conversation,
  earlier variables, a `set()` helper, a way to rewrite the messages, a console
  log, server-side `chat()` calls, and optional `fetch()` calls (network access
  is off by default). There is also a small `APIRequest` helper (and
  `APIImageRequest` for images) if you prefer an OpenAI-looking client shape. The
  editor is CodeMirror when it can load, and a plain textarea when it can't.
- **Assure** — a guard clause. Compare two templated values (equals, contains,
  starts with, greater than, is empty, and friends). If the check fails, the
  workflow stops before the Final Request.
- **Final Request** (fixed) — builds the message list that is actually forwarded:
  templated text blocks plus blocks that splice in the caller's original
  messages. You can optionally override the model here.

Template variables use the familiar `{{ name }}` / `@@name@@` forms. Saving a
workflow type-checks every reference, so typos and forward references are caught
in the form instead of failing at 2am. Expect-JSON and JavaScript nodes are
exempt from that check because their variables only exist at runtime.

Turn on **Node reasoning** and any reasoning a sub-call produces is prefixed to
the reasoning the client receives, one logged block per node.

Sub-calls are not limited by the calling key's model/provider restrictions (only
the final model is), and their token usage is folded into the one request row. A
failing node aborts with `502 workflow_node_failed` before anything is forwarded.

## MSQ

MSQ exists because "the model feels kinda slow today" isn't exactly a useful
monitoring strategy.

A recorder periodically sends a real request to a configured model and measures
it.

It can track:

- Time to first token
- Total response time
- Tokens/sec
- Token count
- Stream integrity
- Repetition and malformed output
- Unexpected scripts/symbols
- Long-context retrieval
- Historical performance

Each check gets a score from 0 to 100. Slow first tokens, slow totals, stray
non-Latin symbols and configured slop words knock points off; hover a chart point
to see exactly why. A recorder spends its first day or so **Examining**, then
settles into **Working** or **Degraded** based on its recent average.

The optional **hardcore checks** are higher-signal and all off by default:

- **Stream integrity** catches truncated streams, malformed chunks, provider
  error events and mid-stream stalls.
- **Text forensics** catches repetition loops, glitch/mojibake text and leaked
  chat-template tokens.
- **Long-context needle** hides a code in filler text and asks for it back.

A hardcore failure can mark a model **Failing** immediately instead of waiting
for the average. Recorders can also file their probes as normal requests under a
hidden **MSQ Measurements** key, so they show up in history and token totals.

Keep in mind that MSQ sends real requests and therefore **uses real provider
tokens**.

## System Management Accounts

For automation that needs to *manage* MidWare — creating routes, minting keys,
editing MSQ recorders — a plain `mw-...` key is not enough. The System Management
API at `/_midware/api/v1` requires every request to be signed with an Ed25519
key, so a stolen token alone cannot be used. The full protocol and a
copy-pasteable client live in [SMA.md](SMA.md).

The short version:

1. The client generates a keypair and asks to enroll, saying what it wants to do.
2. The admin opens a consent page, sees exactly what is requested and where the
   client will send the user back, then allows or denies it.
3. The client exchanges its pending token for a live one.
4. Every later call is signed and must stay inside the granted permissions.

Permissions are deny-by-default. An account can manage routes, its own keys, key
on/off status, or MSQ recorders, depending on what it was granted. It can always
shut itself off. Revoking an account takes effect on its next call and deletes
the keys it minted.

The whole surface is switched from **Settings → System Management Accounts API**.
It refuses plain HTTP by default. If the instance has no TLS and sits on a
network you trust, you can allow plain HTTP; behind a TLS terminator, set
`MIDWARE_TRUST_PROXY_HOPS=1` so MidWare sees the original scheme.

## Signing in to providers

Adding a route doesn't have to mean hunting for an API key. The route forms
offer a **Sign in with service** menu:

- **Nano-GPT** — pay-as-you-go device login.
- **Nano-GPT (Subscription)** — the same handshake, pointed at the subscription
  endpoint.
- **OpenRouter** — OAuth in a popup.

The provider key goes straight into the route form, never into a MidWare page
that displays it, and the form submits itself when everything is filled in.
Routes created this way are tagged, so the Routes page can show a balance line
under the name (cash balance, remaining subscription tokens, or remaining
OpenRouter credit) without you asking.

## Security

MidWare is designed around the idea that upstream API credentials should stay on
the server.

Clients receive `mw-...` keys instead.

The admin interface is separately protected with a browser login at `/signin`,
while `/v1/*` uses MidWare API keys. The first time you set up an instance, you
need the setup code printed in the server console — that's the "I control this
host" check. Sessions last 30 days by default, survive closing the browser, and
are refreshed on each visit. Changing the password signs out every other browser.

Request message contents are hidden from signed-out visitors, even though the
dashboard itself is public.

For anything exposed beyond a trusted local network, use HTTPS. Over plain HTTP
the login password and session cookie are sent to the server unencrypted. When a
TLS proxy terminates HTTPS in front of MidWare, set
`MIDWARE_SESSION_COOKIE_SECURE=1` so the browser only returns the session cookie
over HTTPS.

A private Tailscale deployment is also a very nice way to keep MidWare away from
the public internet.

### Security disclaimer

MidWare should *in theory* have the security controls described above, but this
project has not gone through a proper security audit or penetration test.

I have not deliberately tried to hack the living shit out of MidWare to prove
that those controls actually hold under attack.

So please don't treat MidWare as hardened security software.

If you're using it for anything beyond personal use, especially if it is exposed
to the public internet or handling sensitive API credentials, **put it behind a
properly configured reverse proxy and add another layer of protection**. HTTPS,
access controls, rate limiting, firewall rules, and whatever other security
controls make sense for your deployment are strongly recommended.

If you find a security issue, please report it responsibly rather than
immediately turning my little middleware into a thermonuclear bomb for someone's
api key. ❤️ (ps: i might act awkward in pull requests or sound like a total
robot. the latter might be because i'm so spooked that i'm pointing opencode at
it lmao)

## Deployment

The easiest way to run MidWare is with Docker Compose.

```yaml
services:
  midware:
    image: ghcr.io/randomboixd/midware:latest
    restart: unless-stopped
    ports:
      - "${PORT:-5000}:${PORT:-5000}"
    environment:
      HOST: "0.0.0.0"
      PORT: "${PORT:-5000}"
      MIDWARE_SECRET_KEY: "${MIDWARE_SECRET_KEY:?set MIDWARE_SECRET_KEY}"
    volumes:
      - ./appdata:/data
```

The container image is published as:

`ghcr.io/randomboixd/midware:latest`

The `appdata` directory contains MidWare's persistent data, including its SQLite
database and any stored images.

### Setting the secret key

`MIDWARE_SECRET_KEY` signs the login session cookie and should be a long, random
secret. Don't just use `password123`, your cat's name, or the output of
`echo "lol"`. Docker Compose requires it; when it is omitted (for example when
running `run.py` directly), MidWare generates one once and stores it in the
database. Changing it signs out every browser.

Python's `secrets` module can generate one:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Then put the generated value into your environment:

```bash
export MIDWARE_SECRET_KEY="your-generated-secret-here"
```

Or create a `.env` file next to your `compose.yml`:

```dotenv
MIDWARE_SECRET_KEY=your-generated-secret-here
```

Keep this secret private. If you change it later, make sure you understand the
effects on the existing MidWare instance before doing so.

### Running it locally

If you don't want Docker:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

On Windows, use `.\.venv\Scripts\activate` instead. `HOST` and `PORT` control the
bind address; `MIDWARE_DB` points at a different SQLite file if you want one.

## Why?

Honestly?

I wanted it.

I wanted a small piece of middleware that could sit between my applications and
various LLM providers without turning into an enormous "AI platform" with 47
Docker containers and a Kubernetes cluster just to forward an HTTP request.

So I built one.

Then I kept adding shit.

And now we have workflows, monitoring, and a whole signed automation API.

## License

MidWare is licensed under the **MIT License**.

See [`LICENSE.md`](LICENSE.md) for the full license text.

If you're reading this because you found the project on GitHub: **hello!** Feel
free to poke around, modify it, break it, improve it, or tell me that some part
of the architecture is fucking insane.

That's kind of the point of putting it here.

**Made in Hungary**
