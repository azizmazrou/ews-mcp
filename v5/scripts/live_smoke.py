#!/usr/bin/env python3
"""Read-only live smoke against a RUNNING v5 server.

Exercises the north-star read path against real Exchange data and prints
per-step latency + response size. READ-ONLY by default; the draft step
(scenario A call 2) runs only behind an explicit --write flag and only
creates a draft (never sends). Reads its target from env at runtime and
writes nothing to the repo.

Env:
    SMOKE_BASE_URL   e.g. http://192.0.2.10:8010   (required)
    SMOKE_API_KEY    bearer token                   (required)
    SMOKE_QUERY      a keyword expected to match    (default: "budget")
    SMOKE_AR_QUERY   an Arabic keyword to search    (optional)

Usage:
    python scripts/live_smoke.py [--write]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


def _req(base, key, method, path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    if data:
        req.add_header("Content-Type", "application/json")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, json.loads(body), (time.perf_counter() - start), len(body)
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, json.loads(body or b"{}"), (time.perf_counter() - start), len(body)


def main() -> int:
    base = os.environ.get("SMOKE_BASE_URL", "").rstrip("/")
    key = os.environ.get("SMOKE_API_KEY", "")
    if not base or not key:
        print("set SMOKE_BASE_URL and SMOKE_API_KEY")
        return 2
    write = "--write" in sys.argv
    query = os.environ.get("SMOKE_QUERY", "budget")
    ar_query = os.environ.get("SMOKE_AR_QUERY", "")
    failures = []
    steps = []

    def step(name, status, data, secs, size, ok):
        steps.append((name, status, round(secs * 1000), size,
                      data.get("source", "-"), "OK" if ok else "FAIL"))
        if not ok:
            failures.append(f"{name}: {status} {json.dumps(data)[:300]}")
        return data

    # 1. health surfaces
    s, d, t, n = _req(base, key, "GET", "/livez", timeout=5)
    step("livez", s, d, t, n, s == 200)
    s, d, t, n = _req(base, key, "GET", "/readyz", timeout=5)
    step("readyz", s, d, t, n, s in (200, 503))
    warm = s == 200
    s, d, t, n = _req(base, key, "GET", "/health", timeout=5)
    tools = d.get("tools")
    step("health", s, d, t, n, s == 200)
    if not warm:
        print("connection not warm yet — live journey skipped")
        _print(steps, tools)
        return 1 if failures else 0

    # 2. the read journey (north-star call 1 + neighbors)
    s, d, t, n = _req(base, key, "POST", "/api/tools/get_mailbox_overview", {})
    step("overview", s, d, t, n, s == 200 and d.get("ok") is True)
    s, d, t, n = _req(base, key, "POST", "/api/tools/search_messages",
                      {"query": query, "limit": 3})
    d = step(f"search '{query}'", s, d, t, n, s == 200 and d.get("ok") is True)
    first_id = (d.get("items") or [{}])[0].get("id")
    if first_id:
        s, d, t, n = _req(base, key, "POST", "/api/tools/get_message",
                          {"id": first_id, "format": "full"})
        step("get_message", s, d, t, n, s == 200 and d.get("ok") is True)
        s, d, t, n = _req(base, key, "POST", "/api/tools/get_thread",
                          {"id": first_id})
        step("get_thread", s, d, t, n, s == 200 and d.get("ok") is True)
    else:
        failures.append(f"search '{query}' returned no items — pick another "
                        "SMOKE_QUERY")
    s, d, t, n = _req(base, key, "POST", "/api/tools/list_events",
                      {"start": "today", "end": "+7d"})
    step("list_events", s, d, t, n, s == 200 and d.get("ok") is True)
    if ar_query:
        s, d, t, n = _req(base, key, "POST", "/api/tools/search_messages",
                          {"query": ar_query, "limit": 3})
        step("search AR", s, d, t, n, s == 200 and d.get("ok") is True)
    s, d, t, n = _req(base, key, "POST", "/api/tools/check_availability", {
        "attendees": [os.environ.get("SMOKE_SELF", "me@example.invalid")],
        "start": "today", "end": "+2d"})
    step("availability", s, d, t, n, s in (200, 502))

    # 3. optional draft step (scenario A call 2) — draft only, never sends
    if write and first_id:
        s, d, t, n = _req(base, key, "POST", "/api/tools/create_draft", {
            "mode": "reply", "reply_to": first_id,
            "body": "smoke-test draft — safe to delete"})
        d = step("create_draft", s, d, t, n, s == 200 and d.get("ok") is True)
        draft_id = d.get("draft_id")
        if draft_id:
            s, d, t, n = _req(base, key, "POST", "/api/tools/delete_draft",
                              {"draft_id": draft_id})
            step("delete_draft", s, d, t, n, s == 200)

    _print(steps, tools)
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    return 0


def _print(steps, tools):
    print(f"\ntools registered: {tools}")
    print(f"{'step':<18} {'status':<7} {'ms':<7} {'bytes':<8} {'source':<7} ok")
    for name, status, ms, size, source, ok in steps:
        print(f"{name:<18} {status:<7} {ms:<7} {size:<8} {source:<7} {ok}")


if __name__ == "__main__":
    sys.exit(main())
