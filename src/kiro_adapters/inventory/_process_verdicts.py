"""The verdicts a liveness probe is allowed to return.

They live in their own module so that the platform helpers (POSIX and Windows)
and the policy layer can all name them without importing each other, and so
that there is exactly one definition of each string.

The third verdict is the important one: a probe that could not answer must
never be reported as "the process is gone".
"""

from __future__ import annotations

# Liveness verdicts.
ALIVE = "alive"
GONE = "gone"
UNKNOWN = "unknown"

# What a start identity looks like when the platform will not say.
UNKNOWN_START = "unknown"
