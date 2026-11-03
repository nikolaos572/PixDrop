# Security

PixDrop holds two kinds of secrets: the users' tokens, and the providers' keys, which spend
money. Users upload images and send prompts that the server passes on; agents read what
comes back. This page describes what keeps users apart and the keys safe, what is not
protected, and how to [report a vulnerability](#reporting-a-vulnerability).

## Protections

- **Tokens.** Every request to the API but the health check and logging out needs a token
  (or, for a transfer link, its secret), compared in constant time with every known
  token. A token is accepted in the `Authorization` or `X-API-Key` header, in the URL
  (`?token=`), or in the web page's cookie; the first of these that a request
  has decides. The users file is re-read when it changes, so a removed token stops working
  at once.
- **The web page's cookie** is `HttpOnly` (scripts cannot read it), `SameSite=Strict`, and
  `Secure` over HTTPS. Changes made with it require an `X-Requested-With` header, which
  other sites cannot send without the server's consent, and the server gives none (no
  CORS). The MCP server does not accept the cookie at all.
- **Pages** are served with a strict Content Security Policy (scripts, styles and images
  only from the server itself, no plugins, not shown in frames of other sites) and are never
  cached. Every response carries
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
  `Referrer-Policy: no-referrer`.
- **Images** are served with the type recognised from their content, with `nosniff`, and
  with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`:
  an SVG opened in the browser cannot run scripts or load anything.
- **Uploads are checked by content.** A reference image must be a PNG, JPEG, WebP, GIF or
  SVG, recognised from its bytes, not its name; raster images are decoded in full by Pillow,
  which refuses damaged files, at most 2 at a time. Each is limited in size
  (`MAX_UPLOAD_MB`) and pixels (50 megapixels), and a job to 16 of them. Images the provider
  returns are checked the same way before they are stored.
- **Request bodies are bounded.** Every body must announce its size in `Content-Length`
  (chunked requests are refused), and is refused before it is read when it is larger than
  its endpoint allows: 16 × `MAX_UPLOAD_MB` + 1 MB for requests that carry images, 1 MB for
  the MCP server, 64 KB for anything else. Requests that carry images are authenticated
  before their body is read, so no one can upload without a token or a transfer link.
- **SVGs** are parsed without entities (`<!ENTITY ...>` is refused, so no "billion laughs"
  and no external entities) and drawn by CairoSVG in its safe mode, which fetches no
  external file or URL, only `data:` URLs, whose pictures may have at most 50 megapixels.
  An SVG can ask for an enormous amount of drawing in a few bytes (nested `<use>`
  elements), so it is drawn in a helper process (`python -m pixdrop.svgdraw`) with at most
  10 seconds of CPU and of waiting and 2 GB of memory, at most 2 at a time: a hostile SVG is
  refused without holding up the server.
- **Users are kept apart.** Every job is looked up by its user and its id: a job of another
  user gets the same `404` as one that does not exist. File names never come from clients:
  directories are named after the validated user names, files after job ids that the
  server makes and checks, and reference images after a stem reduced to letters, digits and
  `._-`.
- **Nothing is deleted.** No endpoint, tool or button deletes a job or an image, so a
  leaked token cannot destroy a user's history (it can read it, and generate images).
- **Provider keys** are only in `config.json`, or in an environment variable it names
  (`api_key_env`), and must be plain ASCII (a key that is not is refused, without showing it
  in the error). They are sent only to the provider's `base_url`: with each generation,
  and with the server's reads of the provider's model catalogue (`GET
  {base_url}/images/models`, at startup and every 6 hours, which the `python -m pixdrop
  models` command makes too), with the same trust as generating. They are never sent to
  clients (the API and the MCP server show the provider's name only), never with the
  download of an image a provider links to, and never logged. A catalogue only decides
  which of the options PixDrop knows a model offers, with which values (which are then
  checked against every request); it cannot add options of its own.
- **The log** never shows tokens: a `token=` in a URL is logged as `token=***`, and the
  secrets of transfer links as `/api/links/***`. Log lines cannot be faked through
  requests: control characters in what clients send are escaped.
- **The [MCP server](MCP.md)** for AI agents runs the API's code, with its checks. The token
  stays in the agent's MCP client, out of the model; files move through transfer links,
  whose URLs hold a 256-bit random secret instead of the token. A link works only from the
  address that asked for it, for one complete transfer, and for `LINK_EXPIRY_MINUTES`.
  Every refusal gets the same `404`; the server keeps only the secrets' SHA-256 and never
  logs them. It is off unless `ENABLE_MCP=1`.
- **The container** runs as the unprivileged user `app` (uid 1001), never as root, and
  needs no capabilities; the image has an init process (tini).

## What is not protected

- **Plain HTTP.** Without HTTPS, the tokens, the cookie, the transfer links, the prompts and
  the images can be read by anyone who can see the network's traffic. Put a reverse proxy
  with a certificate in front of the service when it is reachable beyond a trusted network.
- **Client addresses from headers.** With `FORWARDED_ALLOW_IPS=*` (the default), PixDrop
  takes the client's address from the first address of `X-Forwarded-For`, else
  `CF-Connecting-IP`, else the connection. Anyone who can reach PixDrop's port without the
  proxy can then claim any address: in the job records, in the log, and for the transfer
  links, which are bound to an address. The same holds behind a proxy that adds to the
  client's own `X-Forwarded-For` instead of replacing it, Cloudflare among them. Publish
  the port only to a proxy that sets `X-Forwarded-For` (`-p 127.0.0.1:8000:8000`, and
  `proxy_set_header X-Forwarded-For $remote_addr;` for nginx), or name the proxies in
  `FORWARDED_ALLOW_IPS`, which makes PixDrop ignore the headers of everyone else; see
  [Reverse proxies](DEPLOYMENT.md#reverse-proxies).
- **Spending.** Anyone with a token can generate images at the provider's expense, as many
  as they like. `MAX_ACTIVE_JOBS` limits how many generations a user runs at the same time,
  not how much they cost. Give tokens only to people you trust with your credits, set a
  spending limit on the provider's key (OpenRouter lets you give each key a credit limit),
  and watch the costs with `python -m pixdrop stats` (see
  [DEPLOYMENT.md](DEPLOYMENT.md#statistics-of-all-users)).
- **Storage.** Nothing is deleted, and there is no storage limit per user: each job adds
  its images and reference images to the disk. Watch the data directory's size, and remove
  old jobs by hand (see [Cleaning up](DEPLOYMENT.md#cleaning-up)).
- **Tokens in URLs.** A token given as `?token=` is kept wherever the URL is: the history
  of browsers and shells, an agent's configuration, the log of a proxy. Prefer the headers.
- **The cookie holds the token itself** for 30 days. Logging out deletes it from the
  browser, but the token stays valid until the administrator changes it.
- **What the providers see.** Prompts and reference images go to the provider (and, through
  OpenRouter, to the company that runs the model), under its terms. PixDrop does not filter
  prompts or images; moderation is the provider's.
- **What reaches agents.** Prompts, the models' text and the providers' messages are read by
  AI agents that use the MCP server, and may contain instructions aimed at them.

## Reporting a vulnerability

Report vulnerabilities privately, with
[Report a vulnerability](https://github.com/nikolaos572/PixDrop/security/advisories/new)
on the repository's Security tab, never in a public issue. Please include:

- what an attacker can do, and what they need (a user's token, or nothing);
- a request, an image or a configuration that shows it, and the steps to reproduce it;
- the PixDrop version or commit, and whether the service runs in Docker or behind a proxy.

The report is confirmed and the fix prepared in private. Once it is released, the issue is
published as a security advisory that credits the reporter (unless they prefer not to be
named). Please keep the issue private until then. Fixes go into the latest version only.

These are vulnerabilities:

- Access to another user's jobs, images or statistics, or to the API without a valid token.
- Reading a provider's key or another user's token through the service.
- A transfer link of the MCP server that works from another address (with a deployment as
  described in [Reverse proxies](DEPLOYMENT.md#reverse-proxies)), after it expired or was
  used up, for another image, or for another user.
- Script injection into the web page, also through an uploaded or generated image, or a
  change another site can make with a user's cookie.
- An upload that makes the server read or write files outside the data directory, or fetch
  a URL (for example through an SVG).
- A way to delete or change another user's jobs, or one's own through the service.
- A way around a configured limit (upload size, jobs in progress) or a
  [fixed limit](CONFIGURATION.md#fixed-limits).

These are not:

- With `FORWARDED_ALLOW_IPS=*`, addresses claimed in `CF-Connecting-IP` or
  `X-Forwarded-For` when PixDrop's port can be reached without the proxy, or behind a proxy
  that keeps the client's own `X-Forwarded-For`: see
  [What is not protected](#what-is-not-protected).
- What a user can do with a valid token: generate images at the provider's expense, and
  fill the disk.
- Tokens or transfer links read off a connection without HTTPS, or from URLs that carry
  `?token=`.
- What the models generate, and what an AI agent does with the prompts and texts it reads.
- Bugs in the providers, in Pillow or in CairoSVG: report them to their maintainers. They
  are a PixDrop vulnerability when they let an upload get past a protection described
  above.
