# MCP server

AI agents such as Claude Code, Codex or Antigravity (`agy`) can use PixDrop by themselves:
an agent connects to `https://<your server>/mcp` with a user's token, finds the operations
PixDrop offers, and generates images, follows their jobs, uploads reference images and saves
the results where you want them. It works with the same jobs as the web page and the API: an
image an agent generates appears on the web page, and the other way round.

[MCP](https://modelcontextprotocol.io) (Model Context Protocol) is the standard way for AI
agents to discover and call a service's operations, called tools.

**Contents**

1. [Turning it on](#turning-it-on)
2. [Connecting an agent](#connecting-an-agent)
3. [How an agent generates images](#how-an-agent-generates-images)
4. [Tools](#tools)
5. [Transfer links](#transfer-links)
6. [Behind a reverse proxy](#behind-a-reverse-proxy)
7. [Troubleshooting](#troubleshooting)
8. [Security](#security)

## Turning it on

The MCP server is off by default, as in TeXdrop. Turn it on with `ENABLE_MCP=1`:

```bash
docker run -d --name pixdrop -p 8000:8000 -v "$PWD/data:/data" \
  -e ENABLE_MCP=1 \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  pixdrop
```

(or `ENABLE_MCP: "1"` under `environment:` in `compose.yaml`, or `ENABLE_MCP=1` in the
environment of `python -m pixdrop`). The server then logs `MCP server at /mcp, transfer
links valid for 60 minute(s)` at startup, and **Server Info** on the web page shows the
address to give to agents, such as `https://pixdrop.example.com/mcp`.

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_MCP` | `0` | `1` serves the MCP server at `/mcp`, and the [transfer links](#transfer-links) under `/api/links/`. Off, both answer `404`. |
| `LINK_EXPIRY_MINUTES` | `60` | A transfer link not used by then stops working. A job waiting for its reference images fails if they have not all arrived this long after it was made. |
| `MEDIUM_IMAGE_PX` | `512` | The longer side of the [medium size](#the-medium-size) that agents can download to look at an image. |

## Connecting an agent

An agent needs the server's address with `/mcp` at the end, and a user's token, which it
sends in the `Authorization` header. It also needs to run shell commands, to run the `curl`
commands the tools give it.

```bash
export PIXDROP_TOKEN='<your token>'
```

**Claude Code**: add the server once, for all your projects:

```bash
claude mcp add --transport http --scope user pixdrop https://pixdrop.example.com/mcp \
  --header "Authorization: Bearer $PIXDROP_TOKEN"
```

or, for one project, in its `.mcp.json`, with the token taken from the environment variable
(so that the file can be shared without it):

```json
{
  "mcpServers": {
    "pixdrop": {
      "type": "http",
      "url": "https://pixdrop.example.com/mcp",
      "headers": {"Authorization": "Bearer ${PIXDROP_TOKEN}"}
    }
  }
}
```

**Codex CLI**, which reads `PIXDROP_TOKEN` from its environment when it starts:

```bash
codex mcp add pixdrop --url https://pixdrop.example.com/mcp --bearer-token-env-var PIXDROP_TOKEN
```

**Antigravity** (`agy`), whose flags come before the name:

```bash
agy mcp add --header "Authorization: Bearer $PIXDROP_TOKEN" pixdrop https://pixdrop.example.com/mcp
```

which writes the server into `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "pixdrop": {
      "disabled": false,
      "headers": {"Authorization": "Bearer <your token>"},
      "serverUrl": "https://pixdrop.example.com/mcp"
    }
  }
}
```

**Other agents** work the same way if they support MCP's Streamable HTTP transport. The
server needs no OAuth. A client that cannot send a header can put the token in the address
instead, as `https://pixdrop.example.com/mcp?token=<your token>`; the server also accepts
it in an `X-API-Key` header. The token in the address ends up wherever the client stores
the address, in plain text.

`claude mcp list` checks the connection (`pixdrop: ... - ✔ Connected`), and `/mcp` in a
session of Claude Code or Antigravity shows the server and its tools.

Then ask the agent, for example:

- *"Use PixDrop to generate a sprite sheet of a knight walking to the right: 4 frames in one
  row, pixel art, transparent background. Save it as `assets/knight-walk.png`."*
- *"Take `assets/knight-front.png` as a reference image and generate the same knight seen
  from the back, from the left and from the right, in the same style, palette and size, with
  a transparent background. Save them next to it as `knight-back.png`, `knight-left.png`
  and `knight-right.png`."*
- *"Which PixDrop models can make transparent backgrounds? Generate a 16:9 title screen for
  the game with the cheapest one, and tell me what it cost."*

## How an agent generates images

The server tells the agent how to use it when it connects, so this happens without
instructions from you:

1. The agent calls `list_models` to choose a model and see the options it accepts, with
   that model's values (a transparent background, SVG output, quality levels, ...), unless
   the server's default model will do.
2. It calls `generate_image` with the prompt and the options. The call waits up to 50
   seconds for the job; while the job is `generating`, the agent calls `get_job`, which waits
   again.
3. A completed job lists `downloads`: for each image, a `curl` command for the full size and
   one for the medium size. The agent runs the one it needs, with the `-o` path changed to
   where the file should go:
   `curl -sS --fail -o assets/knight-walk.png https://pixdrop.example.com/api/links/kQB...`.

With reference images (image to image, or text and image to image):

1. The agent calls `generate_image` with `reference_images` set to their number. The job is
   created as `pending`, with an `upload` command such as
   `curl -sS --fail-with-body -F reference=@REFERENCE_1.png https://pixdrop.example.com/api/links/xCo...`.
2. It runs that command with its files in place of `REFERENCE_1.png`, `REFERENCE_2.png`, ...
   The upload prints the job, which starts generating once all its reference images are in.
3. It calls `get_job` until the job is finished, and downloads the images as above.

A failed job's `error` says why (see [Errors of a job](API.md#errors-of-a-job)); a refused
request (an option the model does not accept, say) comes back as a tool error with the
reason, and the agent tries again.

A completed `generate_image` (or `get_job`) result looks like this:

```json
{
  "id": "1793636061763_4746",
  "status": "completed",
  "prompt": "A sprite sheet of a knight walking, 4 frames, pixel art, transparent background",
  "model": "sunburst",
  "provider": "openrouter",
  "upstream_model": "openai/gpt-image-2.5-sunburst",
  "api_type": "images",
  "options": {"quality": "high", "aspect_ratio": "4:1", "background": "transparent"},
  "source": "mcp",
  "client_ip": "198.51.100.23",
  "created_at": "2026-11-02T18:14:21+02:00",
  "started_at": "2026-11-02T18:14:21+02:00",
  "finished_at": "2026-11-02T18:14:47+02:00",
  "duration_seconds": 25.817,
  "reference_count": 0,
  "references": [],
  "images": [
    {"index": 1, "filename": "1793636061763_4746.png", "content_type": "image/png", "bytes": 802016, "width": 1536, "height": 384, "original_name": null, "url": null, "medium_url": null}
  ],
  "text": null,
  "usage": {"prompt_tokens": 12, "completion_tokens": 1290, "total_tokens": 1302, "cost_usd": 0.04},
  "error": null,
  "upload_url": null,
  "upload": null,
  "downloads": [
    {
      "url": "https://pixdrop.example.com/api/links/kQBrFJbwfiACA4Q3ecNRAhzo0swdarYx7sNXO1OTsG8",
      "method": "GET",
      "expires_at": "2026-11-02T19:14:47+02:00",
      "command": "curl -sS --fail -o 1793636061763_4746.png https://pixdrop.example.com/api/links/kQBrFJbwfiACA4Q3ecNRAhzo0swdarYx7sNXO1OTsG8",
      "image": 1,
      "size": "full"
    },
    {
      "url": "https://pixdrop.example.com/api/links/VdJIJtJe2L16Mq5zLEA8tuk_JOUT9yRlJZ3GyUkg8B8",
      "method": "GET",
      "expires_at": "2026-11-02T19:14:47+02:00",
      "command": "curl -sS --fail -o 1793636061763_4746_medium.png https://pixdrop.example.com/api/links/VdJIJtJe2L16Mq5zLEA8tuk_JOUT9yRlJZ3GyUkg8B8",
      "image": 1,
      "size": "medium"
    }
  ]
}
```

It is the API's [job](API.md#the-job-object), without the API's URLs (which need the token:
`url`, `medium_url` and `upload_url` are `null`), plus `upload` (for a `pending` job: the
link, the method, when it expires, and the `curl` command) and `downloads` (for a
`completed` job: the same for the full and the medium size of each image). Each call that
returns a job makes new links.

### The medium size

Every image can be downloaded in medium size: scaled down to fit 512 × 512 pixels
(`MEDIUM_IMAGE_PX`), in the image's format (PNG for GIFs and SVGs). An agent that wants to
look at an image, to check it before going on, reads the medium size: it costs it far
fewer tokens than the full size, and shows enough to judge the result. The full size is
what it saves for you.

## Tools

Each tool runs the code of an API endpoint, with the same checks, limits and answers
(described in [API.md](API.md)).

| Tool | Does | As the API's |
|---|---|---|
| `server_info` | The server's settings and limits, and your account. | [`GET /api/info`](API.md#get-apiinfo) |
| `list_models` | The models, their descriptions, how many reference images each needs and accepts, and the options each accepts with that model's own values and defaults. | [`GET /api/models`](API.md#get-apimodels) |
| `generate_image` | Generate images from `prompt`, with `model` and the options (`aspect_ratio`, `resolution`, `size`, `quality`, `background`, `output_format`, `output_compression`, `n`, `seed`); `reference_images` announces reference images to upload (default 0); waits for the job for at most `wait_seconds` (default and maximum 50). | [`POST /api/jobs`](API.md#post-apijobs) |
| `upload_link` | A new link to upload the reference images of a pending job (`job_id`), and the `curl` command that does it. | [`POST /api/jobs/{id}/references`](API.md#post-apijobsidreferences) |
| `get_job` | One job (`job_id`, or `last`: your newest job, whatever its status); while it is generating, waits until it finishes, for at most `wait_seconds` (default and maximum 50). A pending job is returned at once. | [`GET /api/jobs/{id}`](API.md#get-apijobsid) |
| `last_job` | Your newest job, whatever its status, as `get_job` gives it; waits for at most `wait_seconds` (default 0). | [`GET /api/jobs/last`](API.md#get-apijobsid) |
| `list_jobs` | Your jobs, newest first, a page at a time (`page`, `per_page`, default 10), without links. | [`GET /api/jobs`](API.md#get-apijobs) |
| `download_link` | A new link to download image `image` (default 1) of a job (`job_id`, or `last`) in `size` `full` (default) or `medium`, or one of its reference images with `reference: true`, and the `curl` command that saves it in the current directory. | [`GET /api/jobs/{id}/images/{n}`](API.md#get-apijobsidimagesn), [`/references/{n}`](API.md#get-apijobsidreferencesn) |
| `get_stats` | Your statistics: counts by status, images, cost in US dollars, tokens and durations, in all, by model and by month. | [`GET /api/stats`](API.md#get-apistats) |

The tools carry hints that agents use to decide when to ask you: `server_info`,
`list_models`, `list_jobs` and `get_stats` only read; the others make links, and
`generate_image` also calls the provider, which costs money. None deletes anything: there
is no delete tool, as there is no delete endpoint. Refusals reach the agent as tool errors
with the API's message, such as `Error executing tool get_job: no such job`.

The tools' arguments are checked by their schema first: `prompt` has 1 to 32,000
characters, `n` is from 1 to 10, `output_compression` from 0 to 100, and job ids look like
`1793636061763_4746`. The options with a list of values (`aspect_ratio`, `resolution`,
`quality`, `background`, `output_format`) are plain strings in the schema, whose
description points to `list_models`: each model has its own values, which follow its
provider's model catalogue (see [CONFIGURATION.md](CONFIGURATION.md#options-from-the-providers-catalogue)).
A value the model does not accept comes back as a tool error, such as `Error executing tool
generate_image: quality must be one of: low, medium, high`, and the agent tries again.

## Transfer links

A tool call carries only text (JSON) that the model writes and reads, so an image cannot be
uploaded, nor saved, through one, and base64 would fill the model's context. The agent moves
images with `curl` instead, through links that the tools hand out. A link's URL holds a
random secret that stands in for the token, for one transfer:

- **Only from the address that asked for it.** The link works only for requests from the
  same IP address as the tool call that made it, normally the computer the agent runs on.
- **For a limited time.** It stops working `LINK_EXPIRY_MINUTES` (60) after it was made. A
  transfer that started in time may end after that.
- **For one complete transfer.** Reference images accepted by a job, or a download of the
  whole file, use the link up. A transfer that fails or is cut off leaves it valid, for
  another try: an upload that is refused (a file that is not an image, too many files) or
  interrupted, or a download cut off before its end. A download link always sends the
  whole image: a `Range` header is ignored. An upload of only some of the
  announced images uses the link too; `upload_link` then gives a new one, whose command
  names the images still expected. A download is complete when the server has sent the
  whole file: behind a reverse proxy, when the proxy has received it, as no server can know
  more.
- **Always renewable.** The agent can ask for new links at any time, as many as it needs
  (`get_job`, `download_link`, `upload_link`): an expired or used link is never a dead end.
- **Kept in memory.** The server keeps the links (by the SHA-256 of their secret) in memory,
  so a restart forgets them; the jobs stay, and the agent asks for new links.

Whatever the reason a link is refused (it does not exist, expired, was used, is used from
another address or with the other method, or its user was removed), the answer is the same
`404`, which tells the agent to ask for a new link:

```json
{"detail": "no such link: links expire, work once and only from the address they were made for; ask for a new one"}
```

The real reason goes to the server's log, for the administrator:

```
2026-11-02 18:14:31 +0200 INFO    10.0.0.4 - GET /api/links/*** 404 2 ms: link for 10.0.0.9 used from 10.0.0.4
2026-11-02 18:14:50 +0200 INFO    10.0.0.9 - POST /api/links/*** 404 1 ms: unknown link
```

The links' endpoints are described in [API.md](API.md#transfer-links-apilinkssecret).

## Behind a reverse proxy

The MCP server needs nothing special from a reverse proxy (nginx, nginx proxy manager,
Caddy, Traefik, ...): it is stateless, and its answers are plain JSON, without event streams
or WebSockets. What matters is what matters for the rest of the service (see
[DEPLOYMENT.md](DEPLOYMENT.md#reverse-proxies)):

- **`PUBLIC_BASE_URL`** should be the address agents connect to, such as
  `https://pixdrop.example.com`, so that the links' URLs start with it.
- **The client's address** binds the links. With `FORWARDED_ALLOW_IPS=*` (the default),
  PixDrop takes it from the first address of `X-Forwarded-For`, else from
  `CF-Connecting-IP`, which anyone who reaches PixDrop's port directly can set: publish the
  port only to the proxy (or name the proxies in `FORWARDED_ALLOW_IPS`), and have the proxy
  set `X-Forwarded-For` to the client's address (nginx:
  `proxy_set_header X-Forwarded-For $remote_addr;`). Without a proxy header, every request
  seems to come from the proxy, and links would work for any client of the proxy.
- **Timeouts and sizes.** The tools answer within 50 seconds, below nginx's default limit
  of 60. Uploads through links carry the reference images: raise the proxy's limit on
  request bodies (`client_max_body_size` for nginx, 1 MB by default) to what agents upload.

## Troubleshooting

**Every link answers `404`.** A link is bound to the exact IP address the tool call came
from, and the agent's MCP client and `curl` must reach PixDrop from the same one. On a host
with both IPv4 and IPv6, they may use different address families (the MCP client connects
over IPv6, say, and `curl` over IPv4), and then no link works for `curl`; the same happens
when they run on different computers, or leave through different proxies or VPNs. The
server's log names both addresses (`link for 2001:db8::5 used from 192.0.2.5`). Make both
use the same route, for example with a host name for PixDrop that has only an IPv4 (or only
an IPv6) address, or by connecting the agent to the same address `curl` uses.

**Tool calls are refused with `401`.** The token is missing or wrong: check the header (or
`?token=`) in the agent's configuration, and that the variable it reads (`PIXDROP_TOKEN`)
is set where the agent starts.

## Security

- **Off unless turned on.** Without `ENABLE_MCP=1`, neither `/mcp` nor `/api/links/` exists.
- **The same token and the same rights.** The agent uses a user's token and can do what that
  user can do through the API, on all their jobs: generate images, at the provider's
  expense, and read every job and image. It cannot delete anything. The endpoint accepts
  the token in the `Authorization` or `X-API-Key` header or in the URL, never the web page's
  cookie, so another web site cannot call it from your browser.
- **The token stays out of the model.** The agent's MCP client adds the header itself: the
  token is not in the tools' descriptions, arguments or results, nor in the commands the
  agent runs. It is stored in the client's configuration file, in plain text: keep that
  file private (and out of version control: use `${PIXDROP_TOKEN}` in `.mcp.json`).
- **Links are short-lived and narrow.** Their secrets have 256 random bits, work only from
  one address, for one transfer of one image (or the reference images of one job), and for
  `LINK_EXPIRY_MINUTES`. The server keeps only their SHA-256, and their secrets never appear
  in its log (`/api/links/***`). Behind a reverse proxy, the address they are bound to is
  only as reliable as the proxy's headers (see [above](#behind-a-reverse-proxy)).
- **Encryption.** Over plain `http://`, the token, the links, the prompts and the images
  can be read by anyone who can see the network's traffic. Use HTTPS (a reverse proxy with
  a certificate) beyond a trusted network.
- **What jobs contain reaches the agent.** The prompts of your jobs (also those made on the
  web page), the models' text and the providers' error messages are read by the agent. Keep
  in mind that they can contain instructions aimed at it, for example in a job someone else
  made with the same token.

See also [SECURITY.md](SECURITY.md).
