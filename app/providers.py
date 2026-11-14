"""External HTTP services. Secrets and provider credentials never reach the browser."""
import json

import httpx
from fastapi import HTTPException

from .config import llm_settings

_client: httpx.AsyncClient | None = None


def client():
    """One pooled client: reusing TLS connections saves a handshake on every turn."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10),
                                    limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120))
    return _client


async def close_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def request(url, service="The AI model provider", **kwargs):
    try:
        response = await client().post(url, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, f"{service} timed out. Your saved work is safe; retry the request.") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Could not reach {service}. Check the server's network connection.") from exc
    if not response.is_success:
        # Do not return provider debug dumps or configuration to the browser.
        reasons = {401: "rejected the API key", 402: "reports that credits are exhausted",
                   403: "says the API key cannot use this service", 429: "is busy or rate limited; retry shortly"}
        reason = reasons.get(response.status_code, "rejected the request")
        raise HTTPException(502, f"{service} {reason} (upstream {response.status_code}).")
    return response


async def reason(system, payload):
    settings = llm_settings()
    if not all(settings[k] for k in ("base_url", "key", "model")):
        raise HTTPException(503, "Reasoning provider is not configured.")
    url = settings["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": settings["model"], "temperature": 0, "max_tokens": 4096,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": "Bearer " + settings["key"]}
    if settings["provider"] == "openrouter":
        headers["X-Title"] = "AwaazDesk"
        body["provider"] = {"require_parameters": True, "data_collection": "deny"}
        if settings["model"] == "google/gemini-2.5-flash":
            body["reasoning"] = {"enabled": False}
    response = await request(url, service="The AI model provider", headers=headers, json=body)
    try:
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("Incomplete model response")
        result = json.loads(choice["message"]["content"])
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Reasoning provider returned invalid JSON. No changes were saved.") from exc
