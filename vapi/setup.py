"""Create or update the Vapi MCP tool and assistant.  ./run.sh vapi

Needs VAPI_API_KEY (private key) in .env. The public URL comes from PUBLIC_URL in
.env, or from the trycloudflare URL in tunnel.log (written by ./run.sh tunnel).
Ids are kept in .vapi.json so a rerun after a tunnel restart only updates the URL.
"""
import json
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
IDS = ROOT / ".vapi.json"


def public_url() -> str:
    url = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
    if not url and (ROOT / "tunnel.log").is_file():
        found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", (ROOT / "tunnel.log").read_text())
        url = found[-1] if found else ""
    if not url:
        sys.exit("No public URL: run ./run.sh tunnel first, or set PUBLIC_URL in .env")
    return url


def main():
    key = (os.getenv("VAPI_API_KEY") or "").strip()
    if not key:
        sys.exit("Set VAPI_API_KEY (private key from dashboard.vapi.ai > API Keys) in .env")
    url = public_url() + "/mcp"
    ids = json.loads(IDS.read_text()) if IDS.is_file() else {}
    api = httpx.Client(base_url="https://api.vapi.ai", headers={"Authorization": f"Bearer {key}"}, timeout=30)

    def call(method, path, body):
        r = api.request(method, path, json=body)
        if r.status_code >= 400:
            sys.exit(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json()

    server = {"url": url, "timeoutSeconds": 20}
    if ids.get("tool"):
        call("PATCH", f"/tool/{ids['tool']}", {"server": server})
    else:
        # Do not add a request-start message with empty content: calls then fail with an OpenAI 400.
        ids["tool"] = call("POST", "/tool", {"type": "mcp", "server": server, "messages": [],
                                             "metadata": {"protocol": "shttp"}})["id"]
    IDS.write_text(json.dumps(ids))

    assistant = {
        "name": "Can You Repeat That",
        "firstMessage": "Arm is ready.",
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"},
        # Cartesia "Matt - Goofy Friend": high pitched, silly male. Deepgram alternative: deepgram / angus.
        "voice": {"provider": os.getenv("VAPI_VOICE_PROVIDER", "cartesia"),
                  "voiceId": os.getenv("VAPI_VOICE", "bfd3644b-d561-4b1c-a01f-d9af98cb67c0")},
        "model": {
            "provider": "openai", "model": "gpt-4.1", "temperature": 0.2,
            "messages": [{"role": "system", "content": (ROOT / "vapi/assistant_prompt.md").read_text()}],
            "toolIds": [ids["tool"]],
        },
    }
    if ids.get("assistant"):
        call("PATCH", f"/assistant/{ids['assistant']}", assistant)
    else:
        ids["assistant"] = call("POST", "/assistant", assistant)["id"]
    IDS.write_text(json.dumps(ids))

    print(f"MCP tool   {ids['tool']}  ->  {url}")
    print(f"Assistant  {ids['assistant']}")
    print(f"Talk to it: https://dashboard.vapi.ai/assistants/{ids['assistant']}")


if __name__ == "__main__":
    main()
