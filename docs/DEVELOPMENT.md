# Development

## How it works

One process runs [FastAPI](https://fastapi.tiangolo.com/) under uvicorn, on one asyncio
event loop. Each generation is an asyncio task that sends the request to the provider with
an asynchronous HTTP client ([httpx2](https://pypi.org/project/httpx2/), which has httpx's
API) and waits for the answer. Waiting takes no thread and no CPU, so any number of
generations run at the same time while the server keeps answering requests; the work that
does need the CPU (checking and scaling images with Pillow, reading and writing files)
runs in threads. SVGs are drawn with CairoSVG in a helper process (`python -m
pixdrop.svgdraw`), limited in time and memory, as TeXdrop unpacks archives.

There is no database. Each job is one JSON file next to its images, created with an id that
is never reused (the file is opened with `O_EXCL`) and replaced as a whole at each change
of status (written to a temporary file, synced, and renamed), so that readers always see a
whole record. Every change to a job happens under one lock, so a request and the end of the
job's generation cannot interleave; the lock is never held while a provider is generating.
The statistics are computed from the records when they are asked for. What only the running
process needs is kept in memory: the jobs in progress (for `MAX_ACTIVE_JOBS` and the
waiting requests), the deadlines of pending jobs, the transfer links and the medium-size
renders, and the providers' model catalogues, which a background task reads at startup
and every 6 hours. At startup, jobs a restart interrupted are marked failed.

The code is in `src/pixdrop/`, one module per concern, each starting with a docstring that
says what it does: `settings`, `users` (tokens), `watched` (the configuration files,
re-read when they change), `config` (providers, models and the options), `discovery`
(the providers' model catalogues, which give the models' options), `client` (the client's
address), `auth`, `sessions` (the web page's logins), `storage` (file layout), `store` (job
records), `images` (recognising, checking and scaling images), `svgdraw` (draws SVGs in the
helper process), `providers` (the requests to OpenRouter-compatible APIs), `jobs` (job
lifecycle), `worker` (the generations), `links` (transfer links), `stats`, `api`,
`mcp_server` (the MCP server for AI agents, whose tools call `api`'s code), `web`, `logs`,
`errors`, `app` (wiring), `accounts` (the commands' changes to the users file), `configure`
(those to `config.json`, with `default_config.json`, the default models) and `__main__` (the
server, and the `setup`, `modify-models`, `adduser`, `reset-token`, `stats` and `models`
commands). The web page is plain HTML, CSS and JavaScript in `src/pixdrop/static/`, with no
build step. What keeps users apart is described in [SECURITY.md](SECURITY.md).

## Running it locally

`tests/fake_provider.py` is a fake OpenRouter: it answers the two image endpoints with a
picture drawn from the prompt (a colour of its own and the prompt's first words), at no
cost and without a key, and lists three models in its catalogue (`GET
/api/v1/images/models`): `fake/images` (most options, up to 4 reference images, and a
parameter PixDrop does not know), `fake/chat` (a model of chat completions) and
`fake/editor` (it only edits: 1 or 2 reference images). `GET /api/v1/key` accepts any key
but `rejected-key`. Start it, and point a provider at it by hand, since `setup` writes
OpenRouter's address and models:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # needs the cairo library (Debian: libcairo2)
.venv/bin/python tests/fake_provider.py --port 9000 &        # fake provider at http://127.0.0.1:9000/api/v1

mkdir -p data
DATA_DIR=data .venv/bin/python -m pixdrop adduser dev        # prints the token to log in with
cat > data/config.json <<'EOF'
{
  "providers": {"fake": {"base_url": "http://127.0.0.1:9000/api/v1", "api_key": "anything"}},
  "models": {
    "fake-images": {"provider": "fake", "model": "fake/images", "api_type": "images"},
    "fake-chat": {"provider": "fake", "model": "fake/chat", "api_type": "chat"},
    "fake-editor": {"provider": "fake", "model": "fake/editor", "api_type": "images"}
  }
}
EOF
DATA_DIR=data .venv/bin/python -m pixdrop
```

Then open `http://localhost:8000` and log in with the token `adduser` printed. The models
take their options from the fake catalogue (`DATA_DIR=data .venv/bin/python -m pixdrop
models` lists it). The `data/` directory is ignored by git. Words in a prompt make the fake provider misbehave, to try the
error paths: `FAIL` (an error status), `FAIL200` (an error in an answer with status `200`),
`NOIMAGE` (an answer without an image), `BADIMAGE` (an image that is not one), `SLOW`
(answers after 5 seconds) and `HANG` (answers after 600 seconds, beyond any timeout).

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # needs the cairo library (Debian: libcairo2)
.venv/bin/python -m pytest -m "not docker"                  # unit and integration tests
.venv/bin/python -m pytest -m docker                         # builds the image and tests the container
```

The tests never reach a real provider: the unit and integration tests replace the network
with a fake transport, and the Docker tests run the containers against
`tests/fake_provider.py`, which they reach through the host's gateway. Set `TEST_IMAGE` to
test an existing image instead of building one. The GitHub workflows are started manually:
**Run Test Suite** runs both test groups, and **Build and Export Docker Image** builds the
image and saves it as an artifact.
