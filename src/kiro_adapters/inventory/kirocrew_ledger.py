"""Ledger replay for the Kiro Crew session inventory.

The crew ledger is an append-only add/remove log; the live set is the set of
paths whose *last* op is ``add``. Replaying it is the one place where Crew
states, in its own words, which sessions exist.
"""

from __future__ import annotations

import json
from typing import Dict, Tuple


class KiroCrewLedgerMixin:
    """Read-only replay of ``session-index/*.jsonl``."""

    def _replay_ledger(self) -> Dict[str, Tuple[bool, int]]:
        """Replay the ledger once, into ``path -> (live, at_ms)``.

        Ops are replayed in file order and then line order. Each index file
        is keyed by its own workspace hash, so paths do not normally collide
        across files; when they do, the later op wins, which is the ledger's
        own semantics. A path that was added and later removed is retired,
        even though its add op is still in the file.
        """
        state: Dict[str, Tuple[bool, int]] = {}
        if not self.index_dir.is_dir():
            return state
        try:
            files = sorted(self.index_dir.glob("*.jsonl"))
        except OSError:
            return state
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    # A half-written line must not take the ledger down.
                    continue
                if not isinstance(record, dict):
                    continue
                session_path = record.get("sessionPath")
                if not isinstance(session_path, str) or not session_path:
                    continue
                try:
                    at = int(record.get("at", 0))
                except (TypeError, ValueError):
                    at = 0
                state[session_path] = (record.get("op") == "add", at)
        return state

    def _live_sessions(self) -> Dict[str, int]:
        """The live set: paths whose last op was an ``add``."""
        return {
            path: at
            for path, (live, at) in self._replay_ledger().items()
            if live
        }
