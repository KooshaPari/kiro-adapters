"""Construction helpers and the process entry point for the adoption shim.

Kept apart from :mod:`server` so the dispatch module stays small. Nothing here
starts a daemon; the only child process it can create is the ACP front end
named by ``ShimConfig.upstream_argv``.
"""

from __future__ import annotations

from typing import Callable, Optional, TextIO

from .server import AdoptionShim, ShimConfig
from .upstream import InProcessUpstream, StdioUpstream, Upstream


def stdio_upstream_factory(config: ShimConfig) -> Upstream:
    """Default transport: an ACP client over a child ``kcode acp`` process."""
    return StdioUpstream(config.upstream_argv, timeout=config.upstream_timeout)


def in_process_upstream() -> Upstream:
    """Transport that does nothing, for wiring a handler in tests or tools."""
    return InProcessUpstream()


def build(
    config: Optional[ShimConfig] = None,
    upstream_factory: Optional[Callable[[ShimConfig], Upstream]] = None,
) -> AdoptionShim:
    return AdoptionShim(config=config, upstream_factory=upstream_factory)


def run(
    config: Optional[ShimConfig] = None,
    reader: Optional[TextIO] = None,
    writer: Optional[TextIO] = None,
    upstream_factory: Optional[Callable[[ShimConfig], Upstream]] = None,
) -> int:
    """Build a shim and serve it until EOF. Returns messages read."""
    shim = build(config=config, upstream_factory=upstream_factory or stdio_upstream_factory)
    return shim.serve(reader=reader, writer=writer)
