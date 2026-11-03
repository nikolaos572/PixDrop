"""The MCP server at /mcp and its transfer links, over HTTP as agents use them."""

import asyncio
import itertools
import logging
import re
import shlex
import signal

import httpx2
import pytest
from fastapi.testclient import TestClient
from helpers import (
    ALICE,
    BOB,
    TOKENS,
    access_lines,
    free_port,
    generate,
    gif,
    png,
    record_of,
    reference,
    share,
    size_of,
    start_server,
    submit,
    svg,
    write_json,
)
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from pixdrop import __version__
from pixdrop.links import REFUSAL

TOOLS = {"server_info", "list_models", "generate_image", "upload_link", "get_job", "last_job", "list_jobs", "download_link", "get_stats"}
EXPIRES = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+0[23]:00"  # in the server's time zone (Europe/Athens)


class ToolFailed(Exception):
    """A tool call answered with an error, which the model reads."""


class Agent:
    """Alice's agent: an MCP client speaking JSON-RPC over HTTP, which runs the transfers the tools
    describe with another HTTP client, without the token (as curl would)."""

    def __init__(self, mcp: TestClient, curl: TestClient) -> None:
        self.mcp = mcp
        self.curl = curl
        self._ids = itertools.count(1)

    def rpc(self, method: str, params: dict | None = None, **request) -> dict:
        response = self.mcp.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"} | request.pop("headers", {}),
            json={"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}},
            **request,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def call(self, tool: str, **arguments) -> dict:
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})["result"]
        if result.get("isError"):
            # The SDK puts "Error executing tool <name>: " before PixDrop's message.
            raise ToolFailed(result["content"][0]["text"].removeprefix(f"Error executing tool {tool}: "))
        return result["structuredContent"]

    def upload(self, link: dict, *files: tuple[str, bytes]) -> httpx2.Response:
        """What the upload command does: curl -F reference=@FILE ... URL."""
        assert link["method"] == "POST"
        return self.curl.post(link["url"], files=[reference(name, data) for name, data in files])

    def download(self, link: dict) -> httpx2.Response:
        assert link["method"] == "GET"
        return self.curl.get(link["url"])


@pytest.fixture
def agent(client):
    return Agent(client, share(client))


def downloads(job: dict) -> dict[tuple[int, str], dict]:
    return {(item["image"], item["size"]): item for item in job["downloads"]}


# --- the endpoint ---


def test_the_agent_finds_the_tools_and_how_to_use_them(agent):
    hello = agent.rpc(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )["result"]
    assert (hello["serverInfo"]["name"], hello["serverInfo"]["version"]) == ("PixDrop", __version__)
    for hint in ("list_models", "generate_image", "get_job", "reference_images", "upload", "60 minutes", "512 px", "1 MB"):
        assert hint in hello["instructions"]

    tools = {tool["name"]: tool for tool in agent.rpc("tools/list")["result"]["tools"]}
    assert set(tools) == TOOLS
    assert all(tool["description"] and tool["title"] for tool in tools.values())
    for name in ("server_info", "list_models", "list_jobs", "get_stats"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    for name in ("generate_image", "upload_link", "download_link", "get_job"):
        assert tools[name]["annotations"]["readOnlyHint"] is False
        assert tools[name]["annotations"]["destructiveHint"] is False  # nothing is ever deleted
    assert tools["generate_image"]["annotations"]["openWorldHint"] is True
    schema = tools["generate_image"]["inputSchema"]
    assert schema["required"] == ["prompt"]
    assert schema["properties"]["wait_seconds"]["maximum"] == 50
    assert schema["properties"]["reference_images"]["maximum"] == 16
    assert "list_models" in schema["properties"]["aspect_ratio"]["description"]  # each model has its own values
    assert tools["get_job"]["inputSchema"]["properties"]["wait_seconds"]["maximum"] == 50
    assert not any("delete" in name for name in tools)


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_the_endpoint_answers_only_post(agent, method):
    assert agent.mcp.request(method, "/mcp").status_code == 405


def test_mcp_jobs_are_marked_as_such(agent, data_dir):
    job = agent.call("generate_image", prompt="a red fox")
    assert job["source"] == "mcp"
    assert record_of(data_dir, "alice", job["id"])["source"] == "mcp"


# --- generating and downloading ---


def test_generate_image_waits_and_lists_the_downloads(agent, data_dir):
    job = agent.call("generate_image", prompt="a red fox", aspect_ratio="16:9")
    assert (job["status"], job["error"], job["model"], job["options"]) == ("completed", None, "gpt-image", {"quality": "high", "aspect_ratio": "16:9"})
    assert (job["upload"], job["upload_url"]) == (None, None)
    [image] = job["images"]
    assert (image["url"], image["medium_url"], image["width"], image["height"]) == (None, None, 1024, 576)  # no API URLs: they need the token
    assert job["usage"]["cost_usd"] == 0.04

    links = downloads(job)
    assert set(links) == {(1, "full"), (1, "medium")}
    full, medium = links[1, "full"], links[1, "medium"]
    for link in (full, medium):
        assert link["method"] == "GET"
        assert link["url"].startswith("http://testserver/api/links/")
        assert re.fullmatch(EXPIRES, link["expires_at"])
    assert full["command"] == f"curl -sS --fail -o {job['id']}.png {full['url']}"
    assert medium["command"] == f"curl -sS --fail -o {job['id']}_medium.png {medium['url']}"
    assert shlex.split(full["command"])[-1] == full["url"]

    response = agent.download(full)  # without the token: the link stands in for it
    assert response.content == (data_dir / "outputs" / "alice" / f"{job['id']}.png").read_bytes()
    again = agent.download(full)
    assert (again.status_code, again.json()) == (404, {"detail": REFUSAL})  # used up
    small = agent.download(medium)
    assert (small.headers["content-type"], size_of(small.content)) == ("image/png", (512, 288))


def test_several_images_each_get_two_downloads(agent):
    job = agent.call("generate_image", prompt="sprites", n=2, output_format="jpeg")
    assert set(downloads(job)) == {(1, "full"), (1, "medium"), (2, "full"), (2, "medium")}
    assert f"-o {job['id']}_2.jpg " in downloads(job)[2, "full"]["command"]
    assert f"-o {job['id']}_2_medium.jpg " in downloads(job)[2, "medium"]["command"]
    assert agent.download(downloads(job)[2, "medium"]).headers["content-type"] == "image/jpeg"


def test_links_work_only_from_the_address_that_asked_for_them(agent, client):
    job = agent.call("generate_image", prompt="a red fox")
    link = downloads(job)[1, "full"]
    elsewhere = share(client, client=("10.0.0.9", 50000))
    assert (elsewhere.get(link["url"]).status_code, elsewhere.get(link["url"]).json()) == (404, {"detail": REFUSAL})
    assert agent.download(link).status_code == 200  # still valid where it belongs


def test_behind_a_proxy_links_follow_the_forwarded_address(agent):
    forwarded = {"X-Forwarded-For": "203.0.113.7"}
    result = agent.rpc("tools/call", {"name": "generate_image", "arguments": {"prompt": "a red fox"}}, headers=forwarded)
    link = downloads(result["result"]["structuredContent"])[1, "full"]
    assert agent.curl.get(link["url"]).status_code == 404  # testclient
    assert agent.curl.get(link["url"], headers={"X-Forwarded-For": "203.0.113.8"}).status_code == 404
    assert agent.curl.get(link["url"], headers=forwarded).status_code == 200


def test_generate_image_returns_after_wait_seconds(agent, upstream):
    job = agent.call("generate_image", prompt="GATE", wait_seconds=0)
    assert (job["status"], job["downloads"], job["upload"]) == ("generating", [], None)
    assert agent.call("get_job", job_id=job["id"], wait_seconds=0)["status"] == "generating"
    with pytest.raises(ToolFailed, match=f"^job {job['id']} has no image 1 yet: it is generating$"):
        agent.call("download_link", job_id=job["id"])
    upstream.gate.set()
    done = agent.call("get_job", job_id=job["id"])  # waits until it is done
    assert done["status"] == "completed" and len(done["downloads"]) == 2


def test_a_failed_generation_says_why(agent):
    job = agent.call("generate_image", prompt="FAIL")
    assert (job["status"], job["downloads"]) == ("failed", [])
    assert job["error"] == "openrouter refused the request (HTTP 402): Insufficient credits. Add more using https://openrouter.ai/credits"


# --- reference images ---


def test_reference_images_through_an_upload_link(agent, upstream):
    job = agent.call("generate_image", prompt="the knight from behind", reference_images=2)
    assert (job["status"], job["reference_count"], job["downloads"], job["upload_url"]) == ("pending", 2, [], None)
    upload = job["upload"]
    assert upload["method"] == "POST" and re.fullmatch(EXPIRES, upload["expires_at"])
    assert upload["command"] == f"curl -sS --fail-with-body -F reference=@REFERENCE_1.png -F reference=@REFERENCE_2.png {upload['url']}"
    assert upstream.bodies == []

    response = agent.upload(upload, ("front.png", png()), ("side.gif", gif()))
    assert response.status_code == 200
    started = response.json()
    assert (started["status"], [picture["filename"] for picture in started["references"]]) == ("generating", ["1_front.png", "2_side.gif"])
    assert agent.upload(upload, ("again.png", png())).status_code == 404  # used up

    done = agent.call("get_job", job_id=job["id"])
    assert done["status"] == "completed"
    assert len(upstream.bodies[-1]["input_references"]) == 2


def test_a_partial_upload_asks_for_the_rest(agent):
    job = agent.call("generate_image", prompt="three views", reference_images=3)
    assert agent.upload(job["upload"], ("front.png", png())).json()["status"] == "pending"
    rest = agent.call("upload_link", job_id=job["id"])
    remaining = "curl -sS --fail-with-body -F reference=@REFERENCE_2.png -F reference=@REFERENCE_3.png "
    assert rest["command"] == remaining + rest["url"]
    again = agent.call("get_job", job_id=job["id"], wait_seconds=0)["upload"]  # get_job hands out a new one too
    assert again["command"] == remaining + again["url"] and again["url"] != rest["url"]
    assert agent.upload(rest, ("side.png", png()), ("back.png", png())).json()["status"] == "generating"


def test_upload_link_is_only_for_pending_jobs(agent, client):
    done = agent.call("generate_image", prompt="a red fox")
    with pytest.raises(ToolFailed, match="^the job is completed: only a pending job takes reference images$"):
        agent.call("upload_link", job_id=done["id"])
    with pytest.raises(ToolFailed, match="^no such job$"):
        agent.call("upload_link", job_id="1793456280412_a9f3")
    bobs = submit(client, reference_count=1, headers=BOB).json()
    with pytest.raises(ToolFailed, match="^no such job$"):
        agent.call("upload_link", job_id=bobs["id"])


def test_too_many_reference_images_for_the_model(agent):
    with pytest.raises(ToolFailed, match="^model 'nano-banana' accepts at most 3 reference image\\(s\\), got 4$"):
        agent.call("generate_image", prompt="x", model="nano-banana", reference_images=4)


# --- download links ---


def test_download_link_for_images_and_references(agent):
    job = generate(agent.mcp, references=[("knight.png", png(64, 48)), ("logo.svg", svg('width="100" height="50"'))])
    link = agent.call("download_link", job_id=job["id"])
    assert (link["method"], link["command"]) == ("GET", f"curl -sS --fail -o {job['id']}.png {link['url']}")
    assert agent.download(link).status_code == 200

    knight = agent.call("download_link", job_id=job["id"], image=1, reference=True)
    assert knight["command"].startswith(f"curl -sS --fail -o {job['id']}_reference_1_knight.png ")
    assert agent.download(knight).content == png(64, 48)
    logo = agent.call("download_link", job_id="last", image=2, size="medium", reference=True)
    assert logo["command"].startswith(f"curl -sS --fail -o {job['id']}_reference_2_logo_medium.png ")
    assert size_of(agent.download(logo).content) == (512, 256)


def test_download_link_refuses_what_does_not_exist(agent, client):
    job = agent.call("generate_image", prompt="a red fox")
    with pytest.raises(ToolFailed, match=f"^job {job['id']} has no image 2$"):
        agent.call("download_link", job_id=job["id"], image=2)
    with pytest.raises(ToolFailed, match=f"^job {job['id']} has no reference image 1$"):
        agent.call("download_link", job_id=job["id"], reference=True)
    bobs = generate(client, headers=BOB)
    with pytest.raises(ToolFailed, match="^no such job$"):
        agent.call("download_link", job_id=bobs["id"])


# --- the other tools, which do what their API endpoints do ---


def test_last_job(agent, client):
    with pytest.raises(ToolFailed, match="^you have no jobs yet$"):
        agent.call("last_job")
    generate(client, "through the API")
    newest = generate(client, "also through the API")
    last = agent.call("last_job")
    assert (last["id"], last["prompt"], len(last["downloads"])) == (newest["id"], "also through the API", 2)


def test_list_jobs_without_api_urls(agent, client):
    ids = [generate(client, f"job {n}")["id"] for n in range(3)]
    pending = submit(client, reference_count=1).json()
    listed = agent.call("list_jobs", per_page=2)
    assert ([job["id"] for job in listed["jobs"]], listed["total"], listed["page"], listed["per_page"]) == ([pending["id"], ids[2]], 4, 1, 2)
    assert agent.call("list_jobs", page=2, per_page=2)["jobs"][1]["id"] == ids[0]
    assert agent.call("list_jobs", page=9)["jobs"] == []
    assert agent.call("list_jobs")["per_page"] == 10
    api = client.get("/api/jobs").json()
    for job in api["jobs"]:  # the same jobs, without the URLs that need the token
        job["upload_url"] = None
        for picture in job["images"] + job["references"]:
            picture["url"] = picture["medium_url"] = None
    assert agent.call("list_jobs") == api | {"per_page": 10}
    assert "/api/jobs/" not in str(agent.call("list_jobs"))


def test_server_info_list_models_and_get_stats_are_the_apis(agent, client):
    generate(client)
    agent.call("generate_image", prompt="x")
    assert agent.call("server_info") == client.get("/api/info").json()
    assert agent.call("list_models") == client.get("/api/models").json()
    stats = agent.call("get_stats")
    assert stats == client.get("/api/stats").json()
    assert stats["sources"] == {"api": 1, "mcp": 1}


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("generate_image", {"prompt": "x", "model": "nope"}, "^unknown model 'nope'; available: gpt-image, nano-banana, plain$"),
        ("generate_image", {"prompt": "x", "model": "nano-banana", "quality": "high"},
         "^model 'nano-banana' does not support 'quality'; its options: aspect_ratio, resolution, seed$"),
        ("generate_image", {"prompt": "x", "model": "nano-banana", "n": 2}, "^model 'nano-banana' does not support 'n'"),
        ("generate_image", {"prompt": "x", "size": "big"}, "^size must match"),
        ("generate_image", {"prompt": "x", "background": "transparent", "output_format": "jpeg"}, "transparent background needs"),
        ("generate_image", {"prompt": "   "}, "^the prompt is empty$"),
        ("get_job", {"job_id": "1793456280412_a9f3"}, "^no such job$"),
    ],
)  # fmt: skip
def test_refusals_reach_the_agent_as_tool_errors(agent, data_dir, tool, arguments, message):
    with pytest.raises(ToolFailed, match=message):
        agent.call(tool, **arguments)
    assert not (data_dir / "outputs" / "alice").exists()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("generate_image", {"prompt": ""}),
        ("generate_image", {"prompt": "x" * 32_001}),
        ("generate_image", {"prompt": "x", "n": 11}),
        ("generate_image", {"prompt": "x", "output_compression": 101}),
        ("generate_image", {"prompt": "x", "seed": -1}),
        ("generate_image", {"prompt": "x", "reference_images": 17}),
        ("generate_image", {"prompt": "x", "wait_seconds": 51}),
        ("generate_image", {}),
        ("get_job", {"job_id": "../../users"}),
        ("get_job", {"job_id": "last", "wait_seconds": -1}),
        ("upload_link", {"job_id": "last"}),
        ("download_link", {"job_id": "last", "image": 0}),
        ("download_link", {"job_id": "last", "size": "small"}),
        ("list_jobs", {"per_page": 101}),
        ("list_jobs", {"page": 0}),
    ],
)
def test_tool_arguments_are_validated(agent, tool, arguments):
    with pytest.raises(ToolFailed, match="(?i)validation error"):
        agent.call(tool, **arguments)


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"quality": "ultra"}, "quality must be one of: auto, low, medium, high, xhigh, max"),
        ({"aspect_ratio": "7:3"}, "aspect_ratio must be one of: 1:1"),
    ],
)
def test_option_values_are_checked_against_the_model(agent, arguments, error):
    # Each model has its own values, so the server checks them (list_models gives them), not the tool's schema.
    with pytest.raises(ToolFailed, match=re.escape(error)):
        agent.call("generate_image", prompt="x", **arguments)


def test_the_limit_of_jobs_in_progress_applies(make_app):
    with TestClient(make_app(max_active_jobs=1), headers=dict(ALICE)) as client:
        agent = Agent(client, share(client))
        agent.call("generate_image", prompt="x", reference_images=1)
        with pytest.raises(ToolFailed, match="^you have 1 job\\(s\\) in progress, the most allowed \\(MAX_ACTIVE_JOBS\\)"):
            agent.call("generate_image", prompt="y")


def test_tools_cannot_reach_other_users_jobs(agent, client):
    bobs = generate(client, headers=BOB)
    with pytest.raises(ToolFailed, match="^no such job$"):
        agent.call("get_job", job_id=bobs["id"], wait_seconds=0)
    assert agent.call("list_jobs")["total"] == 0
    bob_agent = Agent(share(client, headers=BOB), agent.curl)
    assert bob_agent.call("last_job")["id"] == bobs["id"]


def test_what_the_model_reads_of_a_tool_error(agent):
    result = agent.rpc("tools/call", {"name": "get_job", "arguments": {"job_id": "1793456280412_a9f3"}})["result"]
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "Error executing tool get_job: no such job"}]


def test_tool_errors_reach_the_access_log(agent, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(ToolFailed, match="no such job"):
        agent.call("get_job", job_id="1793456280412_a9f3")
    assert access_lines(caplog)[-1] == "testclient alice POST /mcp 200 N ms: no such job"


def test_links_use_public_base_url(make_app):
    with TestClient(make_app(public_base_url="https://pixdrop.example.com"), headers=dict(ALICE)) as client:
        agent = Agent(client, share(client))
        job = agent.call("generate_image", prompt="x")
        assert all(item["url"].startswith("https://pixdrop.example.com/api/links/") for item in job["downloads"])
        pending = agent.call("generate_image", prompt="x", reference_images=1)
        assert pending["upload"]["url"].startswith("https://pixdrop.example.com/api/links/")
        assert client.get("/api/info").json()["server"]["mcp_url"] == "https://pixdrop.example.com/mcp"


def test_links_of_removed_users_stop_working(agent, data_dir):
    job = agent.call("generate_image", prompt="x")
    write_json(data_dir / "users.json", {"users": {"bob": TOKENS["bob"]}})
    assert agent.download(downloads(job)[1, "full"]).json() == {"detail": REFUSAL}


# --- the real server, with the official MCP client ---


@pytest.fixture
def server(real_data):
    """The real server, using the fake provider over HTTP, and its base URL."""
    port = free_port()
    process = start_server(real_data, port, ENABLE_MCP="1")
    yield f"http://127.0.0.1:{port}"
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=30)


async def official_client(url: str, steps):
    """Connect as Alice with the SDK's client (as Claude Code and other agents do), and run `steps`."""
    async with (
        httpx2.AsyncClient(headers=dict(ALICE), timeout=60) as http,
        Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client,
    ):
        return await steps(client)


def test_generate_with_the_official_client(server):
    async def steps(client):
        assert {tool.name for tool in (await client.list_tools()).tools} == TOOLS
        pending = (await client.call_tool("generate_image", {"prompt": "the knight", "model": "fake-chat", "reference_images": 1})).structured_content
        httpx2.post(pending["upload"]["url"], files=[reference("knight.png", png())]).raise_for_status()
        return (await client.call_tool("get_job", {"job_id": pending["id"]})).structured_content

    job = asyncio.run(official_client(server, steps))
    assert (job["status"], job["error"], job["source"]) == ("completed", None, "mcp")
    full = next(item for item in job["downloads"] if item["size"] == "full")
    image = httpx2.get(full["url"])
    assert (image.status_code, size_of(image.content)) == (200, (1024, 1024))
    assert httpx2.get(full["url"]).status_code == 404
