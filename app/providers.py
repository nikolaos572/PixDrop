"""Requests to the providers: OpenRouter, or any API with the same two image endpoints.

A model's api_type picks the endpoint and the shape of the request:

- "images": POST {base_url}/images with the prompt, the options under their own names and the
  reference images in input_references; the images come back base64-encoded in data[].
- "chat": POST {base_url}/chat/completions with modalities ["image", "text"], the prompt and
  the reference images in the user's message, and the options in image_config; the images
  come back in choices[0].message.images[], as data: URLs (or links, which are fetched).

Either way the answer's usage gives the tokens and the cost in USD (usage.cost). Errors
come as an HTTP error status, or inside an answer with status 200 (OpenRouter reports
errors that happen during the generation that way): both become a GenerationError, whose
message is kept as the job's error. Reference images are passed in as data: URLs.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2

from .config import Model
from .store import Usage

MAX_IMAGE_DOWNLOAD_BYTES = 64 * 1024**2  # an image a chat model returns as a link
_ERROR_DETAIL_CHARS = 1000
# The options' names in the image_config of chat completions, where they differ.
_CHAT_OPTION_NAMES = {"resolution": "image_size"}


class GenerationError(Exception):
    """The provider did not produce an image. The message says why, for the job's error;
    `usage` is what the provider reported when it answered all the same (and may charge)."""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


@dataclass(frozen=True)
class Generation:
    images: list[bytes]
    usage: Usage | None
    text: str | None  # text the model returned along with the images
    upstream_id: str | None


class ProviderClient:
    """Generates images through the providers, sharing one asynchronous HTTP client: a slow
    generation does not hold up any other request."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    async def generate(
        self, model: Model, prompt: str, options: Mapping[str, str | int], references: Sequence[str]
    ) -> Generation:
        """Generate with `model`; `references` are data: URLs. Raises GenerationError."""
        provider = model.provider
        if model.api_type == "images":
            url, body = f"{provider.base_url}/images", images_request(model, prompt, options, references)
        else:
            url, body = f"{provider.base_url}/chat/completions", chat_request(model, prompt, options, references)
        headers = {**provider.headers, "Authorization": f"Bearer {provider.api_key}"}
        try:
            response = await self._http.post(url, json=body, headers=headers)
        except httpx2.TimeoutException:
            raise GenerationError(f"{provider.name} did not answer in time") from None
        except httpx2.HTTPError as exc:
            raise GenerationError(f"cannot reach {provider.name}: {str(exc) or type(exc).__name__}") from None
        answer = _json(response, provider.name)
        usage = _usage(answer.get("usage"))
        try:
            if model.api_type == "images":
                images, text = await self._images_answer(answer, provider.name)
            else:
                images, text = await self._chat_answer(answer, provider.name)
            if not images:
                said = f"; it answered: {_shorten(text)}" if text else ""
                raise GenerationError(f"the model returned no image{said}")
        except GenerationError as exc:
            exc.usage = usage  # the provider answered: its usage (and cost) still counts
            raise
        upstream_id = answer.get("id")
        return Generation(
            images=images,
            usage=usage,
            text=text,
            upstream_id=upstream_id if isinstance(upstream_id, str) else None,
        )

    async def _images_answer(self, answer: dict[str, Any], provider: str) -> tuple[list[bytes], str | None]:
        images = []
        for item in _list(answer.get("data")):
            if isinstance(item, dict) and isinstance(item.get("b64_json"), str):
                images.append(_base64(item["b64_json"]))
            elif isinstance(item, dict) and isinstance(item.get("url"), str):
                images.append(await self._fetch(item["url"], provider))
        return images, None

    async def _chat_answer(self, answer: dict[str, Any], provider: str) -> tuple[list[bytes], str | None]:
        choices = _list(answer.get("choices"))
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        if isinstance(choice.get("error"), dict) or choice.get("finish_reason") == "error":
            raise GenerationError(f"{provider} reported an error during the generation: {_error_text(choice)}")
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        images = []
        for item in _list(message.get("images")):
            image_url = item.get("image_url") if isinstance(item, dict) else None
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str):
                images.append(await self._fetch(url, provider))
        return images, _text(message.get("content"))

    async def _fetch(self, url: str, provider: str) -> bytes:
        """An image given as a data: URL, or as a link (fetched without the provider's key)."""
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            if not header.endswith(";base64"):
                raise GenerationError(f"{provider} returned an image in an unsupported form")
            return _base64(payload)
        if not url.startswith(("https://", "http://")):
            raise GenerationError(f"{provider} returned an image in an unsupported form")
        try:
            async with self._http.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content += chunk
                    if len(content) > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise GenerationError(f"the image {provider} linked to is larger than 64 MB")
                return bytes(content)
        except httpx2.HTTPError as exc:
            raise GenerationError(f"cannot download the image {provider} linked to: {str(exc) or type(exc).__name__}") from None


def images_request(model: Model, prompt: str, options: Mapping[str, str | int], references: Sequence[str]) -> dict[str, Any]:
    """The body of POST /images. The options have the endpoint's own names."""
    body: dict[str, Any] = {"model": model.upstream, "prompt": prompt, **options}
    if references:
        body["input_references"] = [_image_part(url) for url in references]
    return _merged(body, model.request)


def chat_request(model: Model, prompt: str, options: Mapping[str, str | int], references: Sequence[str]) -> dict[str, Any]:
    """The body of POST /chat/completions asking for an image."""
    content: str | list[dict[str, Any]] = prompt
    if references:
        content = [{"type": "text", "text": prompt}, *(_image_part(url) for url in references)]
    body: dict[str, Any] = {
        "model": model.upstream,
        "modalities": list(model.modalities),
        "messages": [{"role": "user", "content": content}],
    }
    image_config = {_CHAT_OPTION_NAMES.get(name, name): value for name, value in options.items() if name != "seed"}
    if image_config:
        body["image_config"] = image_config
    if "seed" in options:
        body["seed"] = options["seed"]
    return _merged(body, model.request)


def _image_part(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


def _merged(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """`base` with `extra` merged in, recursively; the values of `extra` win."""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _merged(merged[key], value)
        else:
            merged[key] = value
    return merged


def _json(response: httpx2.Response, provider: str) -> dict[str, Any]:
    """The answer's JSON document, or GenerationError for an error status or an error body."""
    try:
        answer = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        answer = None
    if response.status_code >= 400:
        detail = _error_text(answer, response.status_code) if isinstance(answer, dict) else _shorten(response.text)
        raise GenerationError(f"{provider} refused the request (HTTP {response.status_code}): {detail}")
    if not isinstance(answer, dict):
        raise GenerationError(f"{provider} sent an unexpected answer (HTTP {response.status_code}): {_shorten(response.text)}")
    if isinstance(answer.get("error"), dict):  # an error reported with status 200
        raise GenerationError(f"{provider} reported an error: {_error_text(answer)}")
    return answer


def _error_text(container: Mapping[str, Any], status: int | None = None) -> str:
    """The message of {"error": {"code", "message", "metadata": {"provider_name", "raw"}}}; the
    code is left out when it repeats the HTTP status already reported."""
    error = container.get("error")
    if not isinstance(error, dict):
        return "no details given"
    text = str(error.get("message") or "no message")
    if isinstance(code := error.get("code"), int | str) and str(code) not in (text, str(status)):
        text = f"{text} (code {code})"
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    if isinstance(name := metadata.get("provider_name"), str):
        text += f" [{name}]"
    if raw := metadata.get("raw"):
        text += f": {raw if isinstance(raw, str) else json.dumps(raw)}"
    return _shorten(text)


def _usage(raw: object) -> Usage | None:
    if not isinstance(raw, dict):
        return None

    def integer(name: str) -> int | None:
        value = raw.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    cost = raw.get("cost")
    return Usage(
        prompt_tokens=integer("prompt_tokens"),
        completion_tokens=integer("completion_tokens"),
        total_tokens=integer("total_tokens"),
        cost=float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None,
    )


def _text(content: object) -> str | None:
    """A message's text: a string, or the text parts of a list of parts."""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return "\n".join(part for part in parts if isinstance(part, str)).strip() or None
    return None


def _base64(payload: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        raise GenerationError("the provider returned an image that is not valid base64") from None


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _shorten(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _ERROR_DETAIL_CHARS else text[: _ERROR_DETAIL_CHARS - 1] + "…"
