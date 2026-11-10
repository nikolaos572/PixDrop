# PixDrop

Generate images with AI models, for AI agents and for people: text to image, image to
image, and text and image to image, such as game sprites and assets, icons, or the other
sides of a character drawn from one side. PixDrop calls [OpenRouter](https://openrouter.ai)
(or any API like it) with the administrator's key, and gives each user a token, a history
and statistics.

![The web page: the Generate form and the history of generated images](docs/screenshot.png)

- **Any image model of OpenRouter**, with the options each one really accepts (aspect ratio,
  resolution, quality, transparent background, SVG, seed, several images at once, ...), read
  from OpenRouter's catalogue: nothing to look up or keep up to date by hand.
- **Reference images**: up to 16 PNG, JPEG, WebP, GIF or SVG images with the prompt, to
  change an image or to draw new ones in its style.
- **Full and medium size**: every image as generated, or scaled down to 512 pixels.
- **Usage and cost** of every job, as the provider reports them, and statistics by model and
  by month, per user and for all users.
- **Per-user tokens and history**, several generations at the same time, and nothing is ever
  deleted.
- **A web page** to write a prompt, choose a model and its options, add reference images
  (drop, choose or paste them) and follow the history: each job's images, cost and errors,
  downloads, and its prompt or images to reuse; with statistics, the server's settings, and a
  light or dark theme.
- **An HTTP API** for programs, and an **MCP server** through which AI agents such as Claude
  Code, Codex and Antigravity generate images, upload references and save the results by
  themselves.

Built with **Python 3.13, FastAPI and uvicorn**. No database: one JSON record per job, next
to its images.

## Run it

```bash
docker build -t pixdrop .

docker run -d --name pixdrop \
  --restart unless-stopped \
  -p 8000:8000 \
  -e TZ=Europe/Athens \
  -v pixdrop-data:/data \
  pixdrop

docker exec -it pixdrop python -m pixdrop setup       # asks for your OpenRouter key, adds the default models
docker exec pixdrop python -m pixdrop adduser alice   # prints alice's token
```

Then open `http://your-host:8000` and log in with the token. `--restart unless-stopped`
starts PixDrop again after a crash or a reboot. Extra hardening (`--cap-drop ALL`,
`--security-opt no-new-privileges`) and other options are in
[Deployment](docs/DEPLOYMENT.md#options). Add users the same way, at any time;
`python -m pixdrop reset-token alice` gives alice a new token if theirs is lost or leaked.
To let AI agents use PixDrop, add `-e ENABLE_MCP=1` and connect them as
[MCP.md](docs/MCP.md#connecting-an-agent) shows.

> Mount the volume at **`/data`**: that is where PixDrop keeps the users, the models, the
> images and the log. To keep them in a directory on the host instead, give it to
> **UID/GID 61001**, the user PixDrop runs as, first: `sudo chown 61001:61001 /your/dir`.
> See [Deployment](docs/DEPLOYMENT.md#a-directory-on-the-host).

> It also works behind an HTTPS reverse proxy as it is. There, set `PUBLIC_BASE_URL` to the
> address users reach (such as `https://pixdrop.example.com`), so that browsers may send
> changes only from pages at that address: see [Reverse proxies](docs/DEPLOYMENT.md#reverse-proxies).

## Documentation

- [Deployment](docs/DEPLOYMENT.md): building, running with or without Docker, the data
  directory, cleaning up, the statistics of all users, reverse proxies, container limits and
  logs.
- [Configuration](docs/CONFIGURATION.md): the users, the providers and models, every
  environment variable, and the fixed limits.
- [API](docs/API.md): every endpoint, how jobs behave, and examples in the shell and Python.
- [MCP server](docs/MCP.md): connecting AI agents, the tools and the transfer links.
- [Security](docs/SECURITY.md): what is protected and what is not, and how to report a
  vulnerability.
- [Development](docs/DEVELOPMENT.md): how it works, running it with a fake provider, the
  tests, and updating the dependencies.

## License

[MIT](LICENSE)
