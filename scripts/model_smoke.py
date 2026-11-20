#!/usr/bin/env python
"""Bounded real OpenAI-compatible model smoke test.

Sends exactly one chat completion to a real OpenAI-compatible endpoint with a
non-sensitive fixture prompt and bounded timeouts. Provider tokens are read
from the environment only and are never logged, persisted, or written back.

Usage:
    OPENMATRIX_MODEL_URL=<base_url> OPENMATRIX_MODEL_NAME=<model> \
        OPENMATRIX_MODEL_KEY=<api_key> python scripts/model_smoke.py

Optional: OPENMATRIX_MODEL_TIMEOUT (seconds, default 30).

Exits 0 on a successful completion that echoes the fixture marker, 1 on
request failure, 2 on invalid environment, and 3 when the response does not
contain the expected marker (drifted behavior, so reviewers get a concrete
signal instead of a silent pass).
"""
import json
import os
import sys

FIXTURE = "Reply with exactly the single word: OPENMATRIX_SMOKE_OK"


def main() -> int:
    base_url = os.environ.get("OPENMATRIX_MODEL_URL", "").rstrip("/")
    model = os.environ.get("OPENMATRIX_MODEL_NAME", "")
    api_key = os.environ.get("OPENMATRIX_MODEL_KEY", "")
    timeout = float(os.environ.get("OPENMATRIX_MODEL_TIMEOUT", "30"))

    missing = [
        name
        for name, value in (("OPENMATRIX_MODEL_URL", base_url), ("OPENMATRIX_MODEL_NAME", model), ("OPENMATRIX_MODEL_KEY", api_key))
        if not value
    ]
    if missing:
        print(f"model_smoke: missing required env: {', '.join(missing)}")
        return 2
    # OpenAI-compatible model ids may include a vendor prefix ("openai/gpt-oss-120b").
    # Reject anything outside a strict, non-shell-interpretable allowlist so a
    # hostile value can never smuggle extra characters into the request.
    if not all(ch.isalnum() or ch in ".-_/" for ch in (model or "")):
        print("model_smoke: OPENMATRIX_MODEL_NAME contains unexpected characters; refusing")
        return 2

    # Never echo the key. Only report presence.
    print(f"model_smoke: connecting to {base_url} model={model} key=present")
    print(f"model_smoke: fixture prompt (non-sensitive): {FIXTURE!r}")

    try:
        import httpx

        with httpx.Client(trust_env=False, timeout=timeout) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a strict instruction follower."},
                        {"role": "user", "content": FIXTURE},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 128,
                },
            )
        body = response.json()
    except Exception as exc:
        print(f"model_smoke: request failed: {type(exc).__name__}: {exc}")
        return 1

    if response.status_code != 200:
        print(f"model_smoke: non-200 status {response.status_code}; body summary: {str(body)[:200]}")
        return 1

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        print(f"model_smoke: unexpected response shape: {exc}; body summary: {str(body)[:200]}")
        return 1

    marker = "OPENMATRIX_SMOKE_OK"
    if marker in (content or "").upper():
        print(f"model_smoke: PASS (fixture marker present in {model} response)")
        return 0

    print(f"model_smoke: FAIL no fixture marker in response content: {content!r}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())