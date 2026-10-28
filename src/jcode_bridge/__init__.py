"""jcode_bridge: make jcode a backend other agents can dispatch to.

The mirror of ``kiro_adapters``: instead of jcode calling Kiro, this exposes
jcode itself as an MCP tool surface, so a Kiro Crew agent (or any MCP client)
can hand jcode a task and read the result.

Two call shapes:

* synchronous: :meth:`JcodeBackend.run` shells out to ``jcode run --json`` and
  returns the parsed answer.
* asynchronous: :meth:`JcodeBackend.start` spawns a detached task wrapper that
  survives this process, with status/output/cancel against the task directory.

The wrapper exists because a backend caller must be able to start work and read
it later. A bare ``Popen`` cannot report an exit code after its parent is gone;
``python -m jcode_bridge.taskrunner`` can, because it is the parent of the
jcode process and writes ``result.json`` when jcode finishes.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
