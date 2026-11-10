# AGENTS.md

Guidance for anyone changing this project, people and AI coding agents alike.
These are defaults, not hard rules. If a task needs a different approach, take
it, explain why, and update this file and the docs to match. When this file and
the code disagree, the code wins, and this file should be fixed.

## The project

PixDrop is a small self-hosted service that generates images with AI models
through OpenRouter (or any API like it), for people (a web page), programs (an
HTTP API) and AI agents (an MCP server). It runs as a single Docker container.
Its sibling, [TeXdrop](https://github.com/nikolaos572/TeXdrop), compiles LaTeX
the same way: where the two do the same thing, keep them alike.

- What it does and how to run it: `README.md`
- Building, running, the data directory, reverse proxies and logs: `docs/DEPLOYMENT.md`
- Users, providers and models, and every setting: `docs/CONFIGURATION.md`
- The HTTP API: `docs/API.md`
- The MCP server for AI agents: `docs/MCP.md`
- Security model: `docs/SECURITY.md`
- How it works, local setup and the tests: `docs/DEVELOPMENT.md`

Python 3.11+ and FastAPI, no database (one JSON record per job, next to its
images), no frontend build step.

- `app/`: the `pixdrop` package, one module per concern, each starting with a
  docstring that says what it does; `app/static/` is the web page (plain HTML,
  CSS and JavaScript).
- `tests/`: pytest, with the providers faked (`tests/fake_provider.py`);
  `test_docker.py` builds the image and tests the container.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -e ".[dev]"   # needs the cairo library
.venv/bin/python -m pytest -m "not docker"   # unit and integration tests
.venv/bin/python -m pytest -m docker         # the Docker image
```

## Working on a task

- If a request is unclear or could mean more than one thing, ask before writing
  code. Don't guess at requirements.
- State the assumptions you make.
- Keep changes focused on what was asked. If you notice something else worth
  fixing, mention it rather than quietly changing it.
- For bigger changes, such as new dependencies, a different data or config
  format, or restructuring, describe the plan and get agreement first.
- Ask before anything destructive: deleting data, force-pushing, rewriting
  history.
- Prefer the simplest solution that works. This is a small service; keep it small.

## Code

- Follow the patterns and style already in the codebase.
- One responsibility per module, and no duplicate code: reuse or extend existing
  helpers before writing new ones. The web page, the API and the MCP server
  share one code path.
- Comments explain why, not what. Remove dead code instead of commenting it out.
- Settings are environment variables, read and checked in `settings.py`.
  Everything works the same with and without Docker.
- Add a dependency only when it clearly earns its place, and say why. Then make
  `requirements.txt` again, as `docs/DEVELOPMENT.md` describes.
- Fail with clear error messages, especially for configuration mistakes.
- Images never travel as base64 to clients: they get URLs, or transfer links
  for agents.
- The server is one process, which keeps the jobs in progress in memory.
- The web page has no inline JavaScript or CSS: the Content-Security-Policy blocks
  them. UI changes should work in light and dark mode, on phone-sized screens
  and with the keyboard.

## Tests

- New features and bug fixes come with tests. For a bug, where practical, first
  write a test that fails because of it.
- Run the full test suite before calling a change done. Fix failures; don't
  skip or weaken tests to make them pass.
- If you change how the container is built or run, run the Docker tests too.
- Never claim something works or tests pass without running them. If you
  couldn't run something, say so.

## Documentation

Docs are part of the change, not a follow-up.

- Added, renamed or removed an environment variable: update the table in
  `docs/CONFIGURATION.md`, and `compose.yaml` if it's commonly used.
- Changed behavior users can see: update `README.md` and the relevant docs page.
- Changed an endpoint: update `docs/API.md`; a tool of the MCP server:
  `docs/MCP.md`.
- Touched authentication, uploads, headers or permissions: make sure
  `docs/SECURITY.md` is still true.
- Changed setup, commands or tests: update `docs/DEVELOPMENT.md`.
- Visibly changed the UI: retake the README screenshot.
- Write for someone installing from scratch. Keep it short and concrete.

## Security and privacy

- Treat every request, upload and header as untrusted input. Every check the
  web page makes is repeated by the server.
- Generated images and job records are the users' history: nothing in the
  service deletes them.
- Tokens and the providers' keys never reach the log, and keys never reach
  clients.
- Don't weaken existing protections, such as the Content-Security-Policy, the
  Origin check, the upload checks or the non-root container, without discussing
  it first.
- New settings should default to the safe option.
- Never commit secrets, real tokens or keys, personal data or real domains. Use
  example.com and obvious placeholders in docs, tests and screenshots.

## Git

- Don't commit or push directly to `main` unless the user explicitly asks for
  it. Work on a branch and open a pull request instead.
- AI agents: commit or push only when asked.
- Small, focused commits whose message says what changed and why.
- Call out breaking changes (configuration, data layout, file ownership) in the
  commit message.
- Don't commit generated files, local data, virtual environments or build output.
- Keep personal information, internal hostnames and links to private chat
  sessions out of commits and PR descriptions.
