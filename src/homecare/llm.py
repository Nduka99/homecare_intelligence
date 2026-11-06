"""A small client for llama-server's OpenAI-compatible chat API, standard library only.

Adapted from PetroKnowledge `llm.py` (MIT, same author). The request names no model: the
server answers with the GGUF it has loaded, which `homecare.models.served` checked by SHA-256.
"""

import json
import urllib.error
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 900


class ModelServerError(RuntimeError):
    """The model server could not be reached or returned an unusable reply."""


def chat_payload(
    system: str, user: str, schema: dict[str, Any], temperature: float, seed: int, max_tokens: int
) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "seed": seed,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": schema},
        },
        # Reasoning would be thrown away, so it is switched off at the chat template.
        "chat_template_kwargs": {"enable_thinking": False},
    }


def chat(
    url: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    temperature: float = 0.0,
    seed: int = 7,
    max_tokens: int = 1024,
) -> Any:
    """One chat request whose reply must follow `schema`; the reply's JSON."""
    body = json.dumps(chat_payload(system, user, schema, temperature, seed, max_tokens))
    request = urllib.request.Request(
        url + "/v1/chat/completions",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            reply = json.loads(response.read())
        choice = reply["choices"][0]
        finish = choice["finish_reason"]
        content = choice["message"]["content"]
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise ModelServerError(f"model server at {url} refused the request: {detail}") from error
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as error:
        raise ModelServerError(f"model server at {url} failed: {error}") from error
    # A reply cut off at max_tokens can still be valid JSON, so only a finished reply is used.
    if finish != "stop":
        raise ModelServerError(f"the model's reply did not finish ({finish})")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError) as error:
        raise ModelServerError("the model's reply is not valid JSON") from error
