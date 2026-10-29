"""Capability declarations must be backed by a real probe.

Two focused concerns live here:

* ``discover_process`` on the jcode file store is proved only when the
  enumeration actually bound a pid to a process start identity. A recorded
  pid that the process table cannot bind, or no pid at all, is unsupported
  with a reason.
* The crew ledger adapter imports ``sanitize_native_id`` once at module
  level; no function may import it (or anything else) inside a loop.

Standard library only. No network.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory.jcode_fs import JcodeStore  # noqa: E402
from kiro_adapters.inventory.model import CAP_DISCOVER_PROCESS  # noqa: E402
from test_inventory_jcode import (  # noqa: E402
    DEAD_PID,
    build_empty_store,
    envelope_for,
    write_session,
)

ALPHA = "session_caps_1700000000000_aaaaaaaaaaaaaaaaaaaaaaaa"
BETA = "session_caps_1700000100000_bbbbbbbbbbbbbbbbbbbbbbbbbbbb"
GAMMA = "session_caps_1700000200000_cccccccccccccccccccccccccccc"

KIROCREW_FS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "kiro_adapters",
    "inventory",
    "kirocrew_fs.py",
)


class DiscoverProcessCapabilityTests(unittest.TestCase):
    """``discover_process`` is proved only by a bound process identity."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store_dir = build_empty_store(self.root)
        self.store = JcodeStore(self.store_dir)

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_no_pid_is_unsupported_with_a_reason(self):
        write_session(self.store_dir, ALPHA)
        capability = envelope_for(self.store.enumerate(), ALPHA).capability(
            CAP_DISCOVER_PROCESS
        )
        self.assertFalse(capability.supported)
        self.assertTrue(capability.reason)
        self.assertIn("no pid", capability.reason)

    def test_an_unbindable_pid_is_unsupported_with_a_reason(self):
        write_session(self.store_dir, BETA, pid=DEAD_PID)
        capability = envelope_for(self.store.enumerate(), BETA).capability(
            CAP_DISCOVER_PROCESS
        )
        self.assertFalse(capability.supported)
        self.assertTrue(capability.reason)
        self.assertIn(str(DEAD_PID), capability.reason)

    def test_a_bound_live_pid_is_proved_with_a_probe(self):
        write_session(self.store_dir, GAMMA, pid=os.getpid())
        capability = envelope_for(self.store.enumerate(), GAMMA).capability(
            CAP_DISCOVER_PROCESS
        )
        self.assertTrue(capability.supported)
        self.assertTrue(capability.probe)
        self.assertTrue(capability.observed_at)


class KiroCrewImportHygieneTests(unittest.TestCase):
    """The crew ledger imports its helpers once, at module level."""

    def _functional_imports(self):
        with open(KIROCREW_FS, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=KIROCREW_FS)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    found.append((node.name, inner.lineno))
        return found

    def test_only_redact_obj_is_imported_inside_a_function(self):
        found = self._functional_imports()
        names = {name for name, _lineno in found}
        # ``redact_obj`` lives in a sibling package and is imported lazily on
        # purpose; it is not part of the model, so it is allowed.
        self.assertTrue(
            all(name == "history" for name, _lineno in found),
            "unexpected function-local import in: {!r}".format(sorted(names)),
        )

    def test_sanitize_native_id_is_not_imported_inside_a_function(self):
        for name, lineno in self._functional_imports():
            self.assertNotEqual(
                name,
                "enumerate",
                "enumerate() imports at line {}; hoist it to module level".format(
                    lineno
                ),
            )


if __name__ == "__main__":
    unittest.main()
