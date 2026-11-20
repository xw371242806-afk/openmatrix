"""Task-type LLM routing (optimization P0-A).

Task-based multi-model orchestration: route each analysis request to the
model best suited to its task type (fast/cheap models for extraction, stronger
models for chained reasoning) instead of a single model for everything.

Design constraints:

- Disabled by default: unless ``OPENMATRIX_ROUTER_ENABLED=true``, ``resolve``
  always returns the caller-supplied default model, so behavior is byte-for-byte
  unchanged for existing users.
- Per-task env overrides: ``OPENMATRIX_MODEL_FAST`` / ``OPENMATRIX_MODEL_CHAIN``
  / ``OPENMATRIX_MODEL_BATCH``. An unset or empty override falls back to the
  caller-supplied default.
- Pure stdlib, no I/O: trivially unit-testable and safe to construct per
  request.

This composes with (but is distinct from) the Map-Reduce dispatcher's internal
cheap/smart model split: the router picks the models per request; the
dispatcher splits one long document across two of them.
"""

from __future__ import annotations

import os

TASK_FAST = "fast"
TASK_CHAIN = "chain"
TASK_BATCH = "batch"

KNOWN_TASKS = (TASK_FAST, TASK_CHAIN, TASK_BATCH)

_TASK_ENV = {
    TASK_FAST: "OPENMATRIX_MODEL_FAST",
    TASK_CHAIN: "OPENMATRIX_MODEL_CHAIN",
    TASK_BATCH: "OPENMATRIX_MODEL_BATCH",
}

_ROUTER_ENABLED_ENV = "OPENMATRIX_ROUTER_ENABLED"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class ModelRouter:
    """Resolve which LLM model should serve a given task type."""

    def __init__(self, default_model: str, enabled: bool | None = None):
        if not isinstance(default_model, str) or not default_model.strip():
            raise ValueError("default_model must be a non-empty string")
        self.default_model = default_model
        self.enabled = _env_flag(_ROUTER_ENABLED_ENV) if enabled is None else bool(enabled)

    def resolve(self, task: str) -> str:
        if not self.enabled:
            return self.default_model
        env_name = _TASK_ENV.get(task)
        if env_name is None:
            return self.default_model
        override = os.environ.get(env_name, "").strip()
        return override or self.default_model

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "default_model": self.default_model,
            "tasks": {
                task: {"env": env_name, "model": self.resolve(task)}
                for task, env_name in _TASK_ENV.items()
            },
        }
