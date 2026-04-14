"""Phase executor for parallel debater turns."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .pane import AgentPane
from .turn import run_turn


@dataclass
class Task:
    agent: AgentPane
    prompt: str


@dataclass
class TaskResult:
    role: str
    answer: str | None
    error: Exception | None


def run_parallel(
    tasks: list[Task],
    turn_dir: Path,
    timeout: float = 900.0,
) -> dict[str, TaskResult]:
    """Kick off all tasks concurrently; wait for every agent to produce .done."""
    results: dict[str, TaskResult] = {}
    threads: list[threading.Thread] = []

    def _worker(task: Task) -> None:
        try:
            ans = run_turn(task.agent, turn_dir, task.prompt, timeout=timeout)
            results[task.agent.role] = TaskResult(task.agent.role, ans, None)
        except Exception as e:
            results[task.agent.role] = TaskResult(task.agent.role, None, e)

    for t in tasks:
        th = threading.Thread(target=_worker, args=(t,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    return results
