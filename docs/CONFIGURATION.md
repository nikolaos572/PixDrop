# Configuration

Two files in the data directory hold the configuration, both managed with commands:
`users.json` ([the users](#users) and their tokens) and `config.json`
([the providers and models](#providers-and-models)). Everything else is an environment
variable. How to run the service is in [DEPLOYMENT.md](DEPLOYMENT.md).

**Contents**

1. [Users](#users)
2. [Providers and models](#providers-and-models)
3. [Environment variables](#environment-variables)
4. [Fixed limits](#fixed-limits)

## Users

Two commands manage the users. Run them on the running container, at any time, without a
restart:

```bash
docker exec pixdrop python -m pixdrop adduser alice       # adds the user and prints their token
docker exec pixdrop python -m pixdrop reset-token alice   # gives the user a new token and prints it
```

With Docker Compose, they start with `docker compose exec pixdrop` instead of
`docker exec pixdrop`. Without Docker, set the same environment as the server's:
`DATA_DIR=~/pixdrop-data .venv/bin/python -m pixdrop adduser alice`.

A user name is 1-32 of `a-z 0-9 _ -`, starting with a letter or a digit, and is also the
name of the user's directory. All users are equal: each one sees only their own jobs, images
and statistics, and there is no administrator account (the administrator sees every user's
statistics on the server, see [DEPLOYMENT.md](DEPLOYMENT.md#statistics-of-all-users)).

A token is what its user logs in with on the web page, and what their scripts and agents
send. `reset-token` is for a token that is lost or leaked: the old token stops working at
once, and every browser logged in with it is logged out, so the user has to log in again and
to give the new one to their scripts and agents. Both commands print the token alone on
standard output and their message on standard error, so a script can write `token=$(docker exec pixdrop python -m pixdrop adduser alice)`. They
exit with status 1, and change nothing, if the user already exists (`adduser`) or does not
(`reset-token`), if the name is invalid, or if the file is invalid or cannot be written.

To remove a user, delete their line from the file (see below). Their jobs stay in
`outputs/<user>` until you [remove them too](DEPLOYMENT.md#cleaning-up): a new user added
later with the same name would see them.

### The users file

The commands keep the users in `data/users.json` (the `USERS_FILE` setting), which you can
also edit by hand (see [`users.example.json`](../users.example.json)):

```json
{"users": {"alice": "<token for alice>", "bob": "<token for bob>"}}
```

A token is 16-256 letters, digits or `._~+/=-`; make one with `openssl rand -hex 32`, as the
commands do. Two users cannot share a token.

The file is re-read when it changes, so users can be added or removed without a restart; a
removed user's token stops working at once. An invalid file stops the server at startup;
an invalid edit while it runs is ignored (with an error in the log), and the previous
content stays in use. Without the file the server starts, logs a warning, and refuses every
request.

The file `adduser` creates can be read only by its owner, as it holds secrets: the
container's user, uid 61001, so use `sudo` to edit it on the host. A file that already exists
keeps its mode. It also keeps its owner if that is the user running the command, or if the
command runs as root; otherwise the user running the command becomes its owner.

The format is that of [TeXdrop](https://github.com/nikolaos572/TeXdrop)'s `users.json`, so
one file can serve both services: point `USERS_FILE` at it, or mount it as
`/data/users.json`:

```bash
docker run ... -v /srv/texdrop/data/users.json:/data/users.json:ro ... pixdrop
```

(Docker mounts the file itself: an editor that saves by writing a new file and renaming it
leaves the container with the old one until it restarts.) The commands keep whatever else
the file holds. They cannot change a file mounted with `:ro`, and say so: add the user where
the file comes from, or mount it without `:ro`, for uid 61001 to write it.

## Providers and models

Two commands manage the models. Run them on the running container, in a terminal (hence
`-it`), at any time, without a restart:

```bash
docker exec -it pixdrop python -m pixdrop setup           # asks for your OpenRouter key, adds the default models
docker exec -it pixdrop python -m pixdrop modify-models   # a checklist to add and remove models
```

With Docker Compose, they start with `docker compose exec pixdrop` instead of
`docker exec -it pixdrop`. Without Docker, set the same environment as the server's:
`DATA_DIR=~/pixdrop-data .venv/bin/python -m pixdrop setup`.

**`setup`** asks for the key (without showing it), asks OpenRouter whether it accepts the key,
and reads OpenRouter's catalogue to check that it still offers every default model. Only
then does it write `config.json`, with the default models of
[`default_config.json`](../src/pixdrop/default_config.json), and it shows what each one
accepts. If anything is wrong (the key is refused or is a management key, which cannot
generate images, a default model is gone, OpenRouter cannot be reached), it says so, exits
with status 1, and writes nothing. A script can pipe the key:
`docker exec -i pixdrop python -m pixdrop setup < key.txt`.

Running `setup` again **replaces the whole file**: the old key, every other provider and
every model you added are deleted, and the default models come back with the new key. It says
so before asking for the key, and until you give a key that works the old file stays. Use it
to change the key, or to start over.

**`modify-models`** reads the catalogue of every provider in the file and shows a checklist,
which starts with the models you have: arrows move, space ticks or unticks, typing searches,
enter saves and Ctrl-C cancels. A model you add gets its name from the end of its id
(`openai/gpt-image-2` becomes `gpt-image-2`, or the whole id if that name is taken) and its
description from the catalogue. The models you keep stay exactly as they are, and the key is
not touched. A model you have that its provider no longer offers is marked, so that you can
remove it. It then asks for the default model, if more than one is left. It needs a terminal,
and a file to change: run `setup` first.

Neither command writes a model's options: the server reads them from the catalogue, so that
they follow what the provider offers.

### The configuration file

`data/config.json` says which image models users can choose, and how to reach them. You can
also edit it by hand, or write it yourself: `setup` is not needed then, and
[`default_config.json`](../src/pixdrop/default_config.json) is a good start. For each model
it names the provider, the provider's id of the model and the API to call; what the model
accepts (its options, their values, how many reference images) PixDrop reads from the
provider's model catalogue:

```json
{
  "providers": {
    "openrouter": {
      "api_key": "sk-or-v1-<your-OpenRouter-key>",
      "headers": {"X-OpenRouter-Title": "PixDrop"}
    }
  },
  "default_model": "sunburst",
  "models": {
    "sunburst": {
      "provider": "openrouter",
      "model": "openai/gpt-image-2.5-sunburst",
      "api_type": "images",
      "description": "OpenAI GPT Image 2.5 Sunburst: precise, follows instructions closely, transparent backgrounds for sprites",
      "defaults": {"quality": "high"}
    },
    "qwen": {
      "provider": "openrouter",
      "model": "qwen/qwen-image-3",
      "api_type": "images",
      "description": "Qwen Image 3: sharp text and small details in images; up to 3 reference images"
    },
    "vector": {
      "provider": "openrouter",
      "model": "recraft/recraft-v4.1-vector",
      "api_type": "images",
      "description": "Recraft V4.1 Vector: SVG icons and logos"
    }
  }
}
```

The file has three keys, `providers`, `models` and `default_model`. An unknown key, in
the file, a provider or a model, is an error, so that a misspelt setting is not silently
ignored.

### Providers

A provider is an API that generates images the way [OpenRouter](https://openrouter.ai)
does: OpenRouter itself, or another service with the same endpoints. Its name (the key in
`providers`, such as `openrouter`) is 1-64 of `A-Z a-z 0-9 . _ -`; models refer to it, and
jobs and their errors show it.

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `https://openrouter.ai/api/v1` | The API's address, `http://` or `https://`. PixDrop calls `{base_url}/images` and `{base_url}/chat/completions` to generate, and reads the model catalogue at `{base_url}/images/models`. |
| `api_key` | | The provider's key, sent as `Authorization: Bearer <key>` with every request to it: visible ASCII characters, without spaces. |
| `api_key_env` | | Instead of `api_key`: the name of an environment variable that holds the key (such as `OPENROUTER_API_KEY`, set with `-e` on the `docker run` command line or under `environment:` in `compose.yaml`), so that the file need not contain it. If the variable is not set, the file is invalid. |
| `headers` | `{}` | Extra HTTP headers sent with every request to the provider, such as OpenRouter's optional attribution headers `HTTP-Referer` (your site's URL) and `X-OpenRouter-Title` (its name), which identify your app to OpenRouter. Names are `A-Z a-z 0-9 -`, values printable ASCII. |

Give exactly one of `api_key` and `api_key_env`. A key with other characters (a stray
line break or a typographic quote, say) makes the file invalid; the error message never
shows the key. The key is used only for the provider's requests: it is never shown by the
API, the MCP server or the web page, and never logged. To keep it out of the file:

```json
"providers": {"openrouter": {"api_key_env": "OPENROUTER_API_KEY"}}
```

```yaml
    environment:
      OPENROUTER_API_KEY: sk-or-v1-...       # or: docker run ... -e OPENROUTER_API_KEY=sk-or-v1-...
```

### Models

A model's name (the key in `models`, such as `sunburst`) is what users choose: 1-100 of
`A-Z a-z 0-9 . _ : / @ + -`, starting with a letter or a digit. The upstream model id
may serve as the name too (`"qwen/qwen-image-3": {...}`).

| Key | Default | Meaning |
|---|---|---|
| `provider` | (required) | The name of one of the `providers`. |
| `model` | (required) | The provider's id of the model, such as `openai/gpt-image-2.5-sunburst`. |
| `api_type` | (required) | `"images"` or `"chat"`: which of the provider's APIs generates with this model. Use `"images"` (see [Images or chat](#images-or-chat)). |
| `description` | `""` | Shown to users and agents when they choose a model. |
| `options` | (the provider's) | Left out: the options the [provider's catalogue](#options-from-the-providers-catalogue) lists for the model, with its own values. A list of [option](#options) names instead fixes which ones users may set, with PixDrop's values; others are refused. `[]`: none. |
| `defaults` | `{}` | Values for some options, used when a request does not set them. Without a default, an option a request leaves out is not sent, and the provider decides. With the provider's options, a default the model does not accept is dropped. |
| `max_reference_images` | (the provider's) | Left out: the number of reference images the provider's catalogue gives for the model, from its minimum (some models only edit images and need at least one) to its maximum, at most 16. A number from 0 (text to image only) to 16 instead fixes the maximum, and the minimum is 0. |
| `modalities` | `["image", "text"]` | Only for `api_type: "chat"`: the output the model is asked for, `["image", "text"]` or `["image"]` (for a model that produces only images, if it refuses the default). |
| `request` | `{}` | Extra fields merged into every request body sent for this model (see [Extra request fields](#extra-request-fields)). |

`default_model` is the model used when a request names none. It must be one of the
`models`; without it, the first model in the file is the default.

### Options from the provider's catalogue

OpenRouter lists every image model with the parameters it accepts and their values, in its
model catalogue (`GET https://openrouter.ai/api/v1/images/models`). A model configured
without `options` offers exactly those of its parameters that PixDrop knows (the
[options](#options) below), with the model's own values and ranges; without
`max_reference_images`, its reference images follow the catalogue too. For example:

| Model | Options it offers | Reference images |
|---|---|---|
| `openai/gpt-image-2.5-sunburst` | `aspect_ratio` (`1:1`, `3:2`, `2:3`, `4:3`, `3:4`, `16:9`, `9:16`, `21:9`, `auto`), `quality` (`auto`, `low`, `medium`, `high`, `xhigh`, `max`), `background` (`auto`, `transparent`, `opaque`), `output_compression` (0-100), `n` (1-10) | 0-16 |
| `qwen/qwen-image-3` | `resolution` (`1K`, `2K`), `seed`, ... | up to 3 |
| `recraft/recraft-v4.1-vector` | `output_format` (`svg` only), ... | |
| `krea/k3-edit` | ... | 1-8: it only edits images |

So each model offers what it really accepts, with nothing to look up or type: a value the
model gains later is accepted as soon as the catalogue lists it, and an option that leaves
no choice (`n` from 1 to 1) is not offered. A request for a value the model does not have is
refused before anything is sent (`quality must be one of: low, medium, high`), and so is a
job with fewer reference images than the model needs (`model 'edit' needs at least 1
reference image(s), got 0`). `GET /api/models`, the MCP server's `list_models`, the web
page's form and Server Info show each model's own options and values.

**When it is read.** The server reads the catalogue of each provider that a model relies on
at startup (in the background, within a second or two), then every 6 hours, and within 30
seconds when `config.json` starts using a provider, or its address or key changes. A read
that fails is tried again after 5 minutes, and the last catalogue read stays in use
meanwhile. The log says what came of it:

```
2026-11-03 02:21:22 +0200 INFO    read the model catalogue of openrouter (61 image models): options and limits for 5 model(s)
2026-11-03 02:21:22 +0200 WARNING model 'old' (vendor/retired-model) is not in the catalogue of openrouter: it offers no options; name them with "options" in config.json
2026-11-03 08:21:23 +0200 WARNING cannot read the model catalogue of openrouter (keeping the one read before): HTTP 503; retrying in 5 minutes
```

and, for a model whose entry lists parameters PixDrop does not know, `model 'NAME' (ID)
also accepts PARAMETERS, which PixDrop does not offer ("request" in config.json can set
them)`.

Until the catalogue has been read, or when the model is not in it (a misspelt id, a model
the provider has retired, a provider without a catalogue), such a model offers no options:
it still generates, with its `defaults`, and takes up to 16 reference images.
`GET /api/models` says where each model's options come from (`options_from`: `provider`,
`config` or `pending`).

**Parameters PixDrop does not know** (a model's own `style`, say, or a parameter added
after this version) are not offered to users: the log names them, and `request` can set
them for every job of the model.

**Fixing the options yourself.** List `options` to choose which options users may set (the
others are refused), with the values of PixDrop's catalogue below, whatever the provider
says; set `max_reference_images` to fix the number of reference images. Do that for a
provider without a model catalogue, or to offer less than a model accepts.

**Finding models.** `python -m pixdrop models` reads the catalogues of the providers in
`config.json` and lists their image models, with the reference images and options each
accepts:

```bash
docker exec pixdrop python -m pixdrop models               # every model, one line each
docker exec pixdrop python -m pixdrop models sunburst      # the models whose id or name contains "sunburst", in detail
docker exec pixdrop python -m pixdrop models qwen --json   # the catalogue's own entries
```

```
openrouter: 61 image models, 1 matching 'sunburst'
  openai/gpt-image-2.5-sunburst  [configured as 'sunburst']
      OpenAI: GPT Image 2.5 Sunburst: GPT Image 2.5 Sunburst is an image generation and editing model from OpenAI, ...
      reference images: 0-16
      aspect_ratio: 1:1 3:2 2:3 4:3 3:4 16:9 9:16 21:9 auto
      quality: auto low medium high xhigh max
      background: auto transparent opaque
      output_compression: 0-100
      n: 1-10
```

The detail also lists the parameters PixDrop does not offer, and, for a model not yet in
`config.json`, a line to copy into its `models`:

```
      config.json: "qwen-image-3-pro": {"provider": "openrouter", "model": "qwen/qwen-image-3-pro", "api_type": "images"}
```

Without Docker, run `python -m pixdrop models` with the server's `DATA_DIR` (or
`CONFIG_FILE`), and the variables `api_key_env` names.

### Images or chat

Providers like OpenRouter generate images through two APIs. `api_type` says which one
PixDrop calls. **Use `"images"`**: every image model of OpenRouter is on its Images API,
which takes the options under their own names and lists them in the catalogue. `"chat"`
remains for providers that generate images only through chat completions.

| `api_type` | Endpoint | Request | Images come back |
|---|---|---|---|
| `"images"` | `POST {base_url}/images` | `model`, `prompt`, the options at the top level under their own names, and the reference images in `input_references` | base64 in `data[].b64_json` (or links in `data[].url`) |
| `"chat"` | `POST {base_url}/chat/completions` | `model`, `modalities`, one user message with the prompt and the reference images, the options in `image_config` (`resolution` as `image_size`) and `seed` at the top level | `data:` URLs (or links) in `choices[0].message.images[]`, with the model's text in `message.content` |

For example, a job with `background=transparent`, `n=2` and one reference image sends this
body for an `images` model with the default `quality` `high`:

```json
{
  "model": "openai/gpt-image-2.5-sunburst",
  "prompt": "A knight, pixel art",
  "quality": "high",
  "background": "transparent",
  "n": 2,
  "input_references": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}}]
}
```

and a job with `resolution=2K`, `aspect_ratio=1:1` and `seed=7` this body for a `chat`
model:

```json
{
  "model": "google/gemini-3.1-flash-image",
  "modalities": ["image", "text"],
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "A knight, pixel art"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}}
  ]}],
  "image_config": {"image_size": "2K", "aspect_ratio": "1:1"},
  "seed": 7
}
```

Without reference images, the message's `content` is the prompt itself. Reference images
are sent as `data:` URLs in their own format; GIFs (their first frame) and SVGs (drawn
2048 pixels on their longer side) are sent as PNG, since providers do not take those
formats. Either way the provider's answer gives the usage: tokens, and the cost in US
dollars (OpenRouter's `usage.cost`), which PixDrop records with the job. An error status,
or an error inside an answer with status `200` (OpenRouter reports errors that happen
during a generation that way), fails the job with the provider's message.

### Options

These are the options PixDrop knows. A model with the provider's options offers those of
them that its catalogue entry lists, with the model's own values (which may be fewer, or
new ones); a model with `options` in `config.json` offers the listed ones with the values
below. PixDrop checks every value, the same way for the web page, the API and the MCP
server, before anything is sent.

| Option | Values | Meaning |
|---|---|---|
| `aspect_ratio` | `1:1`, `1:2`, `1:4`, `1:8`, `2:1`, `2:3`, `2.35:1`, `3:2`, `3:4`, `4:1`, `4:3`, `4:5`, `5:2`, `5:4`, `5:7`, `7:5`, `8:1`, `9:16`, `16:9`, `9:19.5`, `19.5:9`, `9:20`, `20:9`, `9:21`, `21:9`, `auto` | Width to height. Providers use the nearest ratio they support. |
| `resolution` | `512`, `768`, `1K`, `1.5K`, `2K`, `4K` | Resolution tier. Sent as `image_size` to `chat` models. |
| `size` | `auto`, or `WIDTHxHEIGHT` with 2-5 digits each, such as `1024x1024` | Exact size in pixels. |
| `quality` | `auto`, `low`, `medium`, `high`, `xhigh`, `max` | Rendering quality. |
| `background` | `auto`, `transparent`, `opaque` | Background. `transparent` needs `output_format` `png` or `webp`: with `jpeg` the request is refused. |
| `output_format` | `png`, `jpeg`, `webp`, `svg` | File format of the image. `svg` is for vector models: the image is stored as `<id>.svg`, served like any other (an SVG opened in a browser cannot run scripts), and its medium size is a PNG. |
| `output_compression` | integer, 0 to 100 | Compression of `jpeg` and `webp` images. |
| `n` | integer, 1 to 10 | Number of images to generate in one job. Only for `api_type: "images"`. |
| `seed` | integer, 0 to 4294967295 | Seed, for repeatable results where the model supports it. |

Enumerated values are accepted in any case (`HIGH` is `high`). `GET /api/models` and the
MCP server's `list_models` give users each model's options with their allowed values and
defaults, and the web page shows them as fields.

### Extra request fields

`request` is merged into the body of every request for the model, recursively: objects
are merged key by key, and other values replace what PixDrop would send. It passes on
parameters that PixDrop has no option for, such as a model's own parameters (which the log
names) or OpenRouter's provider routing:

```json
"sunburst": {
  "provider": "openrouter", "model": "openai/gpt-image-2.5-sunburst", "api_type": "images",
  "request": {"provider": {"order": ["openai"], "allow_fallbacks": false}}
}
```

Its values win over the options: a model with `"request": {"background": "transparent"}`
always asks for a transparent background, whatever a user chooses, while the job still
lists the user's choice. Use `defaults` for options, and `request` for the rest.
`request` cannot set the fields PixDrop fills in itself: `model`, `prompt`, `messages`,
`input_references`, `modalities` and `stream`.

### Changes and errors

The file is re-read when it changes, so models can be added, changed or removed without a
restart (a provider it starts using has its catalogue read within 30 seconds). An invalid
file stops the server at startup, with a message that names the problem (`cannot start:
/data/config.json: model 'm': api_type must be "images" or "chat"`); an invalid edit while
it runs is ignored, with that message in the log, and the previous configuration stays in
use. Without the file the server starts with no models (and a warning in the log), and
every generation is refused with `no models are configured on this server`.

A job records the model's name, provider, upstream id and API type when it is made. If one
of them changes before the generation starts (a job waiting for its reference images, say),
or the model is removed, the job fails with `the configuration of the model 'sunburst'
changed since the job was made; submit it again` or `the model 'sunburst' is no longer
configured on this server`.

### Examples

**OpenRouter, with the key in an environment variable**: the file can then be shared or
kept in version control.

```json
{
  "providers": {"openrouter": {"api_key_env": "OPENROUTER_API_KEY"}},
  "models": {
    "sunburst": {"provider": "openrouter", "model": "openai/gpt-image-2.5-sunburst", "api_type": "images"},
    "flux": {"provider": "openrouter", "model": "black-forest-labs/flux-3-image", "api_type": "images"}
  }
}
```

```bash
docker run ... -e OPENROUTER_API_KEY=sk-or-v1-... ... pixdrop
```

**Fewer options than the model has**: game icons, text to image only, always a
transparent PNG:

```json
"icons": {
  "provider": "openrouter", "model": "openai/gpt-image-2.5-sunburst", "api_type": "images",
  "description": "Game icons on a transparent background",
  "options": ["quality"],
  "defaults": {"quality": "high"},
  "max_reference_images": 0,
  "request": {"background": "transparent", "output_format": "png"}
}
```

**Two providers**: OpenRouter, and another service with the same API but without a model
catalogue, whose models therefore list their options.

```json
"providers": {
  "openrouter": {"api_key_env": "OPENROUTER_API_KEY"},
  "inhouse": {"base_url": "https://images.example.lan/v1", "api_key_env": "INHOUSE_KEY"}
},
"models": {
  "inhouse-sdxl": {"provider": "inhouse", "model": "sdxl", "api_type": "images",
                   "options": ["size", "seed"], "max_reference_images": 1}
}
```

## Environment variables

Set them with `-e NAME=value` on the `docker run` command line, under `environment:` in
`compose.yaml`, or in the environment of `python -m pixdrop`:

```yaml
    environment:
      TZ: Europe/Athens
      PUBLIC_BASE_URL: https://pixdrop.example.com
      MAX_ACTIVE_JOBS: "2"
```

Sizes are in MB (1 MB = 1024 × 1024 bytes). Server Info on the web page and
`GET /api/info` show the values in effect. An invalid value stops the server at startup,
with a message that names it (`cannot start: MAX_UPLOAD_MB must be at least 1, got 0`).

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `/data` | Where the service keeps everything: the two configuration files, the jobs and their images, and the log. The Docker image uses the volume `/data`; without Docker, set it to a directory of your choice. |
| `CONFIG_FILE` | `$DATA_DIR/config.json` | The [providers and models](#providers-and-models) file. |
| `USERS_FILE` | `$DATA_DIR/users.json` | The [users](#users) file. |
| `PUBLIC_BASE_URL` | (empty) | The address users reach the service at, such as `https://pixdrop.example.com`: the base of the URLs the server hands out (the images' URLs in jobs, the MCP server's address and its transfer links). Empty: the address each request came to. Set it behind a reverse proxy (see [DEPLOYMENT.md](DEPLOYMENT.md#reverse-proxies)). It is a scheme, a host name and optionally a port, without a path: PixDrop is served at the root of its address (a domain or subdomain of its own), not under a path such as `/pixdrop/`. A trailing `/` is ignored. |
| `TZ` | `UTC` | Time zone of the dates the API and the web page show, of the months in the statistics, and of the log (an IANA name such as `Europe/Athens`). Job records store UTC. |
| `GENERATION_TIMEOUT_SECONDS` | `180` | A generation without an answer from the provider after this long fails. At least 10. |
| `MAX_UPLOAD_MB` | `20` | Largest reference image, from 1 to 1024. |
| `MAX_ACTIVE_JOBS` | `4` | Jobs each user may have in progress at the same time (waiting for their reference images or generating); more are refused with `429` until one finishes. It limits how many generations run at once, not how much they cost. |
| `MEDIUM_IMAGE_PX` | `512` | The longer side of the medium size of images, from 64 to 4096. |
| `ENABLE_MCP` | `0` | Set to `1` to serve the [MCP server](MCP.md) at `/mcp`, through which AI agents (Claude Code, Codex, Antigravity, ...) use the service with a user's token, and its transfer links under `/api/links/`. |
| `LINK_EXPIRY_MINUTES` | `60` | A transfer link of the MCP server stops working this long after it was made (it also stops once used). A job waiting for its reference images fails if they have not all arrived this long after it was made. At least 1. |
| `FORWARDED_ALLOW_IPS` | `*` | IPs of the reverse proxies whose `X-Forwarded-Proto` and `X-Forwarded-For` headers are trusted: for the scheme of the request (`https` URLs and the cookie's `Secure` flag) and for the client's address. `*` trusts any peer: the client's address is then the first address of `X-Forwarded-For`, else Cloudflare's `CF-Connecting-IP`, else the connection's, so the port must be reachable only through the proxy. Naming the proxies (such as `172.17.0.1`) makes PixDrop take `X-Forwarded-For` only from them, and ignore `CF-Connecting-IP` (see [Reverse proxies](DEPLOYMENT.md#reverse-proxies)). |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Where the server listens. With Docker, publish the port with `-p` instead; the image's health check follows `PORT` should you change it (with `--network host`, say). |

The flags take `1`, `true` or `yes`, and `0`, `false` or `no`.

## Fixed limits

These are not configurable.

| | |
|---|---|
| Reference images in one job | 16, the most OpenRouter's Images API accepts (its `input_references`); each model may accept fewer (`max_reference_images`) |
| A prompt | 32,000 characters (spaces at either end do not count) |
| A picture (reference or generated) | 50 megapixels (width × height), also a picture embedded in an SVG |
| Drawing an SVG | 10 seconds and 2 GB of memory, in a helper process; at most 2 at a time |
| A request body with images (`POST /api/jobs`, uploads of reference images, upload links) | 16 × `MAX_UPLOAD_MB` + 1 MB (321 MB by default) |
| A request body to the MCP server (`POST /mcp`) | 1 MB |
| Any other request body (such as a login) | 64 KB |
| An image a model returns as a link | 64 MB |
| Reading a provider's model catalogue | every 6 hours, 5 minutes after a failure; 30 seconds to answer |
| Images generated in one job (`n`) | 10 |
| Medium sizes made at the same time | 2 |
| Medium sizes kept in memory | 64 MB, the most recently used |
| Waiting for a job with `GET /api/jobs/{id}?wait=` | 600 seconds |
| Waiting for a job in an MCP tool call | 50 seconds |
| Jobs per page of the job list | 100 |
| The provider's error message kept in a job's `error` | 1,000 characters |
| The log file | 10 MB, plus 4 older files |
