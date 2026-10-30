"""An ACP upstream that never answers.

Used by acceptance/shim_live_probe.py to exercise the shim's fail-closed path:
the upstream link is up (the process is alive) but it does not answer
`initialize`, so the shim must fall back to its own advertised capabilities
rather than hanging or leaking internals.
"""
import sys
import time

for _line in sys.stdin:
    pass
time.sleep(600)
