"""The recorder's seam to the operating system and to its own control loop.

``ProcessRunner`` is the interface for starting external tools (streamlink,
yt-dlp, ffmpeg). Two adapters satisfy it:

* ``SubprocessRunner`` - production. Commands are argument lists (never a
  shell), every ``run`` needs a timeout, and ``popen`` always starts the child
  in its own session so its process group can be stopped without touching
  the recorder's own group (invariant 2).
* ``FakeProcessRunner`` - tests and dry runs. Records every command and
  answers with scripted results; nothing is executed.

``LiveMonitorPorts`` bundles what the live-only monitor needs from the
outside world (config lookup, presence probe, sleep, shutdown flag), so the
loop can be driven by fakes instead of patching module globals.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class ProcessRunner:
    """Interface. ``resolve`` maps a tool name to an executable path."""

    def resolve(self, tool: str) -> str:
        raise NotImplementedError

    def run(self, cmd: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def popen(self, cmd: list[str], **kwargs: Any) -> subprocess.Popen:
        raise NotImplementedError


class SubprocessRunner(ProcessRunner):
    def __init__(self, resolvers: Optional[dict[str, Callable[[], str]]] = None):
        self._resolvers = dict(resolvers or {})

    def resolve(self, tool: str) -> str:
        resolver = self._resolvers.get(tool)
        if resolver is None:
            raise FileNotFoundError(f"no resolver for {tool}")
        return resolver()

    def run(self, cmd, *, timeout, **kwargs):
        if not isinstance(cmd, (list, tuple)) or kwargs.get("shell"):
            raise ValueError("commands must be argument lists without a shell")
        return subprocess.run(list(cmd), timeout=timeout, **kwargs)

    def popen(self, cmd, **kwargs):
        if not isinstance(cmd, (list, tuple)) or kwargs.get("shell"):
            raise ValueError("commands must be argument lists without a shell")
        kwargs.pop("preexec_fn", None)
        if os.name != "nt":
            kwargs["start_new_session"] = True
        return subprocess.Popen(list(cmd), **kwargs)


@dataclass
class FakeProcessRunner(ProcessRunner):
    """Scripted adapter. ``results`` are returned by ``run`` in order (the last
    one repeats); ``error`` is raised instead when set."""

    results: list = field(default_factory=list)
    error: Optional[BaseException] = None
    popen_factory: Optional[Callable[..., Any]] = None
    calls: list = field(default_factory=list)

    def resolve(self, tool: str) -> str:
        return tool

    def run(self, cmd, *, timeout, **kwargs):
        self.calls.append(("run", list(cmd), {"timeout": timeout, **kwargs}))
        if self.error is not None:
            raise self.error
        if not self.results:
            return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def popen(self, cmd, **kwargs):
        self.calls.append(("popen", list(cmd), kwargs))
        if self.popen_factory is None:
            raise OSError("FakeProcessRunner has no popen_factory")
        return self.popen_factory(list(cmd), **kwargs)


@dataclass
class LiveMonitorPorts:
    get_config: Callable[[str], Optional[dict]]
    live_presence: Callable[[str, dict], dict]
    should_stop: Callable[[], bool]
    sleep: Callable[..., Any]
    interval: Callable[[], float]
