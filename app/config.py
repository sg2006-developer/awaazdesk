import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
# Reuse ONLY the existing Gnani key during development in the original workspace.
if not os.getenv("GNANI_API_KEY"):
    from dotenv import dotenv_values
    shared = dotenv_values(ROOT.parent / ".env")
    if shared.get("GNANI_API_KEY"):
        os.environ["GNANI_API_KEY"] = shared["GNANI_API_KEY"]

def llm_settings():
    """Keep each provider's credential paired with its endpoint, never an old key."""
    router_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER")
    provider = os.getenv("LLM_PROVIDER", "openrouter" if router_key else "compatible")
    if provider == "openrouter":
        return {
            "provider": provider,
            "base_url": "https://openrouter.ai/api/v1",
            "key": router_key,
            "model": os.getenv("OPENROUTER_MODEL") or "google/gemini-2.5-flash",
        }
    return {"provider": provider, "base_url": os.getenv("LLM_BASE_URL"),
            "key": os.getenv("LLM_API_KEY"), "model": os.getenv("LLM_MODEL")}

def llm_ready():
    settings = llm_settings()
    return all(settings[k] for k in ("base_url", "key", "model"))
