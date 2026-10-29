"""One read-only entry point over every session-inventory adapter.

The individual adapters (:mod:`.jcode_fs`, :mod:`.kirocrew_fs`) each answer
"what sessions does *this* store hold". A consumer app does not want to know
which store exists on this host; it wants one list, one lookup and one history
call that behave the same way whether the store is ``~/.jcode``, ``~/.kcode``
or ``~/.kiro``.

Three rules shape this module:

1. **Nothing here can raise.** A missing store, an unreadable directory, a
   malformed session uid or a platform quirk comes back as an empty list, a
   ``None`` or a failure-shaped dict. The MCP surface renders those directly,
   so a broken store must not become a broken tool.
2. **Nothing here starts anything.** No daemon is contacted, no harness binary
   is executed, and the jcode stores are built with ``probe_processes=False``
   so even the ``ps`` probe is off. A discovery call is a file read.
3. **History dispatches on the uid's source prefix.** A ``session_uid`` is
   ``"<source>:<native_session_id>"``; the prefix selects the store, so the
   caller never decides which adapter to use.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import jcode_fs, kirocrew_fs
from .model import (
    MAX_HISTORY_LIMIT,
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
    SOURCE_KIROCREW_LEDGER,
    STATE_STALE,
    SessionEnvelope,
    clamp_history_limit,
    envelope_sort_key,
    now_iso,
    summarize,
)

# Ceiling on how many envelopes one list call may return. Enumeration itself is
# bounded per store; this bounds the combined answer a consumer receives.
MAX_SESSION_LIMIT = 500
DEFAULT_SESSION_LIMIT = 200

DEFAULT_HISTORY_LIMIT = 20

# The sources this registry knows how to read, in a stable order so two calls
# produce the same output.
KNOWN_SOURCES = (
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
    SOURCE_KIROCREW_LEDGER,
)

_HARNESS_KIND = {
    SOURCE_JCODE_FS: "jcode",
    SOURCE_KCODE_FS: "kcode",
    SOURCE_KIROCREW_LEDGER: "kirocrew",
}

_STORE_SUBDIR = {
    SOURCE_JCODE_FS: ".jcode",
    SOURCE_KCODE_FS: ".kcode",
    SOURCE_KIROCREW_LEDGER: ".kiro",
}


# -- store construction ----------------------------------------------------
def _build_store(source: str):
    """Construct the read-only store for ``source``, or ``None`` if unknown.

    The jcode-family stores are built with process probing off: the registry
    promises not to spawn a probe process, and that promise is cheaper to keep
    than to explain. A pid file then stays a hint, and ``discover_process``
    stays honestly unproven.
    """
    if source == SOURCE_JCODE_FS:
        return jcode_fs.default_store(probe_processes=False)
    if source == SOURCE_KCODE_FS:
        return jcode_fs.kcode_store(probe_processes=False)
    if source == SOURCE_KIROCREW_LEDGER:
        return kirocrew_fs.default_store()
    return None


def _store_path(source: str) -> Optional[str]:
    """The conventional store path for a source, without touching it."""
    subdir = _STORE_SUBDIR.get(source)
    if subdir is None:
        return None
    try:
        from pathlib import Path

        return str(Path.home() / subdir)
    except (RuntimeError, OSError):
        # No resolvable home directory is a fact about the host, not an error.
        return None


def _present(source: str) -> bool:
    store = _build_store(source)
    if store is None:
        return False
    try:
        return bool(store.exists())
    except (OSError, ValueError):
        return False


def available_sources() -> List[Dict[str, Any]]:
    """Every source this registry can read, with presence and store path.

    Presence is a directory-existence check only: it does not enumerate and
    does not read a record, so calling this is cheap on a machine with a large
    session store.
    """
    out: List[Dict[str, Any]] = []
    for source in KNOWN_SOURCES:
        out.append(
            {
                "source": source,
                "harness_kind": _HARNESS_KIND[source],
                "path": _store_path(source),
                "present": _present(source),
            }
        )
    return out


# -- session uid handling --------------------------------------------------
def parse_session_uid(session_uid: Any) -> Optional[Tuple[str, str]]:
    """Split ``"<source>:<native_session_id>"`` into its two parts.

    Returns ``None`` for anything that is not a uid naming a known source with
    a non-empty native id. The split is on the *first* colon only: a native id
    may legitimately contain one.
    """
    if not isinstance(session_uid, str):
        return None
    source, sep, native_id = session_uid.partition(":")
    if not sep or not native_id or source not in KNOWN_SOURCES:
        return None
    return source, native_id


# -- enumeration -----------------------------------------------------------
def enumerate_sessions(
    limit: Optional[int] = None,
    include_stale: bool = True,
) -> List[SessionEnvelope]:
    """Every session the present stores can name, in a deterministic order.

    A store that raises while enumerating contributes nothing rather than
    failing the call: one unreadable store must not blind a consumer to the
    other two. ``include_stale=False`` drops sessions whose jcode heartbeat is
    missing -- those are the ones a ``~/.jcode`` directory keeps long after the
    process is gone.
    """
    try:
        bound = DEFAULT_SESSION_LIMIT if limit is None else int(limit)
    except (TypeError, ValueError):
        bound = DEFAULT_SESSION_LIMIT
    if bound <= 0:
        return []
    bound = min(bound, MAX_SESSION_LIMIT)

    envelopes: List[SessionEnvelope] = []
    for source in KNOWN_SOURCES:
        store = _build_store(source)
        if store is None:
            continue
        try:
            if source == SOURCE_KIROCREW_LEDGER:
                found = store.enumerate(limit=bound)
            else:
                found = store.enumerate()
        except Exception:  # noqa: BLE001 - one store must not break the list
            continue
        if not include_stale:
            found = [
                envelope for envelope in found if envelope.state != STATE_STALE
            ]
        envelopes.extend(found)

    envelopes.sort(key=envelope_sort_key)
    return envelopes[:bound]


def find_session(session_uid: Any) -> Optional[SessionEnvelope]:
    """The envelope for one uid, or ``None``.

    Lookup goes through :func:`enumerate_sessions` so a found session is
    always one the list would also have shown: a ``get`` that can return an
    envelope the list hides would be a different inventory.
    """
    if parse_session_uid(session_uid) is None:
        return None
    for envelope in enumerate_sessions(limit=MAX_SESSION_LIMIT):
        if envelope.session_uid == session_uid:
            return envelope
    return None


def inventory_summary(include_stale: bool = True) -> Dict[str, Any]:
    """Counts by source, launch mode and state, plus the source roster."""
    envelopes = enumerate_sessions(include_stale=include_stale)
    summary = summarize(envelopes)
    summary["include_stale"] = include_stale
    summary["observed_at"] = now_iso()
    summary["sources"] = available_sources()
    return summary


# -- history ---------------------------------------------------------------
def _history_failure(
    session_uid: Any, detail: str, key: str = "messages"
) -> Dict[str, Any]:
    return {
        "ok": False,
        "session_uid": session_uid if isinstance(session_uid, str) else None,
        "detail": detail,
        key: [],
    }


def session_history(
    session_uid: Any,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> Dict[str, Any]:
    """A bounded, redacted slice of one session, dispatched by uid prefix.

    The adapters already bound and redact their own slices; this function only
    routes the request and adds the uid/source so a consumer can tell which
    store answered. ``limit`` is clamped once, here, against the shared
    :data:`~.model.MAX_HISTORY_LIMIT` ceiling.
    """
    parsed = parse_session_uid(session_uid)
    if parsed is None:
        return _history_failure(
            session_uid,
            "session_uid must be '<source>:<native_session_id>' for a known "
            "source (one of: {})".format(", ".join(KNOWN_SOURCES)),
        )
    source, native_id = parsed
    window = clamp_history_limit(limit, MAX_HISTORY_LIMIT)

    store = _build_store(source)
    if store is None:  # pragma: no cover - parse_session_uid already filtered
        return _history_failure(session_uid, "no store for source {!r}".format(source))
    try:
        result = store.history(native_id, limit=window)
    except Exception as exc:  # noqa: BLE001 - a read failure is data, not a raise
        return _history_failure(
            session_uid,
            "history read failed: {}: {}".format(type(exc).__name__, exc),
        )

    if not isinstance(result, dict):
        return _history_failure(session_uid, "history read returned no mapping")

    enriched = dict(result)
    enriched["session_uid"] = session_uid
    enriched["source"] = source
    enriched["limit"] = window
    # Two adapters name their window differently; normalise the count so a
    # consumer does not have to branch on source to know how much came back.
    if "returned" not in enriched:
        key = "messages" if isinstance(enriched.get("messages"), list) else "events"
        value = enriched.get(key)
        enriched["returned"] = len(value) if isinstance(value, list) else 0
    return enriched


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_SESSION_LIMIT",
    "KNOWN_SOURCES",
    "MAX_SESSION_LIMIT",
    "available_sources",
    "enumerate_sessions",
    "find_session",
    "inventory_summary",
    "parse_session_uid",
    "session_history",
]
