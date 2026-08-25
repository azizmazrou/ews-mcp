#!/usr/bin/env python3
"""v5 boot smoke: never-exit contract end-to-end, no Exchange needed.

Boots the real HTTP server against an unreachable Exchange and asserts:
process survives; /livez + /health 200; /readyz 503 state=connecting;
get_server_status works cold via REST; an EWS tool fails fast with
upstream_unavailable; send-class refusal carries the kill_switch code
(policy beats connectivity); /openapi.json publishes confirm_token for
send_draft; tier filtering shrinks the registry.

Run from v5/:  python scripts/boot_smoke.py [draft|full]
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"
KEY = "v5-smoke-key"


def _req(method, path, payload=None, timeout=8):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {KEY}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> int:
    tier = sys.argv[1] if len(sys.argv) > 1 else "full"
    env = dict(os.environ)
    env.update({
        "EWS_SERVER_URL": "https://192.0.2.1/EWS/Exchange.asmx",  # TEST-NET-1
        "EWS_EMAIL": "smoke@example.invalid",
        "EWS_USERNAME": "smoke", "EWS_PASSWORD": "smoke",
        "MCP_TRANSPORT": "http", "MCP_HOST": "127.0.0.1", "MCP_PORT": str(PORT),
        "MCP_API_KEY": KEY,
        "SEND_ENABLED": "false",
        "EWS_CAPABILITY_TIER": tier,
        "REQUEST_TIMEOUT": "3", "LOG_LEVEL": "WARNING",
        "DATA_DIR": os.path.join(os.environ.get("TEMP", "/tmp"), "v5-smoke-data"),
        "PYTHONPATH": os.getcwd(),
    })
    proc = subprocess.Popen([sys.executable, "-m", "ewsmcp.main"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []
    try:
        deadline = time.time() + 30
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"FAIL: process exited early ({proc.returncode})")
                return 1
            try:
                up = _req("GET", "/livez", timeout=2)[0] == 200
                if up:
                    break
            except Exception:
                time.sleep(0.5)
        if not up:
            print("FAIL: /livez never came up")
            return 1

        status, health = _req("GET", "/health")
        tools = health.get("tools", 0)
        if status != 200:
            failures.append(f"/health {status}")
        # 32 = 6 mail-read + 7 calendar/people + 3 tasks + 12 writes + 4 rules
        # with the default EWS_SEMANTIC_INDEX=none (find_similar
        # unregistered). Update DELIBERATELY together with the generated docs.
        if tier == "full" and tools != 32:
            failures.append(f"expected 32 tools in full tier, got {tools}")
        if tier == "draft" and tools >= 32:
            failures.append(f"tier=draft should shrink registry, got {tools}")

        status, ready = _req("GET", "/readyz")
        if status != 503 or ready.get("connection", {}).get("state") != "connecting":
            failures.append(f"/readyz {status} {ready}")

        status, st = _req("POST", "/api/tools/get_server_status", {})
        if status != 200 or st.get("connection", {}).get("state") != "connecting":
            failures.append(f"get_server_status cold {status} {st}")

        status, msg = _req("POST", "/api/tools/search_messages", {"limit": 1})
        if status != 503 or msg.get("error", {}).get("code") != "upstream_unavailable":
            failures.append(f"search cold {status} {msg}")

        if tier == "full":
            status, send = _req("POST", "/api/tools/send_draft", {"draft_id": "d1"})
            if send.get("error", {}).get("code") != "kill_switch":
                failures.append(f"send_draft expected kill_switch, got {send}")
            _, openapi = _req("GET", "/openapi.json")
            schema = json.dumps(openapi.get("paths", {}).get("/api/tools/send_draft", {}))
            if "confirm_token" not in schema:
                failures.append("confirm_token absent from send_draft OpenAPI")
        else:
            status, _ = _req("POST", "/api/tools/send_draft", {"draft_id": "d1"})
            if status != 404:
                failures.append(f"tier=draft: send_draft should be unregistered, got {status}")

        if failures:
            print("FAILURES:")
            for f in failures:
                print(" -", f)
            return 1
        print(f"OK: v5 boot smoke passed (tier={tier}, tools={tools})")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
