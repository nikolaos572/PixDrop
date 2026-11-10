# Agent guidelines

PixDrop is a FastAPI service that generates images through OpenRouter-compatible
providers, for people (a web page), scripts (a JSON API) and AI agents (an MCP server).
Python 3.11+, no database (one JSON record per job, next to its images), no frontend build step.

- `src/pixdrop/`: one module per concern, each starting with a docstring that says what
  it does; `static/` is the web page (plain HTML, CSS and JavaScript).
- `tests/`: pytest, with the providers faked (`tests/fake_provider.py`); `test_docker.py`
  builds the image and tests the container.
- `docs/`: deployment, configuration (every setting), API, MCP, security, development.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -e ".[dev]"   # needs the cairo library
.venv/bin/python -m pytest -m "not docker"                  # unit and integration tests
.venv/bin/python -m pytest -m docker                         # the Docker image
```

## Guidelines

- Keep it simple: the smallest change that solves the problem, in the style of the surrounding code.
- One responsibility per module; no duplicated logic. The web page, the API and the MCP server share one code path.
- Never trust client input. Every check the web UI makes is repeated by the server.
- Generated images and job records are the user's history: nothing in the service deletes them.
- Images never travel as base64 to clients; they get URLs (or transfer links, for agents).
- Everything works the same with and without Docker; settings are environment variables, read in `settings.py`.
- Every behaviour change comes with tests; run `python -m pytest -m "not docker"` (and `-m docker` for image changes).
- Discuss design changes before implementing them; update `README.md` and `docs/` when behaviour or configuration changes.
