"""Requests to the providers (OpenRouter's two image APIs), their answers, and their errors."""

import asyncio
import base64
import json

import httpx2
import pytest
from helpers import API_KEY, BASE_URL, CONFIG, config_with, png

from pixdrop import providers
from pixdrop.config import parse_config
from pixdrop.providers import Generation, GenerationError, ProviderClient, chat_request, images_request
from pixdrop.store import Usage

MODELS = parse_config(CONFIG, env={}).models
PNG = png(8, 8)
PNG_B64 = base64.b64encode(PNG).decode()
DATA_URL = f"data:image/png;base64,{PNG_B64}"
REFERENCE = "data:image/jpeg;base64,/9j/AAAA"


class Upstream:
    """Answers each request with the next queued answer (a response, or an exception to raise), and
    keeps the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def body(self) -> dict:
        return json.loads(self.requests[0].content)


def generate(upstream, model="gpt-image", prompt="a red fox", options=None, references=(), config=None) -> Generation:
    chosen = parse_config(config, env={}).models[model] if config else MODELS[model]

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(upstream)) as http:
            return await ProviderClient(http).generate(chosen, prompt, options or {}, list(references))

    return asyncio.run(run())


def images_answer(*images, **extra):
    return httpx2.Response(200, json={"data": [{"b64_json": base64.b64encode(i).decode()} for i in images], **extra})


def chat_answer(message=None, **extra):
    message = {"role": "assistant", "images": [{"type": "image_url", "image_url": {"url": DATA_URL}}]} if message is None else message
    return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}], **extra})


def fails(upstream, message, **arguments):
    with pytest.raises(GenerationError) as caught:
        generate(upstream, **arguments)
    assert str(caught.value) == message
    return caught.value


# --- api_type "images" ---


def test_the_images_request():
    upstream = Upstream(images_answer(PNG))
    generate(upstream, options={"quality": "high", "n": 2, "size": "1024x1536"})
    request = upstream.requests[0]
    assert (request.method, str(request.url)) == ("POST", f"{BASE_URL}/images")
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["X-OpenRouter-Title"] == "PixDrop"  # the provider's own headers
    assert request.headers["Content-Type"] == "application/json"
    assert upstream.body == {"model": "openai/gpt-5-image", "prompt": "a red fox", "quality": "high", "n": 2, "size": "1024x1536"}


def test_reference_images_go_in_input_references():
    body = images_request(MODELS["gpt-image"], "the knight, from the back", {}, [DATA_URL, REFERENCE])
    assert body == {
        "model": "openai/gpt-5-image",
        "prompt": "the knight, from the back",
        "input_references": [
            {"type": "image_url", "image_url": {"url": DATA_URL}},
            {"type": "image_url", "image_url": {"url": REFERENCE}},
        ],
    }
    assert "input_references" not in images_request(MODELS["gpt-image"], "x", {}, [])


def test_the_images_answer():
    upstream = Upstream(
        images_answer(PNG, b"second", id="img-1", usage={"prompt_tokens": 12, "completion_tokens": 2580, "total_tokens": 2592, "cost": 0.08})
    )
    generation = generate(upstream)
    assert generation == Generation(
        images=[PNG, b"second"], usage=Usage(12, 2580, 2592, 0.08), text=None, upstream_id="img-1"
    )


def test_images_given_as_links_are_downloaded_without_the_key():
    upstream = Upstream(
        httpx2.Response(200, json={"data": [{"url": "https://cdn.example.com/a.png"}, {"b64_json": PNG_B64}]}),
        httpx2.Response(200, content=b"linked image"),
    )
    assert generate(upstream).images == [b"linked image", PNG]
    download = upstream.requests[1]
    assert (download.method, str(download.url)) == ("GET", "https://cdn.example.com/a.png")
    assert "Authorization" not in download.headers  # the key goes to the provider only
    assert "X-OpenRouter-Title" not in download.headers


# --- api_type "chat" ---


def test_the_chat_request():
    upstream = Upstream(chat_answer())
    generate(upstream, model="nano-banana")
    request = upstream.requests[0]
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert upstream.body == {
        "model": "google/gemini-2.5-flash-image",
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": "a red fox"}],
    }


def test_chat_options_go_in_image_config_and_the_seed_at_the_top():
    body = chat_request(MODELS["nano-banana"], "a red fox", {"aspect_ratio": "16:9", "resolution": "2K", "seed": 42}, [])
    assert body["image_config"] == {"aspect_ratio": "16:9", "image_size": "2K"}  # resolution is called image_size there
    assert body["seed"] == 42
    assert "image_config" not in chat_request(MODELS["nano-banana"], "a red fox", {"seed": 7}, [])
    assert "seed" not in chat_request(MODELS["nano-banana"], "a red fox", {"aspect_ratio": "1:1"}, [])


def test_chat_reference_images_go_in_the_message():
    body = chat_request(MODELS["nano-banana"], "the knight, from the back", {}, [DATA_URL, REFERENCE])
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "the knight, from the back"},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
                {"type": "image_url", "image_url": {"url": REFERENCE}},
            ],
        }
    ]


def test_chat_modalities_come_from_the_model():
    config = config_with(**{"image-only": {"provider": "openrouter", "model": "x/y", "api_type": "chat", "modalities": ["image"]}})
    assert chat_request(parse_config(config, env={}).models["image-only"], "x", {}, [])["modalities"] == ["image"]


def test_the_chat_answer():
    message = {
        "role": "assistant",
        "content": "Here is your fox.",
        "images": [
            {"type": "image_url", "image_url": {"url": DATA_URL}},
            {"type": "image_url", "image_url": {"url": "https://cdn.example.com/b.webp"}},
        ],
    }
    upstream = Upstream(chat_answer(message, id="gen-42", usage={"prompt_tokens": 9, "completion_tokens": 1290, "total_tokens": 1299, "cost": 1}),
                        httpx2.Response(200, content=b"webp bytes"))  # fmt: skip
    generation = generate(upstream, model="nano-banana")
    assert generation == Generation(
        images=[PNG, b"webp bytes"], usage=Usage(9, 1290, 1299, 1.0), text="Here is your fox.", upstream_id="gen-42"
    )
    assert "Authorization" not in upstream.requests[1].headers


@pytest.mark.parametrize(
    ("content", "text"),
    [
        ("  Here it is.  ", "Here it is."),
        ([{"type": "text", "text": "Part one."}, {"type": "image_url"}, {"type": "text", "text": "Part two."}], "Part one.\nPart two."),
        ("   ", None),
        (None, None),
        ([], None),
    ],
)
def test_the_text_of_a_chat_answer(content, text):
    message = {"role": "assistant", "content": content, "images": [{"type": "image_url", "image_url": {"url": DATA_URL}}]}
    assert generate(Upstream(chat_answer(message)), model="nano-banana").text == text


# --- the extra request fields of a model ---


def test_request_is_merged_into_every_body():
    extra = {"provider": {"order": ["openai"], "allow_fallbacks": False}, "image_config": {"thinking": "low"}, "quality": "low"}
    config = config_with(
        merged={"provider": "openrouter", "model": "x/y", "api_type": "images", "options": ["quality"], "request": extra},
        merged_chat={"provider": "openrouter", "model": "x/z", "api_type": "chat", "options": ["aspect_ratio"], "request": extra},
    )
    models = parse_config(config, env={}).models
    assert images_request(models["merged"], "x", {"quality": "high"}, []) == {
        "model": "x/y", "prompt": "x", "quality": "low",  # the request's values win
        "provider": {"order": ["openai"], "allow_fallbacks": False}, "image_config": {"thinking": "low"},
    }  # fmt: skip
    chat = chat_request(models["merged_chat"], "x", {"aspect_ratio": "1:1"}, [])
    assert chat["image_config"] == {"aspect_ratio": "1:1", "thinking": "low"}  # merged, not replaced
    assert chat["provider"] == {"order": ["openai"], "allow_fallbacks": False}


# --- usage ---


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cost": 0.5}, Usage(1, 2, 3, 0.5)),
        ({"cost": 0}, Usage(None, None, None, 0.0)),
        ({"prompt_tokens": "1", "completion_tokens": True, "total_tokens": 2.5, "cost": "0.04"}, Usage(None, None, None, None)),
        ({"cost": False}, Usage()),
        ({}, Usage()),
        ([1, 2], None),
        (None, None),
    ],
)
def test_usage(usage, expected):
    assert generate(Upstream(images_answer(PNG, usage=usage))).usage == expected


def test_an_upstream_id_that_is_not_a_string_is_ignored():
    assert generate(Upstream(images_answer(PNG, id=12345))).upstream_id is None


# --- errors ---


def test_an_error_status_with_openrouters_error_document():
    upstream = Upstream(httpx2.Response(402, json={"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"}}))
    fails(upstream, "openrouter refused the request (HTTP 402): Insufficient credits. Add more using https://openrouter.ai/credits")


@pytest.mark.parametrize(
    ("status", "error", "detail"),
    [
        (400, {"code": "invalid_prompt", "message": "Bad prompt"}, "Bad prompt (code invalid_prompt)"),
        (400, {"code": 400, "message": "Bad prompt"}, "Bad prompt"),  # the code repeats the status
        (429, {"message": "Rate limited", "metadata": {"provider_name": "Google"}}, "Rate limited [Google]"),
        (502, {"code": 502, "message": "Provider returned error", "metadata": {"provider_name": "OpenAI", "raw": {"reason": "overloaded"}}},
         'Provider returned error [OpenAI]: {"reason": "overloaded"}'),
        (500, {}, "no message"),
        (500, "a string", "no details given"),
    ],
)  # fmt: skip
def test_the_details_of_an_error(status, error, detail):
    fails(Upstream(httpx2.Response(status, json={"error": error})), f"openrouter refused the request (HTTP {status}): {detail}")


def test_an_error_status_without_json():
    fails(Upstream(httpx2.Response(502, text="<html>Bad gateway</html>")), "openrouter refused the request (HTTP 502): <html>Bad gateway</html>")
    fails(Upstream(httpx2.Response(503, json=["unavailable"])), 'openrouter refused the request (HTTP 503): ["unavailable"]')


def test_long_error_texts_are_shortened():
    error = fails(Upstream(httpx2.Response(500, text="x" * 5000)), "openrouter refused the request (HTTP 500): " + "x" * 999 + "…")
    assert len(str(error)) < 1100


def test_an_error_inside_an_answer_with_status_200():
    error = {"code": 502, "message": "Provider returned error", "metadata": {"provider_name": "Fake", "raw": "upstream exploded"}}
    fails(Upstream(httpx2.Response(200, json={"error": error})), "openrouter reported an error: Provider returned error (code 502) [Fake]: upstream exploded")


@pytest.mark.parametrize(
    ("choice", "detail"),
    [
        ({"error": {"code": 500, "message": "Image generation failed"}, "finish_reason": "error"}, "Image generation failed (code 500)"),
        ({"error": {"message": "Blocked"}, "message": {"images": [{"image_url": {"url": DATA_URL}}]}}, "Blocked"),
        ({"finish_reason": "error", "message": {"content": ""}}, "no details given"),
    ],
)
def test_an_error_during_a_chat_generation(choice, detail):
    upstream = Upstream(httpx2.Response(200, json={"choices": [choice]}))
    fails(upstream, f"openrouter reported an error during the generation: {detail}", model="nano-banana")


@pytest.mark.parametrize("answer", [httpx2.Response(200, text="not json"), httpx2.Response(200, json=[1, 2]), httpx2.Response(200, content=b"\xff\xfe")])
def test_an_unexpected_answer(answer):
    with pytest.raises(GenerationError, match=r"^openrouter sent an unexpected answer \(HTTP 200\)"):
        generate(Upstream(answer))


def test_no_image():
    fails(Upstream(images_answer()), "the model returned no image")
    fails(Upstream(httpx2.Response(200, json={"created": 1})), "the model returned no image")
    fails(Upstream(chat_answer({"role": "assistant", "content": "I cannot draw that."})), "the model returned no image; it answered: I cannot draw that.", model="nano-banana")
    fails(Upstream(httpx2.Response(200, json={"choices": []})), "the model returned no image", model="nano-banana")


def test_images_in_forms_that_cannot_be_read():
    fails(Upstream(httpx2.Response(200, json={"data": [{"b64_json": "abcde"}]})), "the provider returned an image that is not valid base64")
    for url in ("data:image/png,rawdata", "ftp://cdn.example.com/a.png", "file:///etc/passwd", "/relative.png"):
        message = {"images": [{"type": "image_url", "image_url": {"url": url}}]}
        fails(Upstream(chat_answer(message)), "openrouter returned an image in an unsupported form", model="nano-banana")


def test_a_link_that_cannot_be_downloaded():
    upstream = Upstream(httpx2.Response(200, json={"data": [{"url": "https://cdn.example.com/gone.png"}]}), httpx2.Response(404))
    with pytest.raises(GenerationError, match="^cannot download the image openrouter linked to: .*404 Not Found"):
        generate(upstream)
    upstream = Upstream(httpx2.Response(200, json={"data": [{"url": "https://cdn.example.com/a.png"}]}), httpx2.ConnectError("refused"))
    fails(upstream, "cannot download the image openrouter linked to: refused")


def test_a_linked_image_too_large_is_not_downloaded(monkeypatch):
    monkeypatch.setattr(providers, "MAX_IMAGE_DOWNLOAD_BYTES", 1000)
    upstream = Upstream(httpx2.Response(200, json={"data": [{"url": "https://cdn.example.com/huge.png"}]}), httpx2.Response(200, content=b"x" * 1001))
    fails(upstream, "the image openrouter linked to is larger than 64 MB")


def test_the_provider_does_not_answer_in_time():
    fails(Upstream(httpx2.ReadTimeout("timed out")), "openrouter did not answer in time")
    fails(Upstream(httpx2.ConnectTimeout("timed out")), "openrouter did not answer in time")


def test_the_provider_cannot_be_reached():
    fails(Upstream(httpx2.ConnectError("[Errno 111] Connection refused")), "cannot reach openrouter: [Errno 111] Connection refused")
    fails(Upstream(httpx2.RemoteProtocolError("Server disconnected without sending a response.")),
          "cannot reach openrouter: Server disconnected without sending a response.")  # fmt: skip


def test_an_error_without_a_message_is_named_by_its_type():
    with pytest.raises(GenerationError, match="^cannot reach openrouter: ReadError$"):
        generate(Upstream(httpx2.ReadError("")))
