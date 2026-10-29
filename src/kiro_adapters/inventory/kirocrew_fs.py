"""Kiro Crew's own session ledger.

Kiro Crew keeps two cooperating artifacts under ``~/.kiro``:

``session-index/<hash>.jsonl``
    An explicit add/remove ledger. Every line is
    ``{"op": "add"|"remove", "sessionPath": "<path>", "at": <epoch_ms>}``.
    The live set is the set of paths whose *last* op is ``add``; a later
    ``remove`` for the same path retires it. This is the one place where
    Crew states, in its own words, which sessions exist.

``sessions/<hash>/sess_<uuid>/``
    The session itself. The first line of its jsonl is a JSON object with
    ``"_type": "metadata"`` carrying ``execution_context`` (with ``store``,
    ``member_id``, ``selection_kind``, ``template_id``, ``selection_name``,
    ``selection_revision``), ``memory_mode``, ``model``, ``queued_prompts``,
    ``turn_timeout_secs``, ``created_at`` and ``last_consolidated``.

The ledger's ``sessionPath`` is **relative**, not absolute: it reads
``<hash>/sess_<uuid>`` and resolves under ``~/.kiro/sessions``. The same
directory also holds plain session files such as
``~/.kiro/crew/sessions/dashboard_chat-1-<epoch>.jsonl``, which use the
same metadata line shape. Both layouts are resolved here, because the ledger
only ever names a relative path.

This adapter is strictly read-only. It never writes to the ledger, never
touches a session file except to read it, never starts the crew gateway and
never calls a model. The crew's ledger names sessions but records no pid, so
no envelope from this source claims liveness: the state stays ``unknown``
and ``process_instance`` stays ``None``.

The implementation is split so no single module carries the whole surface:
ledger replay in :mod:`.kirocrew_ledger`, layout and path confinement in
:mod:`.kirocrew_paths`, envelope assembly and declarations in
:mod:`.kirocrew_envelope`, and the transcript surface in
:mod:`.kirocrew_history`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

from .kirocrew_envelope import KiroCrewStoreEnvelopeMixin
from .kirocrew_history import KiroCrewStoreHistoryMixin
from .kirocrew_ledger import KiroCrewLedgerMixin
from .kirocrew_paths import KiroCrewStoreLayoutMixin


class KiroCrewStore(
    KiroCrewLedgerMixin,
    KiroCrewStoreLayoutMixin,
    KiroCrewStoreEnvelopeMixin,
    KiroCrewStoreHistoryMixin,
):
    """Read-only view of the Kiro Crew session ledger."""

    def __init__(
        self,
        kiro_dir: os.PathLike,
        host_id: str = "unknown",
        namespace_id: str = "unknown",
        harness_version: Optional[str] = None,
        harness_lineage: Optional[str] = None,
    ) -> None:
        self.kiro_dir = Path(kiro_dir)
        self.host_id = host_id
        self.namespace_id = namespace_id
        self.harness_version = harness_version
        self.harness_lineage = harness_lineage
        # native id -> session file. Filled by enumerate() so history() can
        # answer without re-reading the whole ledger.
        self._session_files: Dict[str, Path] = {}


def default_store() -> KiroCrewStore:
    """The Kiro Crew store for the current user, if one exists."""
    return KiroCrewStore(Path.home() / ".kiro")
