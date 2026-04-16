"""Agent adapter config loader."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class PermissionPattern:
    match: re.Pattern
    respond: str


@dataclass
class StartupKey:
    delay: float  # seconds after launch
    key: str  # tmux key name ("Enter", "Space", ...)


@dataclass
class Adapter:
    name: str
    cmd: str
    submit_key: str
    submit_delay: float
    permission_prompts: list[PermissionPattern]
    ready_patterns: list[re.Pattern]
    answer_instruction: str
    startup_keys: list[StartupKey]

    @classmethod
    def load(cls, path: Path) -> Adapter:
        data = yaml.safe_load(path.read_text())
        return cls(
            name=data["name"],
            cmd=data["cmd"],
            submit_key=data.get("submit_key", "Enter"),
            # Delay between typing the prompt and pressing Enter. Some TUIs
            # (notably codex) need a beat to finish ingesting the paste
            # before a solitary Enter counts as submit.
            submit_delay=float(data.get("submit_delay", 0.6)),
            permission_prompts=[
                PermissionPattern(re.compile(p["match"]), p["respond"])
                for p in data.get("permission_prompts", [])
            ],
            ready_patterns=[re.compile(p) for p in data.get("ready_patterns", [])],
            answer_instruction=data["answer_instruction"],
            # Optional: keys to press automatically after the CLI launches.
            # Useful for dismissing "trust this folder?" style dialogs whose
            # highlighted default we want to accept.
            startup_keys=[
                StartupKey(float(k.get("delay", 2.0)), k.get("key", "Enter"))
                for k in data.get("startup_keys", [])
            ],
        )


ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"


def load_adapter(name: str) -> Adapter:
    return Adapter.load(ADAPTERS_DIR / f"{name}.yaml")


def validate_all_adapters() -> dict[str, Adapter | str]:
    """Load every adapter YAML and return {name: Adapter_or_error_string}.

    Used at startup so bad YAML surfaces before a debate is underway.
    Individual failures don't abort — a broken adapter only matters if
    someone actually tries to use it — but the caller can print warnings
    and decide its own policy.
    """
    results: dict[str, Adapter | str] = {}
    for path in sorted(ADAPTERS_DIR.glob("*.yaml")):
        name = path.stem
        try:
            results[name] = Adapter.load(path)
        except Exception as e:
            results[name] = f"{type(e).__name__}: {e}"
    return results
