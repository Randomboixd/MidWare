# MidWare

A gloriously uninteresting middleware for LLM APIs.

MidWare sits between your application and your LLM providers. It gives you one OpenAI-compatible endpoint, while handling routing, API keys, model permissions, logging, Premodels, token accounting, monitoring, and other fun little pieces of infrastructure that you probably don't want to implement five times.

It's intentionally boring. One container. SQLite. No Kubernetes-powered distributed microservice nightmare. Just middleware.

> **⚠️ This is a vibecoded project.**
>
> A large portion of MidWare was built with **DeepSeek Flash** through OpenCode. I originally picked it because I wanted to test the model properly, and honestly, it looked like a fucking good deal.
>
> At the time of writing, the total DeepSeek API cost of building this thing was roughly **$2.12 USD (~700 HUF)**.
>
> There are absolutely human touches in here, especially around the UI, architecture, security decisions, debugging, and the parts where I had to stop the AI from doing something stupid.
>
> So yes: this is AI-assisted software. Please keep that in mind when reading the code. 

## Terminology

MidWare has a few terms that can get confusing because apparently naming things wasn't enough suffering already.

**Route** — A configured connection to an upstream provider/API. For example, a route might point at DeepSeek's API.

**Model** — The actual model identifier provided by a route, such as `deepseek-chat` or `z-ai/glm-5.3-flash`.

**Host** — The upstream service behind a route. In some parts of the UI/code you'll see these terms used together.

**Premodel** — A server-side model configuration that behaves like a model itself. It can select an underlying model, inject settings/prompts, merge caller configuration, and expose a new model slug such as `<p>-st-assistant-model`.

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
* Multiple upstream routes
* Per-key model and route permissions
* Rate limits and token limits
* Server-side provider API keys
* CORS restrictions
* Streaming responses
* Token accounting
* Request history and request inspector
* Basic Auth protected administration
* Instance claiming during initial setup
* Premodels
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

## Security

MidWare is designed around the idea that upstream API credentials should stay on the server.

Clients receive `mw-...` keys instead.

The admin interface is separately protected with HTTP Basic Authentication, while `/v1/*` uses MidWare API keys.

For anything exposed beyond a trusted local network, use HTTPS. Running Basic Auth over plain HTTP means the credentials are not encrypted in transit.

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
      MIDWARE_SECRET_KEY: "${MIDWARE_SECRET_KEY:?set MIDWARE_SECRET_KEY}"
    volumes:
      - ./appdata:/data
```

The container image is published as:

`ghcr.io/randomboixd/midware:latest`

The `appdata` directory contains MidWare's persistent data, including its SQLite database.

### Setting the secret key

`MIDWARE_SECRET_KEY` should be a long, random secret. Don't just use `password123`, your cat's name, or the output of `echo "lol"`.

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

