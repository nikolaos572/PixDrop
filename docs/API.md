# API documentation

Everything the service does is available through a JSON API over HTTP: generate images from
a prompt and reference images, follow the job, download the images in full or medium size,
and look at your jobs and statistics. The web page uses the same API, so anything it can do,
a program can do too.

This document describes the whole API: authentication, every endpoint with its
parameters, responses and errors, how jobs behave, and complete examples. The server also
serves its machine-readable description (see [OpenAPI](#openapi)). AI agents can use the
service through its MCP server instead, which offers the same operations as tools (see
[MCP.md](MCP.md)).

**Contents**

1. [Quick start](#quick-start)
2. [Good to know](#good-to-know)
3. [Concepts](#concepts)
4. [Authentication](#authentication)
5. [Requests and responses](#requests-and-responses)
6. [Jobs](#jobs)
7. [Reference images](#reference-images)
8. [Endpoints](#endpoints)
9. [Examples](#examples)
10. [Writing a client](#writing-a-client)
11. [OpenAPI](#openapi)

## Quick start

```bash
URL=http://localhost:8000; AUTH="Authorization: Bearer $TOKEN"

# Generate an image and wait up to 50 seconds for it (the model and the options are optional)
curl -H "$AUTH" -F "prompt=A pixel-art treasure chest, 32x32 sprite" -F background=transparent "$URL/api/jobs?wait=50"
# {"id":"1793635964825_508b","status":"completed",...,"images":[{"index":1,...,"url":"http://localhost:8000/api/jobs/1793635964825_508b/images/1",...}],...}

# Still "generating"? Wait again, as often as needed
curl -H "$AUTH" "$URL/api/jobs/1793635964825_508b?wait=50"

# Download the image, in full size and in medium size (512 pixels)
curl -H "$AUTH" -o chest.png $URL/api/jobs/1793635964825_508b/images/1
curl -H "$AUTH" -o chest_medium.png "$URL/api/jobs/1793635964825_508b/images/1?size=medium"

# From reference images: the same character from another side
curl -H "$AUTH" -F "prompt=The same knight seen from the back" -F reference=@knight-front.png "$URL/api/jobs?wait=50"
```

## Good to know

A few things that are easy to miss when building a client; the sections below have the
details.

- **Every generation costs money.** The provider charges for each image, so a `POST
  /api/jobs` is not repeated safely: after a broken connection, list your newest jobs before
  submitting again ([more](#post-apijobs)).
- **The options depend on the model.** Each model accepts some options (aspect ratio,
  quality, ...) with its own values, and refuses the others: ask
  [`GET /api/models`](#get-apimodels), which follows the providers' model catalogues
  ([more](#concepts)).
- **Images are never in the JSON.** A job lists the URLs of its images, which you download
  with your token, in full size or in medium size ([more](#images)).
- **Keep `wait` below your proxy's timeout.** `?wait=N` keeps the request open for up to `N`
  seconds. nginx, for example, ends requests after 60 seconds by default, so use `wait=50`
  and ask again until the job is finished ([more](#waiting-for-a-job)).
- **Uploads need a `Content-Length` header.** Streamed (chunked) requests are refused with
  `411`; send files as `curl -F` and most HTTP libraries do ([more](#requests-and-responses)).
- **`wait` does not wait for a pending job.** A job waiting for its reference images is
  returned at once: only uploading them moves it on ([more](#get-apijobsid)).
- **`detail` is a list for `422`.** Other errors have a message in `detail`, but invalid
  parameters give a list of problems ([more](#errors)).
- **No calls from other web sites.** The API sends no CORS headers, so a web page on
  another domain cannot call it from the browser ([more](#requests-and-responses)).
- **Nothing is deleted.** There is no way to delete a job or an image through the API; jobs
  stay, with their images, as your history.

## Concepts

- **User and token.** The administrator gives each user a name and a secret token (see
  [CONFIGURATION.md](CONFIGURATION.md#users)). Every request is made as one user, and a user
  sees only their own jobs.
- **Model.** A name the administrator configured, such as `sunburst`, that stands for a
  model of a provider such as [OpenRouter](https://openrouter.ai) (`openai/gpt-image-2.5-sunburst`).
  One model is the default, used when a request names none.
- **Options.** Settings of a generation: `aspect_ratio`, `resolution`, `size`, `quality`,
  `background`, `output_format`, `output_compression`, `n` (the number of images) and
  `seed`. Each model accepts some of them, with its own values (as its provider's model
  catalogue lists them, or as the administrator chose), and may have defaults for them;
  [`GET /api/models`](#get-apimodels) lists them.
- **Reference images.** Up to 16 images sent with the prompt: an image to change
  (image to image), or images to follow (text and image to image), such as one side of a
  game character to draw the others in the same style. Each model accepts a given number,
  and a model that only edits images needs at least one.
- **Job.** One request, with its prompt, model, options and reference images, and what came
  out of it: the images, the model's text, the usage and cost, or the error. Its id is the
  time it was made in milliseconds and 4 hexadecimal digits, such as `1793635964825_508b`,
  so ids sort in the order jobs were made.
- **Usage and cost.** The tokens and the cost in US dollars of a generation, as the provider
  reported them.
- **Full and medium size.** Every image can be downloaded as it was generated or uploaded,
  or scaled down to fit 512 × 512 pixels (the server's `medium_image_px`), which is quicker
  to show or to look at.

## Authentication

Send the token with every request (except [`GET /api/health`](#get-apihealth)), in one of
these places, which the server looks at in this order:

1. the `Authorization` header: `Authorization: Bearer 4c1f0a6e9b...`;
2. the `X-API-Key` header: `X-API-Key: 4c1f0a6e9b...`;
3. the URL: `?token=4c1f0a6e9b...`;
4. the web page's cookie (see below).

The first of these that the request has decides: a request with a wrong token in
`Authorization` is refused, even if `X-API-Key` has the right one.

```bash
curl -H "Authorization: Bearer $TOKEN" $URL/api/session
curl -H "X-API-Key: $TOKEN" $URL/api/session
curl "$URL/api/session?token=$TOKEN"
# {"user":"alice"}
```

- The word `Bearer` is not case-sensitive. A token has 16 to 256 characters, letters,
  digits or `._~+/=-`.
- A missing, malformed or unknown token gets `401 Unauthorized` with the header
  `WWW-Authenticate: Bearer` and `{"detail": "missing or invalid token"}`. The same happens
  when the administrator removes or replaces the token while you use it: the users file is
  re-read when it changes.
- Prefer the headers. A token in the URL is for clients that cannot send headers (an image
  URL given to another program, say); URLs are easily kept in the history of browsers and
  shells, and in the logs of proxies. PixDrop's own log never shows tokens (it writes
  `token=***`).
- To check a token, call [`GET /api/session`](#get-apisession), which returns the user name.

**Browsers.** The web page logs in with [`POST /api/session`](#post-apisession), which keeps
the token in an HTTP-only cookie named `pixdrop_token`. The cookie is used only when the
request has none of the other three. Requests authenticated by it must carry the header
`X-Requested-With` (any value) when they change something (`POST`, `PUT`, `PATCH`,
`DELETE`); otherwise they get `403 Forbidden` with `{"detail": "cookie-authenticated
requests need a X-Requested-With header"}`. This stops other web sites from acting with a
visitor's cookie. Jobs made through the cookie are marked as made on the web page
(`"source": "web"`). Programs should use a header.

## Requests and responses

- **Base URL.** All endpoints are under `/api` on the server, for example
  `http://localhost:8000/api/jobs`. Behind a reverse proxy the base URL is the proxy's.
- **Request bodies.** `POST /api/jobs` and the uploads of reference images take
  `multipart/form-data`, as `curl -F` sends it (without files, `POST /api/jobs` also takes
  a URL-encoded form, as `curl -d` sends it). The only JSON body is the one of
  [`POST /api/session`](#post-apisession). Every `POST`, `PUT` and `PATCH` request must
  have a `Content-Length` header, and no request may be sent in chunks
  (`Transfer-Encoding`); otherwise the server answers `411 Length Required`
  (`{"detail": "Content-Length required"}`). Ordinary HTTP clients send the header; do not
  use chunked uploads.
- **Body sizes.** A body larger than its endpoint allows is refused at once with `413`,
  before it is read (`{"detail": "the request exceeds the 321 MB limit"}`): 16 ×
  `max_upload_mb` + 1 MB (321 MB by default) for the requests that carry images
  (`POST /api/jobs`, `POST /api/jobs/{id}/references` and upload links), 1 MB for the MCP
  server, 64 KB for anything else, such as a login. Requests that carry images are also
  authenticated before their body is read: without a valid token they get `401` (or `403`)
  at once, without uploading anything.
- **Responses** are JSON (`application/json`), except the images and `204 No Content`,
  which has no body. Fields are always present; those that may be empty are `null`. New
  fields may be added in later versions, so ignore fields you do not know.
- **URLs** in responses are absolute: they start with the server's `PUBLIC_BASE_URL`, or
  with the address the request came to (shown as `base_url` by
  [`GET /api/info`](#get-apiinfo)).
- **Dates** are ISO 8601 strings with the offset of the server's time zone (the `TZ`
  setting, shown by [`GET /api/info`](#get-apiinfo)), to the second, for example
  `"2026-11-02T18:12:44+02:00"`.
- **Sizes** in the limits are in MB, where 1 MB = 1024 × 1024 bytes. File sizes and
  storage use are in bytes.
- **Methods.** Only the methods listed here are accepted; others get `405 Method Not
  Allowed`. `HEAD` is not supported: use `GET`.
- **Cross-origin requests (CORS)** are not supported: a web page served from another
  origin cannot call the API from the browser. Call it from your server, or serve your page
  from the same origin through a reverse proxy.
- **Rate limits.** There are none, but each user may have at most `max_active_jobs` jobs
  in progress (4 by default): more are refused with `429` until one finishes.
- Responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
  `Referrer-Policy: no-referrer`.

### Errors

Every error except `500` has a JSON body with a `detail` field. For most errors `detail` is
a message meant for people, which a client can show as it is:

```json
{"detail": "model 'icons' does not support 'background'; its options: quality"}
```

For `422` (a parameter is missing or has a value of the wrong kind) `detail` is a list with
one entry per problem. `loc` says where the value is (`path`, `query` or `body`, then its
name), `msg` what is wrong, and `input` what was received:

```json
{"detail": [{"type": "less_than_equal", "loc": ["query", "wait"], "msg": "Input should be less than or equal to 600", "input": "601", "ctx": {"le": 600}}]}
```

The messages may be reworded in later versions: base decisions on the status code, not
on the text.

| Status | Meaning |
|---|---|
| `200 OK`, `201 Created`, `204 No Content` | Success. |
| `206 Partial Content` | Part of an image, as asked with a `Range` header. |
| `400 Bad Request` | The request cannot be used: an empty prompt, an unknown model, an option the model does not accept or a value it does not allow, a reference image that is not one, or too many of them. `detail` says why. |
| `401 Unauthorized` | No token, or a token the server does not know. |
| `403 Forbidden` | A cookie-authenticated change without `X-Requested-With`. |
| `404 Not Found` | No such job (or a job of another user), no such image (yet), or no such transfer link. |
| `405 Method Not Allowed` | The endpoint does not accept this method. |
| `409 Conflict` | Reference images sent for a job that is no longer waiting for them. |
| `411 Length Required` | A `POST` without `Content-Length`, or a chunked request. |
| `413 Content Too Large` | A request body larger than the endpoint allows (see [Requests and responses](#requests-and-responses)). |
| `422 Unprocessable Content` | A missing parameter, or one of the wrong kind or out of range; `detail` is a list. |
| `429 Too Many Requests` | You have `max_active_jobs` jobs in progress already. |
| `500 Internal Server Error` | A fault in the server (body: the text `Internal Server Error`). It is logged; retrying may help. |

When the server is restarted (for an upgrade, say), connections are refused for a few
seconds, and requests still open 5 seconds after the shutdown began, such as a long
[wait](#get-apijobsid), end with `500`. Retry them. A job that was generating fails (see
[Statuses](#statuses)); jobs waiting for their reference images keep waiting.

## Jobs

### The job object

Endpoints that return jobs return this object:

```json
{
  "id": "1793635971187_8aee",
  "status": "completed",
  "prompt": "The same knight seen from the back, same style and palette",
  "model": "nano-banana",
  "provider": "openrouter",
  "upstream_model": "google/gemini-3.1-flash-image",
  "api_type": "images",
  "options": {"aspect_ratio": "1:1"},
  "source": "api",
  "client_ip": "203.0.113.7",
  "created_at": "2026-11-02T18:12:51+02:00",
  "started_at": "2026-11-02T18:12:51+02:00",
  "finished_at": "2026-11-02T18:12:58+02:00",
  "duration_seconds": 6.912,
  "reference_count": 2,
  "references": [
    {
      "index": 1,
      "filename": "1_knight-front.png",
      "content_type": "image/png",
      "bytes": 215,
      "width": 64,
      "height": 64,
      "original_name": "knight-front.png",
      "url": "http://localhost:8000/api/jobs/1793635971187_8aee/references/1",
      "medium_url": "http://localhost:8000/api/jobs/1793635971187_8aee/references/1?size=medium"
    },
    {
      "index": 2,
      "filename": "2_palette.jpg",
      "content_type": "image/jpeg",
      "bytes": 8230,
      "width": 800,
      "height": 600,
      "original_name": "palette.jpg",
      "url": "http://localhost:8000/api/jobs/1793635971187_8aee/references/2",
      "medium_url": "http://localhost:8000/api/jobs/1793635971187_8aee/references/2?size=medium"
    }
  ],
  "images": [
    {
      "index": 1,
      "filename": "1793635971187_8aee.png",
      "content_type": "image/png",
      "bytes": 1503722,
      "width": 1024,
      "height": 1024,
      "original_name": null,
      "url": "http://localhost:8000/api/jobs/1793635971187_8aee/images/1",
      "medium_url": "http://localhost:8000/api/jobs/1793635971187_8aee/images/1?size=medium"
    }
  ],
  "text": null,
  "usage": {"prompt_tokens": 612, "completion_tokens": 1290, "total_tokens": 1902, "cost_usd": 0.04},
  "error": null,
  "upload_url": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | The job's id: 13 digits (the time it was made, in milliseconds since 1970, UTC), `_` and 4 hexadecimal digits. |
| `status` | string | `pending`, `generating`, `completed` or `failed` (see [Statuses](#statuses)). |
| `prompt` | string | The prompt, without spaces at either end. |
| `model` | string | The model's name, as in [`GET /api/models`](#get-apimodels). |
| `provider` | string | The provider the model is reached through, as the administrator named it. |
| `upstream_model` | string | The provider's id of the model. |
| `api_type` | string | `images` or `chat`: the provider's API the model is called through. |
| `options` | object | The options of the generation: those you set and the model's defaults for the others. Integers (`n`, `seed`) are numbers, the others strings. |
| `source` | string | Where the job was made: `web` (the web page), `api` or `mcp` (an AI agent). |
| `client_ip` | string | The address the job was made from. |
| `created_at` | string | When the job was made. |
| `started_at` | string or null | When its generation started: when it was made, or when its last reference image arrived. `null` while it is `pending`. |
| `finished_at` | string or null | When it became `completed` or `failed`; `null` before. |
| `duration_seconds` | number or null | How long the generation took, until the provider's answer (or the failure); `null` before, and for jobs that never started or were cut off by a restart. |
| `reference_count` | integer | How many reference images the job has, or expects (for a `pending` job). |
| `references` | list | The [reference images](#reference-images) received, numbered from 1 (see below). |
| `images` | list | The generated images, numbered from 1 (see below). Empty until the job is `completed`. |
| `text` | string or null | Text the model returned with the images (chat models often say something), or `null`. |
| `usage` | object or null | What the provider reported: `prompt_tokens`, `completion_tokens`, `total_tokens` and `cost_usd` (the cost in US dollars, OpenRouter's `usage.cost`), each `null` if it did not say. `null` while the job is in progress. A failed job has it when the provider answered without a usable image (no image, or one that is not an image), which it may charge for, and `null` when the provider refused the request, did not answer or could not be reached. |
| `error` | string or null | For a failed job, why (see [Errors of a job](#errors-of-a-job)); otherwise `null`. |
| `upload_url` | string or null | For a `pending` job, where to upload its reference images ([`POST /api/jobs/{id}/references`](#post-apijobsidreferences)); otherwise `null`. |

Each image in `images` and `references`:

| Field | Type | Meaning |
|---|---|---|
| `index` | integer | Its number in the job, from 1. |
| `filename` | string | Its file name on the server: `<id>.png`, `<id>_2.png`, ... for generated images, which their download offers under that name; `1_<name>.png`, ... for reference images (the uploaded name reduced to letters, digits and `._-`, at most 60 characters), which their download offers as `<id>_reference_<filename>`. The extension follows the image's type. |
| `content_type` | string | `image/png`, `image/jpeg`, `image/webp`, `image/gif` or `image/svg+xml`, recognised from the content. |
| `bytes` | integer | Its size. |
| `width`, `height` | integer | Its size in pixels (for an SVG, the size it declares). |
| `original_name` | string or null | A reference image's file name as it was uploaded (at most 200 characters); `null` for generated images. |
| `url` | string | Where to download it in full size, with your token. |
| `medium_url` | string | Where to download it in medium size. |

### Statuses

```
pending ──(all reference images uploaded)──> generating ──> completed
   │                                              │
   └──(not uploaded in time)──> failed <──────────┘
```

- `pending`: the job waits for the reference images it announced (see
  [Reference images](#reference-images)). Only uploading them moves it on, so `?wait=`
  returns a pending job at once. It fails if they have not all arrived
  `link_expiry_minutes` (60) after the job was made (checked every 30 seconds).
- `generating`: the provider is generating the images. Jobs of all users generate at the
  same time; each user may have `max_active_jobs` jobs pending or generating.
- `completed`: the images are there, in `images`, with the usage.
- `failed`: the generation did not produce an image. `error` says why, and `usage` what the
  provider reported if it answered all the same.

`completed` and `failed` are final: a job never changes after that. A job that was
`generating` when the server stopped is `failed`; it is not started again.

### Errors of a job

`error` is one line. The provider's own message, where it gives one, comes after the
provider's name (as the administrator named it, such as `openrouter`), shortened to 1,000
characters:

- `openrouter refused the request (HTTP 402): Insufficient credits. Add more using https://openrouter.ai/credits`:
  the provider answered with an error status. Its message may be followed by its code, the
  name of the upstream provider in brackets and that provider's own message.
- `openrouter reported an error: Provider returned error (code 502) [Google]: ...`: an error
  in an answer with status `200`. `openrouter reported an error during the generation: ...`
  is the same for a chat model.
- `the model returned no image`, followed by `; it answered: ...` with the model's text when
  it gave one (a chat model may answer with words only, for example when it declines the
  prompt).
- `openrouter did not answer in time`, `no image after 180 seconds (GENERATION_TIMEOUT_SECONDS)`:
  the generation took longer than the server allows.
- `cannot reach openrouter: ...`, `openrouter sent an unexpected answer (HTTP 200): ...`,
  `the provider returned an unusable image: ...`, `openrouter returned an image in an
  unsupported form`, `cannot download the image openrouter linked to: ...`: the provider
  could not be reached, or what it sent is not usable.
- `the reference images were not uploaded within 60 minute(s)`: a `pending` job whose time
  ran out.
- `the model 'sunburst' is no longer configured on this server`, `the configuration of the
  model 'sunburst' changed since the job was made; submit it again`: the administrator
  changed the models before the generation started.
- `interrupted: the server stopped during the generation`, `interrupted: the server
  restarted during the generation`: the server was stopped or restarted.
- `internal error while generating; see the server log`: a fault in the server.

### Images

Images never travel inside the JSON: each image of a job has a `url` and a `medium_url`,
from which you download it with your token
([`GET /api/jobs/{id}/images/{n}`](#get-apijobsidimagesn)).

- **Full size** is the file as the provider generated it (or as you uploaded it, for
  reference images), in its own format: PNG, JPEG or WebP for most models, SVG for vector
  models (`output_format=svg`).
- **Medium size** is scaled down to fit a square of `medium_image_px` (512) pixels, to show
  a preview or to look at an image cheaply. PNG, JPEG and WebP keep their format, and are
  sent as they are when they fit already; a GIF gives a PNG of its first frame, and an SVG a
  PNG drawn to fit the square. The medium size is made when it is first asked for, and is
  not stored.

### Lifetime and storage

Jobs are never deleted, by the server or through the API: a job stays with its images and
reference images, and can be downloaded at any time. `account.storage_bytes` in
[`GET /api/info`](#get-apiinfo) says how much your jobs take. Only the administrator can
remove jobs, by hand on the server.

## Reference images

### What can be uploaded

- PNG, JPEG, WebP, GIF and SVG, recognised from the content, not from the name. Other files
  are refused with `400` (`knight.txt: unsupported image type: use PNG, JPEG, WebP, GIF or
  SVG`).
- Each file at most `max_upload_mb` (20 MB), and at most 50 megapixels (width × height).
- Raster images are decoded in full, so damaged files are refused. An animated GIF counts
  by its first frame, which is what the model receives. Other formats (BMP, TIFF, ...) are
  refused as unsupported.
- An SVG must be well-formed XML with an `<svg>` root element, without entity declarations
  (`<!ENTITY ...>`), which are refused, and it must be drawable within 10 seconds; it is
  drawn without fetching anything outside it, and pictures embedded in it (as `data:` URLs)
  may have at most 50 megapixels too. Its size is its `width` and `height` (in `px`, `pt`,
  `pc`, `mm`, `cm` or `in`), else its `viewBox`, else 300 × 150.
- Each image is a file part named `reference`, as `curl -F reference=@image.png` sends it.
  An empty file part without a name (a file field of an HTML form left empty) is ignored; a
  text field named `reference` is refused with `400` (`'reference' must be an image file (as
  curl -F reference=@image.png sends it), not text`).
- At most 16 per job, at most the model's `max_reference_images`, and at least its
  `min_reference_images` ([`GET /api/models`](#get-apimodels)).

The model receives the images as they were uploaded, except GIFs (their first frame) and
SVGs (drawn 2048 pixels on their longer side), which it receives as PNG.

### Sending them

There are two ways to give a job its reference images:

- **With the request** (the usual way): add them to [`POST /api/jobs`](#post-apijobs) as
  fields named `reference`, one per file. The job starts generating at once.

  ```bash
  curl -H "$AUTH" -F "prompt=The same knight seen from the back" \
    -F reference=@knight-front.png -F reference=@palette.jpg "$URL/api/jobs?wait=50"
  ```

- **Later** (two steps): announce their number with `reference_count` instead. The job is
  created as `pending`, with an `upload_url`; upload the images there, in one request or in
  several ([`POST /api/jobs/{id}/references`](#post-apijobsidreferences)). The job starts
  generating when the last one arrives. This is how the MCP server's agents upload: the job
  is made by a tool call, and the files are sent by `curl` (see [MCP.md](MCP.md)). A pending
  job counts towards `max_active_jobs`, and fails if its images have not all arrived
  `link_expiry_minutes` after it was made.

## Endpoints

| Endpoint | |
|---|---|
| [`GET /api/health`](#get-apihealth) | Whether the server is up (no token). |
| [`GET /api/info`](#get-apiinfo) | The server's settings and limits, and your account. |
| [`GET /api/models`](#get-apimodels) | The models, their options and allowed values. |
| [`POST /api/jobs`](#post-apijobs) | Generate images. |
| [`GET /api/jobs`](#get-apijobs) | Your jobs, newest first, a page at a time. |
| [`GET /api/jobs/{id}`](#get-apijobsid) | One job, optionally waiting until it finishes. |
| [`POST /api/jobs/{id}/references`](#post-apijobsidreferences) | Upload the reference images of a pending job. |
| [`GET /api/jobs/{id}/images/{n}`](#get-apijobsidimagesn) | Download a generated image, full size or medium. |
| [`GET /api/jobs/{id}/references/{n}`](#get-apijobsidreferencesn) | Download a reference image, full size or medium. |
| [`GET /api/stats`](#get-apistats) | Your statistics. |
| [`GET /api/session`](#get-apisession) | Who you are. |
| [`POST /api/session`](#post-apisession), [`DELETE /api/session`](#delete-apisession) | Log a browser in and out (the web page). |
| [`POST /api/links/{secret}`, `GET /api/links/{secret}`](#transfer-links-apilinkssecret) | Upload or download through a transfer link of the MCP server. |

In paths, `{id}` is a job id such as `1793635964825_508b`, or `last` for your newest job,
whatever its status (except for uploads, which need the id); `{n}` is an image's number,
from 1 to 16. Anything else gets `422`.

### `GET /api/health`

Whether the server is running. Needs no token; meant for monitoring and health checks.

**Response** `200`:

```json
{"status": "ok"}
```

### `GET /api/info`

The server's settings and limits, and your account. These are the values the server
applies, so a client can check a request against them before sending it.

**Response** `200`:

```json
{
  "server": {
    "version": "1.0.0",
    "timezone": "Europe/Athens",
    "base_url": "http://localhost:8000",
    "mcp_url": "http://localhost:8000/mcp",
    "link_expiry_minutes": 60.0,
    "generation_timeout_seconds": 180.0,
    "max_upload_mb": 20,
    "max_reference_images": 16,
    "max_active_jobs": 4,
    "max_prompt_chars": 32000,
    "medium_image_px": 512,
    "models": 2,
    "default_model": "sunburst"
  },
  "account": {"user": "alice", "jobs": 14, "in_progress": 1, "storage_bytes": 93509}
}
```

| Field | Type | Meaning |
|---|---|---|
| `server.version` | string | The service's version. |
| `server.timezone` | string | The IANA time zone of the dates in responses. |
| `server.base_url` | string | The start of the URLs in responses: `PUBLIC_BASE_URL`, or the address this request came to. |
| `server.mcp_url` | string or null | The address of the [MCP server](MCP.md), to give to AI agents; `null` when it is off. |
| `server.link_expiry_minutes` | number | A [transfer link](#transfer-links-apilinkssecret) stops working, and a `pending` job fails, this long after it was made. |
| `server.generation_timeout_seconds` | number | A generation without an answer after this long fails. |
| `server.max_upload_mb` | integer | The largest reference image. |
| `server.max_reference_images` | integer | Reference images per job, whatever the model (each model may allow fewer). |
| `server.max_active_jobs` | integer | Jobs each user may have pending or generating at the same time. |
| `server.max_prompt_chars` | integer | The longest prompt. |
| `server.medium_image_px` | integer | The longer side of the medium size. |
| `server.models` | integer | How many models are configured. |
| `server.default_model` | string or null | The model used when a request names none; `null` when there are no models. |
| `account.user` | string | Your user name. |
| `account.jobs` | integer | Your jobs, of any status. |
| `account.in_progress` | integer | Your jobs pending or generating now. |
| `account.storage_bytes` | integer | Bytes your jobs take on the server: records, images and reference images. |

### `GET /api/models`

The models you can generate with, the options each accepts with that model's own allowed
values, its defaults, and how many reference images it takes. Pass a model's `name` and
options to [`POST /api/jobs`](#post-apijobs).

**Response** `200`:

```json
{
  "default_model": "sunburst",
  "models": [
    {
      "name": "sunburst",
      "description": "OpenAI GPT Image 2.5 Sunburst: precise, follows instructions closely, transparent backgrounds for sprites",
      "provider": "openrouter",
      "upstream_model": "openai/gpt-image-2.5-sunburst",
      "api_type": "images",
      "default": true,
      "min_reference_images": 0,
      "max_reference_images": 16,
      "options": [
        {"name": "aspect_ratio", "description": "Width to height; providers clamp to the ratios they support", "type": "enum", "values": ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "auto"], "minimum": null, "maximum": null, "pattern": null, "default": null},
        {"name": "quality", "description": "Rendering quality", "type": "enum", "values": ["auto", "low", "medium", "high", "xhigh", "max"], "minimum": null, "maximum": null, "pattern": null, "default": "high"},
        {"name": "background", "description": "Background; transparent needs png or webp output", "type": "enum", "values": ["auto", "transparent", "opaque"], "minimum": null, "maximum": null, "pattern": null, "default": null},
        {"name": "output_compression", "description": "Compression of jpeg and webp images, 0-100", "type": "integer", "values": null, "minimum": 0, "maximum": 100, "pattern": null, "default": null},
        {"name": "n", "description": "Number of images to generate in one job", "type": "integer", "values": null, "minimum": 1, "maximum": 10, "pattern": null, "default": null}
      ],
      "options_from": "provider"
    },
    {
      "name": "icons",
      "description": "Game icons on a transparent background",
      "provider": "openrouter",
      "upstream_model": "openai/gpt-image-2.5-sunburst",
      "api_type": "images",
      "default": false,
      "min_reference_images": 0,
      "max_reference_images": 0,
      "options": [
        {"name": "quality", "description": "Rendering quality", "type": "enum", "values": ["auto", "low", "medium", "high", "xhigh", "max"], "minimum": null, "maximum": null, "pattern": null, "default": "high"}
      ],
      "options_from": "config"
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `default_model` | string or null | The model used when a request names none. |
| `models[].name` | string | What to pass as `model`. |
| `models[].description` | string | What the administrator wrote about it (may be empty). |
| `models[].provider`, `models[].upstream_model`, `models[].api_type` | string | Where the model runs, its id there, and the API used (`images` or `chat`). |
| `models[].default` | boolean | Whether it is the default model. |
| `models[].min_reference_images`, `models[].max_reference_images` | integer | How many reference images a job of this model must and may have. A model that only edits images needs at least one; `max_reference_images` `0`: none. |
| `models[].options[].name` | string | The option, as a form field of `POST /api/jobs`. |
| `models[].options[].type` | string | `enum` (one of `values`, in any case), `integer` (from `minimum` to `maximum`) or `string` (matching `pattern`). The values and bounds are this model's own: two models may offer the same option with different values. |
| `models[].options[].default` | string, integer or null | The value used when you leave the option out; `null`: the option is then not sent, and the provider decides. |
| `models[].options_from` | string | Where the options come from: `provider` (the provider's model catalogue, see [CONFIGURATION.md](CONFIGURATION.md#options-from-the-providers-catalogue)), `config` (listed by the administrator), or `pending` (the catalogue has not been read yet, or does not list the model: no options for now). |

The full list of options PixDrop knows is in [CONFIGURATION.md](CONFIGURATION.md#options).
The models follow the server's configuration and the providers' catalogues, which may change
while the server runs: take the options and values from here rather than hard-coding them.

### `POST /api/jobs`

Generates images. The request is checked, its reference images received and checked, and
the job created; its generation starts at once, unless reference images are announced for
later (then the job is `pending`).

**Request**: `multipart/form-data` with

| Field | Required | |
|---|---|---|
| `prompt` | yes | What to generate, at most 32,000 characters. |
| `model` | no | A model's `name` from [`GET /api/models`](#get-apimodels). Omitted or empty: the default model. |
| `aspect_ratio`, `resolution`, `size`, `quality`, `background`, `output_format`, `output_compression`, `n`, `seed` | no | The [options](CONFIGURATION.md#options), each only if the model accepts it, with one of the values [`GET /api/models`](#get-apimodels) gives for that model. Omitted or empty: the model's default, if it has one. |
| `reference` | no | A reference image; repeat the field for several (from the model's `min_reference_images` to its `max_reference_images`). |
| `reference_count` | no | Instead of `reference`: how many reference images you will upload later, from 0 to 16 (default `0`). With files in `reference`, it must be `0` or their number. |

**Query parameters**

| Parameter | Default | |
|---|---|---|
| `wait` | `0` | Seconds to wait for the generation to finish before answering, from 0 to 600 (see [`GET /api/jobs/{id}`](#get-apijobsid)). A `pending` job is returned at once. |

```bash
curl -H "$AUTH" -F "prompt=A sprite sheet of a knight walking, 4 frames in a row, pixel art" \
  -F model=sunburst -F aspect_ratio=4:1 -F background=transparent -F output_format=png "$URL/api/jobs"
```

**Response** `201`: the new [job](#the-job-object), `generating` (or `pending`), or already
finished if `wait` was long enough:

```json
{"id": "1793636061763_4746", "status": "generating", "prompt": "A sprite sheet of a knight walking, 4 frames in a row, pixel art", "model": "sunburst", "provider": "openrouter", "upstream_model": "openai/gpt-image-2.5-sunburst", "api_type": "images", "options": {"quality": "high", "aspect_ratio": "4:1", "background": "transparent", "output_format": "png"}, "source": "api", "client_ip": "203.0.113.7", "created_at": "2026-11-02T18:14:21+02:00", "started_at": "2026-11-02T18:14:21+02:00", "finished_at": null, "duration_seconds": null, "reference_count": 0, "references": [], "images": [], "text": null, "usage": null, "error": null, "upload_url": null}
```

A job that fails at the provider is still created (`201`); its `status` becomes `failed`,
with the reason in `error`.

**Errors**

- `400`, with one of these `detail`s:
  - `the prompt is empty` (only spaces), `the prompt is too long: 32001 characters (at most 32000)`
  - `unknown model 'dalle'; available: sunburst, qwen, nano-banana, flux, vector`, `no models are configured on this server`
  - `model 'icons' does not support 'background'; its options: quality`
  - a value the model does not accept: `quality must be one of: low, medium, high`, `n must
    be an integer from 1 to 4`, `size must match auto|\d{2,5}x\d{2,5} (got 'big')`
  - `a transparent background needs output_format png or webp, not jpeg`
  - `model 'qwen' accepts at most 3 reference image(s), got 4`,
    `model 'edit' needs at least 1 reference image(s), got 0`,
    `at most 16 reference images per job, got 17`
  - `reference_count is 2 but 1 reference image(s) were sent`
  - a reference image that cannot be used, after its name:
    `knight.txt: unsupported image type: use PNG, JPEG, WebP, GIF or SVG` (also for BMP,
    TIFF and other image formats), `scan.png: the image is damaged or cannot be read`,
    `empty.png: the file is empty`,
    `poster.png: the image is too large: 8000 × 7000 pixels (at most 50 megapixels)`,
    `icon.svg: the SVG file is not valid XML (or declares entities, which are refused)`,
    `icon.svg: the SVG file has no <svg> root element`,
    `icon.svg: the SVG file embeds a picture that is too large: 8000 × 7000 pixels (at most 50 megapixels)`,
    `icon.svg: the SVG file takes too long to draw (more than 10 seconds)`,
    `icon.svg: the SVG file cannot be drawn within the limits of time and memory`,
    `icon.svg: the SVG file cannot be drawn`
  - `a reference image exceeds the 20 MB limit (MAX_UPLOAD_MB)`
  - `'reference' must be an image file (as curl -F reference=@image.png sends it), not text`
- `401`, `403`: no valid token, checked before the upload is read (see
  [Authentication](#authentication)).
- `411`, `413`: see [Requests and responses](#requests-and-responses).
- `422`: no `prompt` (or an empty one), a `reference_count` out of range, or a `wait` out of
  range.
- `429`: `you have 4 job(s) in progress, the most allowed (MAX_ACTIVE_JOBS); wait until one finishes`.

A `POST` is not repeated safely: if the connection breaks before the response arrives, the
job may or may not have been created, and each generation is paid for. List your newest jobs
([`GET /api/jobs`](#get-apijobs), or [`GET /api/jobs/last`](#get-apijobsid)) before
submitting again.

### `GET /api/jobs`

Your jobs, newest first, a page at a time. Jobs made on the web page, through the API and
by AI agents are listed together.

**Query parameters**

| Parameter | Default | |
|---|---|---|
| `page` | `1` | The page, from 1 to 1,000,000. Page 1 has the newest jobs. |
| `per_page` | `12` | Jobs per page, from 1 to 100. |

```bash
curl -H "$AUTH" "$URL/api/jobs?page=1&per_page=20"
```

**Response** `200`:

```json
{
  "jobs": [{"id": "1793636071638_7a43", "status": "completed", ...}, {"id": "1793636061763_4746", "status": "completed", ...}],
  "total": 57,
  "page": 1,
  "per_page": 20
}
```

| Field | Type | Meaning |
|---|---|---|
| `jobs` | list of [jobs](#the-job-object) | This page's jobs. A page after the last one is empty (not an error). |
| `total` | integer | How many jobs you have in all, on every page. |
| `page`, `per_page` | integer | As requested (or their defaults). |

The number of pages is `ceil(total / per_page)`. Jobs made between two requests shift the
pages; to go through all jobs, see [Listing all jobs](#listing-all-jobs).

**Errors**: `422` for a `page` or `per_page` out of range or not an integer.

### `GET /api/jobs/{id}`

One of your jobs; `last` is your newest one, whatever its status. With `wait`, the server
holds the request while the job is generating, until it finishes or the time is up, so a
client learns of the result as soon as it is there, without polling quickly.

**Query parameters**

| Parameter | Default | |
|---|---|---|
| `wait` | `0` | Seconds to wait for a generating job to become `completed` or `failed`, from 0 to 600. |

**Response** `200`: the [job](#the-job-object). It is returned as soon as the job is
finished, when `wait` seconds have passed (then the job may still be `generating`: ask
again), or at once with `wait=0`. A `pending` job is returned at once too: it does not move
on until its reference images are uploaded.

```bash
curl -H "$AUTH" "$URL/api/jobs/1793636061763_4746?wait=50"
curl -H "$AUTH" "$URL/api/jobs/last"
```

Behind a reverse proxy, keep `wait` below the proxy's read timeout (60 seconds by default
for nginx), or the proxy ends the request with a `504`. Asking again in a loop with
`wait=50` works everywhere (see [Waiting for a job](#waiting-for-a-job)). Give your HTTP
client a timeout longer than `wait`.

**Errors**: `404` with `no such job` for a job that does not exist or belongs to another
user, or `you have no jobs yet` for `last`; `422` for an invalid `id` or `wait`.

### `POST /api/jobs/{id}/references`

Uploads reference images of a `pending` job, which announced them with `reference_count`.
They may come in one request or in several; the job starts generating when the last one
arrives. The job's `upload_url` is this endpoint.

**Request**: `multipart/form-data` with

| Field | Required | |
|---|---|---|
| `reference` | yes | A reference image; repeat the field for several, up to the number still expected. |

```bash
curl -H "$AUTH" -F reference=@knight-front.png -F reference=@knight-side.png \
  $URL/api/jobs/1793635980937_087f/references
```

**Response** `200`: the [job](#the-job-object), `generating` once all its reference images
are in, `pending` while some are still expected.

**Errors**

- `400`: `no reference image was sent: add them as form fields named 'reference'`,
  `the job expects 1 more reference image(s), got 2`, or a reference image that cannot be
  used (as for [`POST /api/jobs`](#post-apijobs)). The job keeps waiting.
- `404`: `no such job`.
- `409`: `the job is generating: reference images can be added only while it is pending`
  (or `completed`, or `failed`, for example after its time ran out).
- `411`, `413`; `422` for an invalid `id` (`last` is not accepted here).

### `GET /api/jobs/{id}/images/{n}`

Downloads image `n` of a job.

**Query parameters**

| Parameter | Default | |
|---|---|---|
| `size` | `full` | `full`: the image as it was generated; `medium`: scaled down to fit `medium_image_px` (see [Images](#images)). |

```bash
curl -H "$AUTH" -o knight-walk.png $URL/api/jobs/1793636061763_4746/images/1
curl -H "$AUTH" -o knight-walk_medium.png "$URL/api/jobs/1793636061763_4746/images/1?size=medium"
```

**Response** `200`, with the image's type (`image/png`, ...) and
`Content-Disposition: inline; filename="1793636061763_4746.png"` (the image's `filename`;
`1793636061763_4746_medium.png` for a scaled-down medium size). Full-size downloads also
send `Content-Length`, `ETag` and `Last-Modified`, and honour a `Range` header
(`Range: bytes=0-1023` gets `206 Partial Content`), so interrupted downloads can be resumed.
Images carry `Cache-Control: private, max-age=86400` (a job's images never change), or
`Cache-Control: no-cache` when asked for through `last` (which job that is changes), and
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`, so
that an SVG opened in a browser cannot run scripts.

`last` is your newest job whatever its status: while it is pending or generating, or if it
failed, `GET /api/jobs/last/images/1` gets `404`, even if an older job has images.

**Errors**: `404` with `no such job`, `job 1793636061763_4746 has no image 2` (followed by
` yet: it is generating` or ` yet: it is pending` while the job is not finished), or `the
image file is missing on the server` (removed by hand); `422` for an invalid `id`, `n` or
`size`.

### `GET /api/jobs/{id}/references/{n}`

Downloads reference image `n` of a job, as uploaded (`size=full`, the default) or in medium
size (`size=medium`), like [`GET /api/jobs/{id}/images/{n}`](#get-apijobsidimagesn). The
offered file name is `<id>_reference_<filename>`, such as
`1793635971187_8aee_reference_1_knight-front.png`.

**Errors**: `404` with `no such job` or `job 1793635971187_8aee has no reference image 3`;
`422` for an invalid `id`, `n` or `size`.

### `GET /api/stats`

Your statistics, computed from all your jobs (made on the web page, through the API and by
AI agents): counts, images, cost, tokens and durations, in all, by model and by month.

**Response** `200` (shortened: each entry of `models` and `months` has every field of
`totals`):

```json
{
  "user": "alice",
  "totals": {
    "jobs": 14,
    "completed": 9,
    "failed": 5,
    "in_progress": 0,
    "images": 10,
    "references": 4,
    "cost_usd": 0.4,
    "average_cost_usd": 0.044444,
    "prompt_tokens": 1308,
    "completion_tokens": 12900,
    "total_tokens": 14208,
    "average_tokens": 1578.7,
    "average_duration_seconds": 2.32
  },
  "first_job_at": "2026-11-02T18:12:44+02:00",
  "last_job_at": "2026-11-02T18:13:53+02:00",
  "models": [
    {"model": "sunburst", "jobs": 12, "completed": 8, "failed": 4, "in_progress": 0, "images": 9, "cost_usd": 0.36, ...},
    {"model": "nano-banana", "jobs": 2, "completed": 1, "failed": 1, "in_progress": 0, "images": 1, "cost_usd": 0.04, ...}
  ],
  "months": [
    {"month": "2026-11", "jobs": 14, "completed": 9, "failed": 5, "in_progress": 0, "images": 10, "cost_usd": 0.4, ...}
  ],
  "sources": {"api": 12, "mcp": 2}
}
```

| Field | Type | Meaning |
|---|---|---|
| `user` | string | Your user name. |
| `totals.jobs`, `.completed`, `.failed`, `.in_progress` | integer | Your jobs, and how many are completed, failed, and pending or generating. |
| `totals.images` | integer | Images generated. |
| `totals.references` | integer | Reference images uploaded. |
| `totals.cost_usd` | number | The cost of your jobs in US dollars, as the providers reported it, also of failed jobs they charged for. |
| `totals.average_cost_usd` | number or null | The average cost of the jobs whose cost was reported; `null` if none was. |
| `totals.prompt_tokens`, `.completion_tokens`, `.total_tokens` | integer | Tokens, as the providers reported them. |
| `totals.average_tokens` | number or null | The average `total_tokens` of the jobs that reported them. |
| `totals.average_duration_seconds` | number or null | The average duration of the completed jobs. |
| `first_job_at`, `last_job_at` | string or null | When your first and newest jobs were made; `null` without jobs. |
| `models` | list | The same counts for each model you used (`model`), the most used first. |
| `months` | list | The same counts for each month (`month`, `YYYY-MM` in the server's time zone), the newest first. |
| `sources` | object | How many jobs were made on the web page (`web`), through the API (`api`) and by agents (`mcp`); sources without jobs are left out. |

### `GET /api/session`

Returns the user the request is authenticated as. Useful to check a token.

**Response** `200`:

```json
{"user": "alice"}
```

**Errors**: `401` for a missing or unknown token.

### `POST /api/session`

Logs a browser in: checks a token and, if it is valid, stores it in the `pixdrop_token`
cookie (`HttpOnly`, `SameSite=Strict`, `Path=/`, valid for 30 days, and `Secure` when the
server is reached over HTTPS). Used by the web page's login form; programs do not need it.

**Request**: JSON

```json
{"token": "4c1f0a6e9b..."}
```

**Response** `200`: `{"user": "alice"}`, with the `Set-Cookie` header.

**Errors**: `401` (`invalid token`); `422` for a body without `token`, or a token longer
than 256 characters.

### `DELETE /api/session`

Logs a browser out: deletes the cookie (and nothing else). Needs no token.

**Response** `204`, without a body.

### Transfer links (`/api/links/{secret}`)

Served only while the [MCP server](MCP.md) is on (`ENABLE_MCP=1`; it is off by default). Its tools
hand out these URLs, through which an agent moves images with `curl`. The secret in the URL
stands in for the token, so these requests carry none; each link works only from the
address that asked for it, for one complete transfer, and for `link_expiry_minutes` (see
[Transfer links](MCP.md#transfer-links)).

**`POST /api/links/{secret}`**, with an upload link: the same request as
[`POST /api/jobs/{id}/references`](#post-apijobsidreferences) (`reference` fields), with
the same response and errors, except that the job comes without its API URLs (`url`,
`medium_url` and `upload_url` are `null`). A refused upload leaves the link valid.

**`GET /api/links/{secret}`**, with a download link: the image, in the size the link was
made for, as [`GET /api/jobs/{id}/images/{n}`](#get-apijobsidimagesn) or
[`/references/{n}`](#get-apijobsidreferencesn) give it. A link always serves the whole
image (`200`): a `Range` header is ignored. A download cut off before its end leaves the
link valid; a complete one uses it up.

**Errors**: `404` with the same message whatever the reason (the link does not exist, has
expired or been used, is used from another address or with the other method, or its user
was removed), which goes to the server's log:

```json
{"detail": "no such link: links expire, work once and only from the address they were made for; ask for a new one"}
```

The link is checked as soon as the request arrives, before an upload is read.

## Examples

### Generating from the shell

A script that generates images from a prompt and optional reference images, waits for
them, and saves them in the current directory. It needs `curl` and
[`jq`](https://jqlang.org/), the URL and the token in `PIXDROP_URL` and `PIXDROP_TOKEN`,
and, to use another model than the default, its name in `PIXDROP_MODEL`:

```bash
#!/bin/bash
# Generate images: ./generate.sh "prompt" [reference image ...]
set -euo pipefail
prompt=$1; shift
auth="Authorization: Bearer $PIXDROP_TOKEN"
# Status requests and downloads, retried for a minute should the server restart meanwhile
get() { curl -sS --fail --retry 12 --retry-delay 5 --retry-all-errors -H "$auth" "$@"; }

form=(--form-string "prompt=$prompt" --form-string "model=${PIXDROP_MODEL:-}")
for file in "$@"; do form+=(-F "reference=@$file"); done
job=$(curl -sS --fail-with-body -H "$auth" "${form[@]}" "$PIXDROP_URL/api/jobs") || { echo "refused: $job" >&2; exit 1; }
id=$(jq -r .id <<< "$job")
while [ "$(jq -r .status <<< "$job")" = generating ]; do
  job=$(get "$PIXDROP_URL/api/jobs/$id?wait=50")
done

if [ "$(jq -r .status <<< "$job")" = failed ]; then
  echo "job $id failed: $(jq -r .error <<< "$job")" >&2
  exit 1
fi
jq -r '.images[] | .filename + " " + .url' <<< "$job" | while read -r name url; do
  get -o "$name" "$url"
  echo "$name"
done
```

```console
$ ./generate.sh "A pixel-art treasure chest, 32x32 sprite, transparent background"
1793637070533_af88.png
$ PIXDROP_MODEL=nano-banana ./generate.sh "The same knight seen from the back" knight-front.png
1793637071333_051c.png
$ ./generate.sh "A dragon" notes.txt
curl: (22) The requested URL returned error: 400
refused: {"detail":"notes.txt: unsupported image type: use PNG, JPEG, WebP, GIF or SVG"}
$ ./generate.sh "A dragon"
job 1793637071892_4468 failed: openrouter refused the request (HTTP 402): Insufficient credits. Add more using https://openrouter.ai/credits
```

### Generating from Python

The same in Python, with [httpx](https://www.python-httpx.org/) (`pip install httpx`).
`generate()` and `save_images()` can be used from other code; options are passed by name,
such as `generate("A castle", aspect_ratio="16:9", quality="high")`.

```python
"""Generate images: python generate.py "prompt" [reference image ...]

Needs PIXDROP_URL and PIXDROP_TOKEN in the environment (and PIXDROP_MODEL for another
model than the default). Saves the images in the current directory, and exits with the
error if the generation failed.
"""

import os
import sys
import time
from pathlib import Path

import httpx

URL = os.environ["PIXDROP_URL"].rstrip("/")
client = httpx.Client(headers={"Authorization": f"Bearer {os.environ['PIXDROP_TOKEN']}"}, timeout=90)


def error_message(response: httpx.Response) -> str:
    """Why the server refused a request. `detail` is a message, or a list for invalid parameters (422)."""
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError):
        return f"{response.status_code} {response.reason_phrase}"
    if isinstance(detail, list):
        return "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in detail)
    return detail


def get(url: str, **params) -> httpx.Response:
    """A GET request (a path, or a URL from a job), retried for a minute should the server restart meanwhile."""
    for _ in range(12):
        try:
            # params=None keeps the query of a URL such as medium_url; params={} would drop it
            response = client.get(url if url.startswith("http") else URL + url, params=params or None)
            if response.status_code < 500:
                return response
        except httpx.TransportError:
            pass
        time.sleep(5)
    raise RuntimeError(f"the server does not answer GET {url}")


def generate(prompt: str, references: list[Path] = (), model: str | None = None, **options: str | int) -> dict:
    """Generate images and wait until the job is finished; returns the job."""
    files = [("reference", (path.name, path.read_bytes())) for path in references]
    data = {"prompt": prompt, "model": model or "", **{name: str(value) for name, value in options.items()}}
    response = client.post(f"{URL}/api/jobs", data=data, files=files or None)
    if response.status_code != 201:
        raise RuntimeError(error_message(response))
    job = response.json()
    while job["status"] == "generating":
        response = get(f"/api/jobs/{job['id']}", wait=50)
        if response.status_code != 200:
            raise RuntimeError(error_message(response))
        job = response.json()
    return job


def save_images(job: dict, directory: Path = Path(".")) -> list[Path]:
    """Download the job's images in full size into `directory`; returns their paths."""
    saved = []
    for image in job["images"]:
        response = get(image["url"])
        if response.status_code != 200:
            raise RuntimeError(error_message(response))
        path = directory / image["filename"]
        path.write_bytes(response.content)
        saved.append(path)
    return saved


if __name__ == "__main__":
    finished = generate(sys.argv[1], [Path(name) for name in sys.argv[2:]], os.environ.get("PIXDROP_MODEL"))
    if finished["status"] == "failed":
        sys.exit(f"job {finished['id']} failed: {finished['error']}")
    for saved_path in save_images(finished):
        print(saved_path)
```

### Uploading reference images in two steps

Announce the reference images, then upload them to the job's `upload_url`, here one by one:

```bash
curl -H "$AUTH" -F "prompt=The knight walking to the left" -F reference_count=2 $URL/api/jobs
# {"id":"1793635980937_087f","status":"pending",...,"reference_count":2,"references":[],...,"upload_url":"http://localhost:8000/api/jobs/1793635980937_087f/references"}
curl -H "$AUTH" -F reference=@knight-front.png $URL/api/jobs/1793635980937_087f/references
# {"id":"1793635980937_087f","status":"pending",...,"reference_count":2,"references":[{"index":1,...}],...}
curl -H "$AUTH" -F reference=@knight-side.png $URL/api/jobs/1793635980937_087f/references
# {"id":"1793635980937_087f","status":"generating",...}
curl -H "$AUTH" "$URL/api/jobs/1793635980937_087f?wait=50"
```

Sending the images with the job, as the examples above do, is simpler; the two steps help
when the job and the files come from different places, as with AI agents.

### Waiting for a job

There are two ways to learn that a job has finished:

- **Waiting** (recommended): call `GET /api/jobs/{id}?wait=50` in a loop while the status
  is `generating`. Each call returns as soon as the job finishes. Between calls there is no
  need to pause. `POST /api/jobs?wait=50` makes the first wait part of the submission. A
  `pending` job is returned at once: upload its reference images first.
- **Polling**: call `GET /api/jobs/{id}` every few seconds. The web page does this for the
  job list (every 2 seconds while a job is in progress).

In both cases treat connection errors and `5xx` statuses as temporary (the server may be
restarting) and try again after a few seconds, as the examples above do.

How long a generation takes depends on the model and the provider: from a few seconds to a
few minutes, at most `generation_timeout_seconds` (shown by
[`GET /api/info`](#get-apiinfo)).

### Downloading the medium size

`medium_url` gives a smaller version of each image, for a preview or a gallery. It has the
image's format (a PNG for GIFs and SVGs):

```bash
curl -H "$AUTH" -o chest_medium.png "$URL/api/jobs/1793635964825_508b/images/1?size=medium"
```

```python
for image in job["images"]:
    Path(f"{Path(image['filename']).stem}_medium.png").write_bytes(get(image["medium_url"]).content)  # a PNG image here
```

### Listing all jobs

Request pages until all `total` jobs have been seen:

```python
def all_jobs() -> list[dict]:
    jobs, page = [], 1
    while True:
        listed = get("/api/jobs", page=page, per_page=100).json()
        jobs += listed["jobs"]
        if page * listed["per_page"] >= listed["total"]:
            return jobs
        page += 1
```

Jobs made meanwhile push older jobs to later pages, so a job can appear twice: drop
duplicates by `id`.

### Your statistics

```bash
curl -s -H "$AUTH" $URL/api/stats | jq -c '{jobs: .totals.jobs, images: .totals.images, cost_usd: .totals.cost_usd}'
# {"jobs":14,"images":10,"cost_usd":0.4}
```

### Checking the models and limits before sending

[`GET /api/models`](#get-apimodels) and [`GET /api/info`](#get-apiinfo) give what the server
accepts. A client can offer only the options a model has, with its values, and refuse a
reference image that is too large, or too many or too few of them, before sending anything:

```python
models = {model["name"]: model for model in get("/api/models").json()["models"]}
info = get("/api/info").json()["server"]
model = models["nano-banana"]
if not model["min_reference_images"] <= len(references) <= model["max_reference_images"]:
    raise RuntimeError(f"{model['name']} takes {model['min_reference_images']} to {model['max_reference_images']} reference images")
for path in references:
    if path.stat().st_size > info["max_upload_mb"] * 1024**2:
        raise RuntimeError(f"{path} is larger than {info['max_upload_mb']} MB")
allowed = {option["name"]: option["values"] for option in model["options"] if option["type"] == "enum"}
# such as {"aspect_ratio": ["1:1", "16:9", ...], "quality": ["low", "medium", "high"]}
```

## Writing a client

- Send the token in a header, and keep it secret: it gives full access to the user's jobs,
  and lets anyone generate images at the provider's expense. Use HTTPS when the server is
  not on a trusted network.
- Treat `401` as "the token is wrong or was revoked": ask for a new one rather than retry.
- Retry requests that failed with a connection error, a timeout or a `5xx` status after a
  few seconds; they are temporary. Do not retry `4xx` errors unchanged: `detail` says what
  to change. After `429`, wait for one of your jobs to finish.
- Do not resubmit (`POST /api/jobs`) automatically after a broken connection: look at your
  newest job first (`GET /api/jobs/last`), or you may pay for the same images twice.
- Read `detail` as a string, or as a list for `422`. Show it to people; do not parse it.
- Ignore unknown fields in responses: later versions may add some.
- Take the options and their values from [`GET /api/models`](#get-apimodels) rather than
  hard-coding them: the administrator may change the models, and the providers their
  models' options, at any time.
- Set HTTP timeouts above `wait` for waiting requests, and above the upload time for
  requests with reference images.
- Use the URLs the job gives (`url`, `medium_url`, `upload_url`) rather than building them.
- Calls from a web page on another origin do not work (no CORS): call the API from a
  server.

## OpenAPI

The server describes its API in the OpenAPI format, for tools and client generators:

| URL | |
|---|---|
| `/openapi.json` | The OpenAPI document (JSON). |
| `/docs` | Interactive documentation (Swagger UI), where requests can be tried after entering a token under **Authorize**. Linked from the web page's Server Info. |
| `/redoc` | The same documentation, as one page (ReDoc). |

The two documentation pages load their scripts from a public CDN, so they need Internet
access in the browser. This document is the reference for what the OpenAPI description does
not say: how jobs behave, the rules for reference images and options, and the error
messages.
