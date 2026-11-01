# MidWare

A gloriously uninteresting middleware for LLM APIs.

MidWare sits between your application and your LLM providers. It gives you one OpenAI-compatible endpoint, while handling routing, API keys, model permissions, logging, Premodels, token accounting, monitoring, and other fun little pieces of infrastructure that you probably don't want to implement five times.

It's intentionally boring. One container. SQLite. No Kubernetes-powered distributed microservice nightmare. Just middleware.

> \*\*⚠️ This is a vibecoded project.\*\*
>
> A large portion of MidWare was built with \*\*DeepSeek Flash\*\* through OpenCode. I originally picked it because I wanted to test the model properly, and honestly, it looked like a fucking good deal.
>
> At the time of writing, the total DeepSeek API cost of building this thing was roughly \*\*$2.12 USD (\~700 HUF)\*\*.
>
> There are absolutely human touches in here, especially around the UI, architecture, security decisions, debugging, and the parts where I had to stop the AI from doing something stupid.
>
> So yes: this is AI-assisted software. Please keep that in mind when reading the code. 

## Terminology

MidWare has a few terms that can get confusing because apparently naming things wasn't enough suffering already.

**Route** — A configured connection to an upstream provider/API. For example, a route might point at DeepSeek's API.

**Model** — The actual model identifier provided by a route, such as `deepseek-chat` or `z-ai/glm-5.3-flash`.

**Host** — The upstream service behind a route. In some parts of the UI/code you'll see these terms used together.

**Premodel** — A server-side model configuration that behaves like a model itself. It can select an underlying model, inject settings/prompts, merge caller configuration, carry its own rate limits, and expose a new model slug such as `<p>-st-assistant-model` (the prefix is configurable in Settings). With **Workflows** enabled, the premodel instead runs a chain of AI sub-calls before forwarding the final request.

**API Key** — A MidWare-generated `mw-...` credential. Clients authenticate to MidWare with these instead of receiving your actual upstream provider keys.

**MSQ** — Model Service Quality. MidWare's synthetic monitoring system. It periodically sends real requests to a model and measures things like TTFT, response time, tokens/sec, stream integrity, and optional quality checks.

**Request** — A request passing through MidWare. Requests can be recorded and inspected from the dashboard, subject to the configured logging behavior.

Yeah, some of this terminology is a little spaghetti. That's why you're reading this section first. :3

## What does MidWare actually do?

The basic flow is:

**Your application → MidWare → Route → LLM provider**

Your application only needs to know about MidWare.

MidWare handles the annoying shit in between.

For example, you can give an application an API key that is allowed to use only certain models:

`mw-xxxxxxxx`

Then your application can request:

`z-ai/glm-5.3-flash`

MidWare checks the key's permissions, determines where that model lives, forwards the request using the private upstream credentials, records whatever should be recorded, and returns the response.

You can also explicitly select a route:

`\[DeepSeek]deepseek-chat`

This allows the same MidWare instance to expose models from multiple providers without requiring every client to know how those providers are configured.

## Features

* OpenAI-compatible `/v1/chat/completions`
* OpenAI-compatible `/v1/models`
* Image generation and embeddings (`/v1/images/*`, `/v1/embeddings`)
* Per-key image response mode: inline base64, or stored on the server and
  returned as `/img/…` URLs with configurable retention (forever by default)
  and an optional HTTPS URL switch
* Multiple upstream routes
* Per-key model and route permissions
* Rate limits and token limits
* Server-side provider API keys
* CORS restrictions
* Streaming responses
* Token accounting
* Request history and request inspector
* Cookie-session protected administration
* Instance claiming during initial setup
* Premodels
* Premodel workflows (chained AI sub-calls)
* SillyTavern preset importing
* Cached upstream model discovery
* MSQ model monitoring
* Long-context needle testing
* Stream integrity and text-forensics checks
* Docker deployment
* SQLite storage
* Tailscale-friendly private deployments

## Premodels

Premodels are MidWare's way of turning a model configuration into another model.

You can define things like:

> `<p>-st-assistant-model` → `z-ai/glm-5.3:thinking`

and configure how the Premodel interacts with the incoming request.

There are currently three merge modes:

* **Premodel wins** — the Premodel configuration takes priority.
* **Caller wins** — incoming request settings take priority.
* **Merge** — MidWare attempts to combine them.

This is useful when you want a client to see a simple model while keeping the actual configuration server-side.

Premodels are chat-only: their base model must be a chat model (the admin form rejects image-generation and embedding ids), and a `<p>-…` prefix on `/v1/images/*` or `/v1/embeddings` is rejected with `403 premodel_chat_only`.

## Workflows

Tick **Use Workflows** on a premodel and the prompt preset is replaced by a
Shortcuts-style chain of AI sub-calls, edited as a vertical stack of cards
(drag the ⠿ handle to reorder, collapse cards with the chevron, add nodes via
the **Add node** picker):

* **Origin** (fixed) — publishes the inbound request as template variables:
  `core_context` (the caller's messages) and `system_request` (method, path,
  headers minus credentials).
* **API Request** nodes (0–8, fully configurable) — each makes a nested,
  non-streaming chat call server-side with its own connection and model. Turn on
  *Expect JSON* and the reply is parsed so every key becomes a variable
  (`{{ analyze.category }}`); the raw text is always `{{ analyze.content }}`.
* **Image Captioner** nodes — grab every image in the caller's messages and
  caption them with a vision model. Either all images in one request (faster) or
  one request per image (more compatible); the captions are exposed as
  `{{ captioner.content }}` and the image count as `{{ captioner.count }}`.
  Requests without images simply skip the node. Prompts (system + caption) are
  tucked behind a **Prompts** button with a sensible default, and the optional
  **Strip images** checkbox removes captioned images from the rest of the chain
  so the final request never sends the same image twice.
* **Environment** nodes — no request, no tokens; they publish variables from the
  server. *Declare Time* exposes `{{ env.time.unix }}` and
  `{{ env.time.formatted("YYYY-MM-DD") }}` (tokens `HH` hour, `MN` minute —
  not month, `MM` is the month — `SE` second, `DD` day, `YY`/`YYYY` year; only
  substituted at non-letter boundaries, so words like
  `SECONDS` stay literal text). The `env` part must match the node's actual
  name — the chips on the card always show the correct prefix. The **Use
  system timezone when formatting is called** checkbox (on by default) picks
  the machine's local timezone; untick it to format in UTC instead. More
  actions will land here over time.
* **Context** nodes — no request, no tokens; they inspect the conversation and
  publish statistics you can use in later prompts: message counts (total,
  user, assistant, images), character count, a flattened `text` transcript, the
  `first_user` / `last_user` / `last_assistant` extracts, and `trimmed` — the
  conversation after applying the node's trim options (*Keep last N messages*,
  *Drop system messages*, *Drop images*). Handy for summarising or shrinking a
  long history before the final call.
* **JavaScript** nodes — a sandboxed JavaScript escape hatch. The card takes a
  script defining `function run(midware)` (async allowed); inside it you get
  `midware.messages`, `midware.vars`, `midware.systemRequest`,
  `midware.set(key, value)` (publishes `{{ node.key }}`), `midware.setMessages(array)`
  to rewrite the conversation for everything downstream, `console.log` (collected
  into node reasoning), `await midware.fetch(url, opts)` — gated by the node's
  **Allow network** checkbox (off by default), http(s) only — and
  `await midware.chat({messages, model, …})`, a server-side model call through
  the node's **Connection** (the upstream key never enters the sandbox and does
  not need Allow network; the model falls back to the node's/default model, and
  token usage is folded into the request row). There is also a built-in
  **`APIRequest`** class you can subclass once at the top level to get an
  OpenAI-like client:
  ```js
  class GLMRequest extends APIRequest {
    constructor() { super(); this.model = "xiaomi/glm-5.3:thinking"; }
  }
  async function run(midware) {
    const glm = new GLMRequest();
    const answer = await glm.ask("Say hi");   // or glm.create({ messages, temperature })
    midware.set("answer", answer);
  }
  ```
   `create(params)` merges your constructor defaults (`this.model`,
   `this.temperature`, `this.max_tokens`) with per-call params and returns the
   full response; `ask(text)` is one-shot sugar. A sibling
   **`APIImageRequest`** class does the same for image generation:
   `create({prompt, model, size, …})` returns the full image response and
   `ask(prompt)` returns the first image's `url` (or `b64_json`), forwarded
   server-side to `…/v1/images/generations` — there is no `midware.image()`,
   the class is the only way in, and the premodel's chat model is never used as
   an image default (premodels are chat-only). The script box is a
   **CodeMirror 6** editor (lazy-loaded from a CDN) that autocompletes the
   sandbox's real surface — `midware.*`, `console.*`, `this.*` inside an
   `APIRequest` subclass, keys inside `chat({…})` / `messages: [{…}]` /
   response literals, member chains like `resp.choices[0].message.`, plus the
   globals that actually exist in the isolate — and falls back to a plain
   textarea if the editor cannot load, so saving
   always works. Runs in a V8 isolate with a
   16 MB memory cap, no filesystem, no access to MidWare secrets, and a 1–30 s
   timeout (default 5); errors surface as `502 workflow_node_failed`.
  `mini-racer` ships in `requirements.txt`, so no extra install is needed.
  Because the script is code rather than a template, saving skips it during
  type checking (like Expect JSON, any dotted path under the node's name is
  accepted).
* **Assure** nodes — guard clauses. Put a templated string on the left, pick an
  operator (equals, contains, starts with, greater than, is empty, …) and put a
  templated string on the right. If the check is false the workflow stops with
  an error before the Final Request; a passed check is logged to the node's
  reasoning as `Passed <name> Assurance Test`.
* **Final Request** (fixed) — builds the message list that is forwarded
  upstream: templated text blocks plus core-context blocks that splice the
  client's original messages. The card shows a block summary; **Edit blocks**
  opens a dialog where you add, reorder and edit blocks (they can pile up
  quickly). Optionally overrides the model.

Turn on **Node reasoning** and any reasoning a sub-call produces is prefixed to
the reasoning the client receives, one logged block per node — also available to
later nodes as `{{ node.reasoning }}`.

Template variables use the familiar `{{ name }}` / `@@name@@` forms. Saving a
workflow type-checks every reference: a path must exist at its position (the
origin variables plus whatever **earlier** nodes publish — the card chips show
the exact prefixes), so typos and forward references are caught in the form
before they can fail at 2am. Nodes with **Expect JSON** on are exempt — their
keys only exist at runtime, so anything dotted under them is accepted (the same
applies to JavaScript nodes, whose variables are only known once the script
runs). Sub-calls
are **not** limited by the calling key's model/provider restrictions (only the
final model is), their token usage is folded into the one request row, and a
failing node aborts with `502 workflow_node_failed` before anything is
forwarded.

## MSQ

MSQ exists because "the model feels kinda slow today" isn't exactly a useful monitoring strategy.

A recorder periodically sends a real request to a configured model and measures it.

It can track:

* Time to first token
* Total response time
* Tokens/sec
* Token count
* Stream integrity
* Repetition and malformed output
* Unexpected scripts/symbols
* Long-context retrieval
* Historical performance

MSQ can therefore tell you that a provider isn't merely responding — it's actually responding *normally*.

Keep in mind that MSQ sends real requests and therefore **uses real provider tokens**.

## System Management Accounts

For automation that needs to *manage* MidWare - creating routes, minting keys, editing MSQ recorders - a plain `mw-...` key is not enough. The System Management API at `/_midware/api/v1` requires every request to be signed with an Ed25519 key, so a stolen token alone cannot be used. The full protocol and a copy-pasteable client live in [SMA.md](SMA.md).

The flow:

1. The client generates an Ed25519 keypair and calls `POST /_midware/api/v1/EnrollSystemAccount` with its public key, a name, a return URL and the permissions it wants. It gets back a pending token and the server's public key.
2. `POST /_midware/api/v1/ActivateSystemAccount` (signed) returns a one-time consent link.
3. The admin opens `/_midware/authorize/<token>`, sees exactly which permissions are requested and where the client will send the user back, then Allows or Denies.
4. `POST /_midware/api/v1/ActivateSystemAccount/exchange` (signed) trades the pending token for a `sma_live_...` token. Every later call to `/_midware/api/v1/interact/*` is signed the same way.

The signature is over a NUL-separated canonical string:

```
"MW-SMA-1" \0 account_id \0 METHOD \0 path(+query) \0 timestamp \0 nonce \0 sha256(body)
```

Sent as `X-Midware-Account`, `X-Midware-Timestamp` (Unix seconds, within `MIDWARE_SMA_NONCE_WINDOW_SECONDS`), `X-Midware-Nonce` (unique per request) and `X-Midware-Signature`, alongside `Authorization: Bearer sma_live_...`. Responses are signed back with the server key under `MW-SMA-RESP-1`.

Permissions are deny-by-default and closed: `midware.read:routes`, `midware.write:routes`, `midware.edit:routes`, `midware.write:apikeys`, `midware.read:apikeysowned`, `midware.edit:apikeysowned`, `midware.read:apikeystatus`, `midware.write:apikeystatus`, `midware.read:msq`, `midware.write:msq`, `midware.edit:msq`. `write` includes delete. Every account can also shut itself off. `read:routes` never returns the upstream API key.

Keys can be switched off without deleting them: a disabled key is recognised and refused with `403 api_key_disabled`, and re-enabling restores it with the same token. The keys page has a toggle, and the `apikeystatus` scopes expose it to automation.

Keys minted through the API are tagged to the account that made them, and cannot be unrestricted: they must name route or model ids.

The whole surface is switched from **Settings → System Management Accounts API**. Revoking an account there takes effect on its next call and deletes the keys it minted. Enrollment is unauthenticated, so it is rate-limited per address and pending requests expire.

If this server is reachable over plain HTTP, signed requests still cannot be forged from a captured token, but an active attacker on the network can relay a request while it is fresh. So the API refuses plain HTTP by default. It answers over HTTPS out of the box; if the instance has no TLS and sits on a network you trust, tick **Allow over plain HTTP** in Settings. Behind a TLS terminator, set `MIDWARE_TRUST_PROXY_HOPS=1` so MidWare sees the original scheme.

## Security

MidWare is designed around the idea that upstream API credentials should stay on the server.

Clients receive `mw-...` keys instead.

The admin interface is separately protected with a browser login at `/signin`, while `/v1/\*` uses MidWare API keys. Sessions last 30 days by default (`MIDWARE_ADMIN_SESSION_DAYS`), survive closing the browser, and are refreshed on each visit. Changing the password signs out every other browser.

For anything exposed beyond a trusted local network, use HTTPS. Over plain HTTP the login password and session cookie are sent to the server unencrypted. When a TLS proxy terminates HTTPS in front of MidWare, set `MIDWARE_SESSION_COOKIE_SECURE=1` so the browser only returns the session cookie over HTTPS.

A private Tailscale deployment is also a very nice way to keep MidWare away from the public internet.

### Security disclaimer

MidWare should *in theory* have the security controls described above, but this project has not gone through a proper security audit or penetration test.

I have not deliberately tried to hack the living shit out of MidWare to prove that those controls actually hold under attack.

So please don't treat MidWare as hardened security software.

If you're using it for anything beyond personal use, especially if it is exposed to the public internet or handling sensitive API credentials, **put it behind a properly configured reverse proxy and add another layer of protection**. HTTPS, access controls, rate limiting, firewall rules, and whatever other security controls make sense for your deployment are strongly recommended.

If you find a security issue, please report it responsibly rather than immediately turning my little middleware into a thermonuclear bomb for someone's api key. ❤️ (ps: i might act awkward in pull requests or sound like a total robot. the latter might be because i'm so spooked that i'm pointing opencode at it lmao)

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
      MIDWARE\_SECRET\_KEY: "${MIDWARE\_SECRET\_KEY:?set MIDWARE\_SECRET\_KEY}"
    volumes:
      - ./appdata:/data
```

The container image is published as:

`ghcr.io/randomboixd/midware:latest`

The `appdata` directory contains MidWare's persistent data, including its SQLite database.

### Setting the secret key

`MIDWARE\_SECRET\_KEY` signs the login session cookie and should be a long, random secret. Don't just use `password123`, your cat's name, or the output of `echo "lol"`. Docker Compose requires it; when it is omitted (for example running `run.py` directly), MidWare generates one once and stores it in the database. Changing it signs out every browser.

Python's `secrets` module can generate one:

```bash
python -c "import secrets; print(secrets.token\_hex(32))"
```

Then put the generated value into your environment:

```bash
export MIDWARE\_SECRET\_KEY="your-generated-secret-here"
```

Or create a `.env` file next to your `compose.yml`:

```dotenv
MIDWARE\_SECRET\_KEY=your-generated-secret-here
```

Keep this secret private. If you change it later, make sure you understand the effects on the existing MidWare instance before doing so.

## Why?

Honestly?

I wanted it.

I wanted a small piece of middleware that could sit between my applications and various LLM providers without turning into an enormous "AI platform" with 47 Docker containers and a Kubernetes cluster just to forward an HTTP request.

So I built one.

Then I kept adding shit.

And now we have MSQ.

## License

MidWare is licensed under the **MIT License**.

See [`LICENSE.md`](LICENSE.md) for the full license text.

If you're reading this because you found the project on GitHub: **hello!** Feel free to poke around, modify it, break it, improve it, or tell me that some part of the architecture is fucking insane.

That's kind of the point of putting it here.

**Made in Hungary**

