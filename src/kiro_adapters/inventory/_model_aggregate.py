"""Collection-level operations over envelopes and capability maps.

The envelope itself (:class:`~.model.SessionEnvelope`) describes *one* session.
Everything here describes *many*: merging two adapters' capability declarations,
a deterministic sort order, and the dashboard summary shape. None of it invents
a fact - a merge never promotes an unprobed capability, and a summary never
collapses "process exists" into "controllable".

This module imports the envelope types from :mod:`.model`; :mod:`.model`
re-exports these helpers after its own definitions, so callers keep importing
them from ``kiro_adapters.inventory.model``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .model import Capability, SessionEnvelope


def merge_capabilities(
    primary: Dict[str, Capability], secondary: Dict[str, Capability]
) -> Dict[str, Capability]:
    """Merge two capability maps without ever inventing support.

    A capability is supported in the result if *either* side proved it, and
    unsupported otherwise. A capability neither side declared stays absent
    from the map entirely - the envelope's :meth:`SessionEnvelope.capability`
    turns an absent capability into an explicit unsupported-with-reason, so
    "not declared" never reads as "declared as false".

    When both sides declare the same capability and disagree, the side that
    proved it wins. When both are unsupported, the primary's reason is kept
    and the secondary's is appended, so the disagreement stays visible
    instead of being smoothed over.
    """
    merged: Dict[str, Capability] = dict(secondary)
    for name, cap in primary.items():
        other = merged.get(name)
        if other is None:
            merged[name] = cap
            continue
        if cap.supported and not other.supported:
            merged[name] = cap
        elif cap.supported and other.supported:
            merged[name] = cap
        elif not cap.supported and not other.supported:
            if other.reason and other.reason not in (cap.reason or ""):
                merged[name] = Capability.unsupported(
                    "{}; also: {}".format(cap.reason or "", other.reason)
                )
            else:
                merged[name] = cap
        else:
            # The secondary proved it and the primary did not. Keep the proof.
            merged[name] = other
    return merged


def envelope_sort_key(envelope: SessionEnvelope) -> tuple:
    """Deterministic ordering so two readers see the same list order."""
    return (
        envelope.source_updated_at or "",
        envelope.native_session_id,
        envelope.source,
    )


def summarize(envelopes: List[SessionEnvelope]) -> Dict[str, Any]:
    """Counts by source, launch mode and state.

    A consumer dashboard wants this shape; it deliberately does not collapse
    "process exists" into "controllable".
    """
    by_source: Dict[str, int] = {}
    by_launch_mode: Dict[str, int] = {}
    by_state: Dict[str, int] = {}
    for env in envelopes:
        by_source[env.source] = by_source.get(env.source, 0) + 1
        by_launch_mode[env.launch_mode] = by_launch_mode.get(env.launch_mode, 0) + 1
        by_state[env.state] = by_state.get(env.state, 0) + 1
    return {
        "total": len(envelopes),
        "by_source": by_source,
        "by_launch_mode": by_launch_mode,
        "by_state": by_state,
    }
