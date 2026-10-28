"""kiro_adapters — drive Kiro Crew (kirocrew) and Kiro Code (kirocode) from jcode.

Two adapters, one contract:

* :class:`kiro_adapters.kirocrew.KiroCrewAdapter` — Kiro Crew personal agent,
  reached either through its CLI (``kirocrew``) or its already-running gateway.
* :class:`kiro_adapters.kirocode.KiroCodeAdapter` — the Kiro coding CLI
  (``kirocode`` / ``kiro-cli``), resolved dynamically with capability probing.

Both return :class:`kiro_adapters.result.AdapterResult`, a single JSON-safe
shape, so jcode (or codex, forge, kirocrew itself) can consume either one
without harness-specific parsing.

The MCP server in :mod:`kiro_adapters.mcp_server` exposes both adapters as
tools and uses only the standard library — no dependency install required.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
