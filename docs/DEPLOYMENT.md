# Deployment

How to build and run the service. The users, the models and the settings are described in
[CONFIGURATION.md](CONFIGURATION.md).

**Contents**

1. [Requirements](#requirements)
2. [Building](#building)
3. [Running](#running)
4. [Data directory](#data-directory)
5. [Starting and stopping](#starting-and-stopping)
6. [Statistics of all users](#statistics-of-all-users)
7. [Reverse proxies](#reverse-proxies)
8. [Container limits](#container-limits)
9. [Logs](#logs)

## Requirements

- Linux, with or without Docker. The Docker image takes about 220 MB.
- Outgoing HTTPS to the provider (`openrouter.ai` for OpenRouter): to generate, and to read
  its model catalogue, which gives the models' options.
- An account with the provider and an API key: for OpenRouter, a key from
  [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys), with credits.

The server does no heavy work itself: the models run at the provider. It checks and scales
images, and otherwise waits for answers.

## Building

```bash
docker build --pull -t pixdrop .
```

Rebuild now and then with `--pull`: that brings in the security fixes of Python and of the
system libraries (cairo). The Python packages, Pillow among them, are pinned to exact
versions in `requirements.txt`, so every build installs the same ones; they change with new
versions of PixDrop (see [Updating dependencies](DEVELOPMENT.md#updating-dependencies)).

## Running

The container runs as uid and gid 61001, which must own the data directory (no user or group
with that number needs to exist on the host):

```bash
mkdir data
sudo chown -R 61001:61001 data

docker run -d --name pixdrop -p 8000:8000 -v "$PWD/data:/data" \
  -e TZ=Europe/Athens \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  pixdrop

docker exec -it pixdrop python -m pixdrop setup        # asks for your OpenRouter key, adds the default models
docker exec pixdrop python -m pixdrop adduser alice    # prints alice's token
```

or `docker compose up -d` with [`compose.yaml`](../compose.yaml), and the same two commands
with `docker compose exec pixdrop` instead of `docker exec -it pixdrop`. Until a user exists,
the server refuses every request, and until `setup` has run it has no models.
[CONFIGURATION.md](CONFIGURATION.md) has more on
[the models](CONFIGURATION.md#providers-and-models), including how to add and remove them, and
on [the users](CONFIGURATION.md#users). Everything the service stores
is in the data directory, so the container can be removed and started again from a new
image (`docker rm -f pixdrop`, then the same `docker run`) without losing anything.

Images before November 2026 ran as uid 1001: when you upgrade from one, give the data
directory to 61001 once (the same `sudo chown -R 61001:61001 data`) before you start the new
container. With rootless Docker or `userns-remap`, the host sees the container's ids shifted,
so a `chown` on the host gives the wrong owner; give the directory to 61001 from inside a
container instead:

```bash
docker run --rm --user 0 -v "$PWD/data:/data" pixdrop chown -R 61001:61001 /data
```

`docker run` and compose are equivalent: `compose.yaml` only writes down the same options,
and every setting is an environment variable, which `-e` sets as well. The image has its
own init process (tini), which passes `docker stop` on to the server and reaps processes
left behind in the container, so `--init`, which adds the same, is not needed (it does no
harm either); `--cap-drop ALL` and `--security-opt no-new-privileges` are not needed either (the
server runs as an unprivileged user and needs no capabilities), and add a layer at no cost.
Only the command line (or `compose.yaml`) can give the port, the data directory, the
restart policy (without `--restart`, the container stays stopped after a reboot) and the
[container's limits](#container-limits).

Use HTTPS (a reverse proxy in front of the service) when it is reachable beyond a trusted
network, and set `PUBLIC_BASE_URL` (see [Reverse proxies](#reverse-proxies)).

To let AI agents such as Claude Code use the service, add `-e ENABLE_MCP=1`: see
[MCP.md](MCP.md).

### Without Docker

Install Python 3.11+ and the cairo library (Debian/Ubuntu: `libcairo2`), which draws SVG
reference images. Then, as an unprivileged user (best a dedicated one):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt .   # the versions the image has
mkdir -p ~/pixdrop-data
DATA_DIR=~/pixdrop-data .venv/bin/python -m pixdrop setup            # asks for your OpenRouter key, adds the default models
DATA_DIR=~/pixdrop-data .venv/bin/python -m pixdrop adduser alice    # prints alice's token
DATA_DIR=~/pixdrop-data PORT=8000 .venv/bin/python -m pixdrop
```

Run one process only: the server keeps the jobs in progress, the transfer links and the
waiting requests in memory, so a second process on the same data directory would not see
them.

## Data directory

```
data/
├── config.json                            providers and models (python -m pixdrop setup creates this)
├── users.json                             users and tokens (python -m pixdrop adduser creates this)
├── sessions.json                          the web page's sessions, as SHA-256 digests (the server writes this)
├── outputs/<user>/<id>.json               a job's record: its request, status and results
├── outputs/<user>/<id>.png                its image (.png, .jpg, .webp, .gif or .svg, after the format)
├── outputs/<user>/<id>_2.png              its further images, when it has several (_2, _3, ...)
├── outputs/<user>/<id>_reference/         its reference images: 1_<name>.png, 2_<name>.jpg, ...
├── logs/server.log                        the server's log (older parts in server.log.1 to .4)
└── tmp/                                   uploads in progress, emptied at startup
```

A job id is the time the job was made, in UTC milliseconds, and 4 random hexadecimal
digits, such as `1793635971187_8aee`, so that `ls` lists a user's jobs in the order they
were made. A reference image keeps its uploaded name, reduced to safe characters, after its
number, with the extension of its detected type. For example:

```
data/outputs/alice/
├── 1793635964825_508b.json
├── 1793635964825_508b.png
├── 1793635971187_8aee.json
├── 1793635971187_8aee.png
├── 1793635971187_8aee_reference/
│   ├── 1_knight-front.png
│   └── 2_palette.jpg
├── 1793635981230_0c5f.json
├── 1793635981230_0c5f.webp
└── 1793635981230_0c5f_2.webp
```

The medium size of images is made when it is asked for, and kept only in memory: only the
full-size files are stored.

### The job record

Each job's `<id>.json` is the only record PixDrop keeps of it (there is no database). It is
written as soon as the request is accepted, and replaced at each change of status, always
as a whole (written to a temporary file, then renamed), so that it can be read at any time,
by the server or with `cat`:

```json
{
  "id": "1793635971187_8aee",
  "user": "alice",
  "status": "completed",
  "created_at": "2026-11-02T16:12:51.187Z",
  "started_at": "2026-11-02T16:12:51.187Z",
  "finished_at": "2026-11-02T16:12:51.293Z",
  "duration_seconds": 0.063,
  "source": "api",
  "client_ip": "203.0.113.7",
  "user_agent": "curl/8.14.1",
  "prompt": "The same knight seen from the back, same style and palette",
  "model": "nano-banana",
  "provider": "openrouter",
  "upstream_model": "google/gemini-3.1-flash-image",
  "api_type": "images",
  "options": {"aspect_ratio": "1:1"},
  "reference_count": 2,
  "references": [
    {"index": 1, "filename": "1_knight-front.png", "content_type": "image/png", "bytes": 215, "width": 64, "height": 64, "original_name": "knight-front.png"},
    {"index": 2, "filename": "2_palette.jpg", "content_type": "image/jpeg", "bytes": 8230, "width": 800, "height": 600, "original_name": "palette.jpg"}
  ],
  "images": [
    {"index": 1, "filename": "1793635971187_8aee.png", "content_type": "image/png", "bytes": 12083, "width": 1024, "height": 1024}
  ],
  "text": null,
  "usage": {"prompt_tokens": 612, "completion_tokens": 1290, "total_tokens": 1902, "cost": 0.04},
  "upstream_id": "gen-1793635971-a9F3kQ",
  "error": null
}
```

| Field | Meaning |
|---|---|
| `id`, `user` | The job's id and its user (also the name of the directory). |
| `status` | `pending` (waiting for its reference images), `generating`, `completed` or `failed`. |
| `created_at` | When the request was accepted, in UTC (ISO 8601 with milliseconds). |
| `started_at` | When the generation started: at once, or when the last reference image arrived. `null` before. |
| `finished_at` | When the job became `completed` or `failed`; `null` before. |
| `duration_seconds` | How long the generation took, until the provider's answer (or the failure). `null` until then, and for jobs that never started or were cut off by a restart. |
| `source` | Where the job came from: `web` (the web page), `api` or `mcp` (an AI agent). |
| `client_ip` | The address of the client that made the job (see [Reverse proxies](#reverse-proxies)). |
| `user_agent` | Its `User-Agent` header, at most 300 characters. |
| `prompt` | The prompt, without spaces at either end. |
| `model` | The model's name in `config.json`. |
| `provider`, `upstream_model`, `api_type` | The model's provider, its id there and the API used, as configured when the job was made. |
| `options` | The options sent: the model's defaults and those the request set. |
| `reference_count` | How many reference images the job has, or announced for later. |
| `references` | The reference images received, numbered from 1: file name in `<id>_reference/`, detected type, size in bytes, width and height in pixels, and the name it was uploaded with. |
| `images` | The generated images, numbered from 1: file name, detected type, size, width and height. |
| `text` | Text the model returned with the image (chat models), or `null`. |
| `usage` | What the provider reported: tokens, and `cost` in US dollars. A failed job keeps it when the provider answered without a usable image (no image, or one that is not an image), since the provider may charge for that. `null` when the provider reported nothing, refused the request, did not answer or could not be reached, and while the job is in progress. |
| `upstream_id` | The provider's id of the generation, when it gives one (OpenRouter's `gen-...`), to look it up in the provider's activity log. |
| `error` | Why a failed job failed; otherwise `null`. |

### Cleaning up

PixDrop never deletes anything: no endpoint, tool or button removes a job or an image,
and finished jobs stay forever. (What does expire is a pending job's wait for its reference
images, after which it fails, and the MCP server's transfer links; the job and its files
stay.) The images and records are the users' history, and they grow with every image.
`GET /api/info` shows each user the storage their jobs take, and `du -sh data/outputs/*`
shows it for all users.

To remove a job by hand, delete its files: the record, the images and the reference
directory. For example, for job `1793635971187_8aee` of `alice`:

```bash
cd data/outputs/alice
sudo rm -rf 1793635971187_8aee.* 1793635971187_8aee_*
```

(`<id>.*` is the record and the first image, `<id>_*` the further images and the reference
directory; `-f` passes over those a job does not have.) A job whose
record is gone disappears from the user's list and statistics; images without a record are
ignored. Remove only finished jobs (`completed` or `failed`): a running generation would
write its record again. To remove a whole user, remove them from `users.json`, then their
directory `outputs/<user>`. The server needs no restart for either.

Old jobs can be removed the same way, by the time in their ids. For example, the jobs of
all users made before 1 October 2026, UTC (ids are compared with that moment in
milliseconds):

```bash
cd data/outputs
before=$(date -u -d 2026-10-01 +%s000)
for record in */*.json; do
  id=$(basename "$record" .json)
  if [ "${id%%_*}" -lt "$before" ]; then sudo rm -rf "$(dirname "$record")/$id".* "$(dirname "$record")/$id"_*; fi
done
```

## Starting and stopping

At startup the server checks the settings and the two configuration files, and does not
start if one is invalid (`cannot start: ...` in the log). It empties `tmp/`, and looks at
the jobs left by the last run:

- Jobs that were `generating` become `failed`, with the error `interrupted: the server
  restarted during the generation`. Generations are not started again: submit the job
  again.
- Jobs that were `pending` keep waiting for their reference images, until
  `LINK_EXPIRY_MINUTES` after they were made; those whose time is up become `failed` with
  `the reference images were not uploaded within 60 minute(s)`. Their transfer links,
  which are kept in memory, are gone: agents ask for new ones.

While it runs, the server checks every 30 seconds for pending jobs whose time is up, and
fails them the same way.

It also reads the model catalogue of each provider whose models take their options from it,
in the background: at startup (within a second or two), every 6 hours, and when
`config.json` starts using a provider (see
[CONFIGURATION.md](CONFIGURATION.md#options-from-the-providers-catalogue)). Until the first
read, those models offer no options, and generate with their defaults.

At shutdown (`docker stop`, or Ctrl+C), the server stops taking connections and gives the
requests still open 5 seconds to end; then it cuts them off (a client waiting for a job
with `?wait=` gets `500 Internal Server Error`), stops the running generations and marks
their jobs `failed`, with the error `interrupted: the server stopped during the
generation`. This all happens within the 10 seconds `docker stop` waits. If the server is
killed instead, its generating jobs are failed at the next start, as above.

## Statistics of all users

Users see their own statistics on the web page, through `GET /api/stats` and through the
MCP server. The administrator sees everyone's on the server, from the job records (another
command, `python -m pixdrop models`, lists the providers' models: see
[CONFIGURATION.md](CONFIGURATION.md#options-from-the-providers-catalogue)):

```bash
docker exec pixdrop python -m pixdrop stats                 # a table of all users
docker exec pixdrop python -m pixdrop stats --user alice    # one user, in detail
docker exec pixdrop python -m pixdrop stats --json          # all users, as JSON (add --user for one)
```

```
User       Jobs  Completed  Failed  Active  Images  Cost (USD)  Avg cost  Tokens  Avg tokens  Avg time (s)
---------  ----  ---------  ------  ------  ------  ----------  --------  ------  ----------  ------------
alice        38         26      12       0      29      1.1600    0.0446   40122        1543           1.4
bob           1          1       0       0       1      0.0400    0.0400    1302        1302           0.1
---------  ----  ---------  ------  ------  ------  ----------  --------  ------  ----------  ------------
ALL USERS    39         27      12       0      30      1.2000    0.0444   41424        1534           1.4
```

```
User alice: 38 job(s), 26 completed, 12 failed, 0 in progress
Images 29, reference images 9
Cost $1.1600 ($0.0446 per job)
Tokens 40122 (2712 prompt, 37410 completion), 1543 per job
First job 2026-11-02T18:12:44+02:00, last job 2026-11-02T18:33:52+02:00
Sources api 34, mcp 3, web 1

By model:
  sunburst: 34 job(s), 11 failed, $1.0400
  nano-banana: 4 job(s), 1 failed, $0.1200

By month:
  2026-11: 38 job(s), 12 failed, $1.1600
```

**Active** counts the jobs pending or generating; **Cost** is what the provider reported,
in US dollars, also for failed jobs that it charged; **Avg cost** and **Avg tokens** are
averages over the jobs whose cost and tokens were reported; **Avg time** is the average
duration of the completed jobs. The **ALL USERS** row is computed over the jobs of all
users together. The `--json`
output has the fields of [`GET /api/stats`](API.md#get-apistats) for each user. The
statistics cover the users with a directory in `outputs/`, also users removed from
`users.json` since; `--user` with a name that has no jobs exits with an error. Without
Docker, run `python -m pixdrop stats` with the server's `DATA_DIR` and `TZ`.

## Reverse proxies

Put PixDrop behind a reverse proxy (nginx, Caddy, Traefik, ...) for HTTPS. Four things
matter:

- **The address.** Set `PUBLIC_BASE_URL` to the address users reach, such as
  `https://pixdrop.example.com`, so that the URLs in jobs, the MCP server's address in Server
  Info and the transfer links point there, whatever the proxy sends as `Host`, and so that
  browsers may send changes (a login, a generation) only from pages at exactly that address.
  Without it, they may from pages of the host the request was sent to, which the proxy must
  then pass on (nginx: `proxy_set_header Host $host;`). PixDrop must
  be served at the root of its address (a domain or subdomain of its own): the web page
  uses paths such as `/api/jobs`, so it cannot live under a path like
  `https://example.com/pixdrop/`, and a `PUBLIC_BASE_URL` with a path stops the server at
  startup.
- **The client's address.** It is recorded with each job, logged, and binds the MCP
  server's transfer links to the agent's computer. PixDrop takes it from the proxy's
  `X-Forwarded-For` header only in requests from the proxies that `FORWARDED_ALLOW_IPS`
  names, and from the connection otherwise. The default, `127.0.0.1`, names a proxy on the
  same machine when PixDrop runs without Docker. A container sees its proxy at another
  address, so until you name the proxy, every request seems to come from it: the log shows
  the proxy's address on every line. Set `FORWARDED_ALLOW_IPS` to that address:
  - for a proxy on the same machine that forwards to the published port, usually
    `172.17.0.1`, the gateway of Docker's default network;
  - for a proxy in a container on the same Docker network, that network's subnet, such as
    `172.18.0.0/16` (see `docker network inspect <network>`), since the proxy container's
    own address can change.

  uvicorn then takes the last address of `X-Forwarded-For` that is not one of the proxies,
  and ignores the headers of every other client, so that no one can claim another address.
  Separate several proxies or networks with commas; an entry that is not an IP address or
  network stops the server at startup instead of being ignored, and the startup log says
  whose `X-Forwarded-For` is used.

  `*` trusts every peer instead: the client's address is then the first address in
  `X-Forwarded-For`, else Cloudflare's `CF-Connecting-IP` header, else the connection's.
  Anyone who can reach PixDrop's port directly can then claim any address, so use it only
  when the port can be reached only through the proxy (`-p 127.0.0.1:8000:8000`, or no `-p`
  with the proxy on the same Docker network), and have the proxy set `X-Forwarded-For` to the
  client's address rather than add to the one the client sent (nginx: `$remote_addr`, not
  `$proxy_add_x_forwarded_for`), since the first address of the list is the one used.
- **The scheme.** The proxies of `FORWARDED_ALLOW_IPS` are also trusted for
  `X-Forwarded-Proto`: the server then knows that the client used HTTPS, marks the web
  page's cookie `Secure`, and, without `PUBLIC_BASE_URL`, hands out `https://` URLs.
- **Timeouts and sizes.** A request waiting for a job (`?wait=`) stays open until the job
  finishes; keep `wait` below the proxy's timeout (nginx: 60 seconds by default), as the MCP
  server does (50 seconds). Uploads of reference images can be large: raise the proxy's
  limit on request bodies (nginx: `client_max_body_size`, 1 MB by default) to what users
  upload at once, up to PixDrop's own limit of 16 × `MAX_UPLOAD_MB` + 1 MB. PixDrop
  refuses chunked request bodies (`411`): the proxy must pass bodies on with their
  `Content-Length`, as nginx does.

For example, with nginx on the same machine:

```nginx
server {
    listen 443 ssl;
    server_name pixdrop.example.com;
    # ssl_certificate and ssl_certificate_key ...

    client_max_body_size 321m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

```bash
docker run -d --name pixdrop -p 127.0.0.1:8000:8000 -v "$PWD/data:/data" \
  -e TZ=Europe/Athens -e PUBLIC_BASE_URL=https://pixdrop.example.com \
  -e FORWARDED_ALLOW_IPS=172.17.0.1 \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  pixdrop
```

Publishing the port on `127.0.0.1` only also keeps anyone from going around the proxy.

Behind Cloudflare, set `FORWARDED_ALLOW_IPS` to Cloudflare's address ranges
([cloudflare.com/ips](https://www.cloudflare.com/ips/), comma-separated), so that the address
Cloudflare adds to `X-Forwarded-For` is the one used; or put a proxy of your own behind
Cloudflare, which accepts connections only from Cloudflare and sets `X-Forwarded-For` from
its `CF-Connecting-IP` header (nginx: `proxy_set_header X-Forwarded-For
$http_cf_connecting_ip;`), and name that proxy. `*` is not enough there: Cloudflare adds the
client's address at the end of an `X-Forwarded-For` the client sent, so the first address
can be anything.

## Container limits

Docker can limit the whole container: its memory, CPU and number of processes. These are
`docker run` options (or `compose.yaml` keys), not environment variables. By default there
are none, and the container may use the whole machine.

| `docker run` | `compose.yaml` | Limits |
|---|---|---|
| `--memory 1g --memory-swap 1g` | `mem_limit: 1g` and `memswap_limit: 1g` | Memory in use (and memory plus swap: the same value means no swap) |
| `--cpus 2` | `cpus: 2` | CPU cores |
| `--pids-limit 256` | `pids_limit: 256` | Processes and threads |

For example:

```bash
docker run -d --name pixdrop -p 8000:8000 -v "$PWD/data:/data" \
  --memory 1g --memory-swap 1g --cpus 2 --pids-limit 256 \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  pixdrop
```

The server is one process, which uses about 100 MB when idle. Memory goes mostly to the
images it handles: decoding a picture to check or scale it takes about 4 bytes per pixel
(200 MB for a 50-megapixel picture; at most 2 medium sizes are made at a time, and those
kept take at most 64 MB), and a generation keeps its reference images and the provider's
answer in memory while it runs. SVGs are drawn by helper processes, at most 2 at a time,
each allowed 2 GB and 10 seconds. Size the limit after the number of generations that run
at once (at most `MAX_ACTIVE_JOBS` per user) and the size of their images. When the memory
runs out, the kernel stops the server, and Docker starts the container again if it has a
restart policy; the generations that were running fail (at the next start). The CPU is
used in short bursts, to check, scale and draw images.

## Logs

The server logs to its console, which `docker logs pixdrop` shows (without Docker: the
terminal, or the journal of the service manager), and to `logs/server.log` in the data
directory, which stays when the container is replaced. At 10 MB the file becomes
`server.log.1`, the previous `.1` becomes `.2`, and so on; files beyond `server.log.4` are
deleted, so the log takes at most 50 MB.

Each line has the time (in the `TZ` time zone), the level and one event:

```
2026-11-02 18:12:23 +0200 INFO    PixDrop 1.0.0 starting, data in /data
2026-11-02 18:12:23 +0200 INFO    loaded the users file /data/users.json
2026-11-02 18:12:23 +0200 INFO    loaded the models file /data/config.json
2026-11-02 18:12:23 +0200 INFO    5 model(s) configured, default sunburst
2026-11-02 18:12:23 +0200 INFO    MCP server at /mcp, transfer links valid for 60 minute(s)
2026-11-02 18:12:24 +0200 INFO    read the model catalogue of openrouter (61 image models): options and limits for 5 model(s)
2026-11-02 18:12:51 +0200 INFO    10.0.0.5 alice POST /api/jobs 201 9 ms
2026-11-02 18:12:51 +0200 INFO    job 1793635971187_8aee (alice): nano-banana, completed in 6.3 s, 1 image(s), $0.0400
2026-11-02 18:13:07 +0200 INFO    10.0.0.5 alice GET /api/jobs/1793635971187_8aee/images/1 200 3 ms
2026-11-02 18:13:35 +0200 INFO    10.0.0.5 alice POST /api/jobs 400 4 ms: model 'icons' does not support 'background'; its options: quality
2026-11-02 18:13:35 +0200 INFO    job 1793636015798_104d (alice): sunburst, failed after 0.9 s: openrouter refused the request (HTTP 402): Insufficient credits. Add more using https://openrouter.ai/credits
2026-11-02 18:14:07 +0200 INFO    10.0.0.7 - POST /api/session 401 2 ms: invalid token
2026-11-02 18:14:21 +0200 INFO    10.0.0.9 alice POST /mcp 200 53 ms
2026-11-02 18:14:31 +0200 INFO    10.0.0.9 alice GET /api/links/*** 200 30 ms
2026-11-02 18:14:50 +0200 INFO    10.0.0.9 alice POST /mcp?token=*** 200 9 ms
2026-11-02 18:14:50 +0200 INFO    10.0.0.4 - GET /api/links/*** 404 2 ms: link for 10.0.0.9 used from 10.0.0.4
```

- Starting and stopping: the version, the configuration files loaded, the models, the MCP
  server, the jobs failed or still pending after a restart, and why the server does not
  start when a setting or a file is not right. A configuration file changed while the
  server runs is logged as loaded, or as ignored with the reason.
- One line per request that changes something (a generation, an upload, a login, each call
  of the MCP server), downloads an image in full size or through a transfer link, or fails:
  the client's address, the user (`-` when unknown), the request, its status and duration,
  and why it failed. A refusal by an MCP tool is logged with status `200` (the protocol's
  answer) and the reason. Other successful reads are not logged, nor are medium-size
  downloads through the API: the web page repeats its reads every few seconds and loads the
  medium-size preview of every job it shows, and Docker repeats its health check every 30
  seconds. Behind a reverse proxy, the address is the proxy's until `FORWARDED_ALLOW_IPS`
  names it (see [Reverse proxies](#reverse-proxies)).
- One line per finished job: its model, the time the generation took, the number of images
  and the cost, or the error.
- The providers' model catalogues: how many models each lists and how many configured
  models take their options from it, the configured models it does not list (a warning),
  the parameters of a model that PixDrop does not offer, and reads that failed (a warning,
  with the next attempt in 5 minutes).
- Unexpected errors, at level `ERROR` and with their traceback.

Tokens never appear in the log: a token in a URL is logged as `token=***`, and the secrets
of the MCP server's transfer links as `/api/links/***` (with the reason when a link is
refused). Nor do the providers' keys. Prompts are not logged; they are in the job records.
