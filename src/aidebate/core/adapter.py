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
    delay: float   # seconds after launch
    key: str       # tmux key name ("Enter", "Space", ...)


@dataclass
class Adapter:
    name: str
    cmd: str
    submit_key: str
    submit_delay: float
    permission_prompts: list[PermissionPattern]
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
