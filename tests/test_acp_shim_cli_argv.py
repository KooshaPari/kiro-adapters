"""Regression test: the shipped CLI entry point must honour its own argv.

``__main__.main(argv=None)`` is documented to parse the process argv. Today the
module calls ``main()`` with no argument, so ``split_upstream(None)`` returns an
empty flag list and the default upstream ``("kcode", "acp")``. Every documented
flag then silently does nothing: ``--session-dir`` and ``--socket`` never reach
``gate.assess``, and ``--upstream`` is ignored in favour of a bare ``kcode acp``.

This is an ``xfail(strict=True)`` because the fix is a one-line change to
``src/jcode_bridge/acp_shim/__main__.py`` (pass ``sys.argv[1:]``). It is left
unfixed in this change on purpose: see ``acceptance/acp-live-adoption.md``.
When the fix lands, this test starts passing and the strict xfail will fail,
which is the signal to drop the marker.
"""

from __future__ import annotations

import sys

import pytest

import jcode_bridge.acp_shim.__main__ as cli


@pytest.mark.xfail(
    strict=True,
    reason="main() is called without sys.argv[1:], so CLI flags are dropped",
)
def test_cli_flags_reach_the_shim_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_run(config=None, **_kwargs):
        captured["config"] = config
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "jcode-acp-adopt",
            "--session-dir",
            "/tmp/acceptance-sessions",
            "--socket",
            "/tmp/acceptance.sock",
            "--upstream",
            "/bin/true",
        ],
    )

    assert cli.main() == 0
    config = captured["config"]
    assert config.session_dirs == ("/tmp/acceptance-sessions",)
    assert config.socket_paths == ("/tmp/acceptance.sock",)
    assert config.upstream_argv == ("/bin/true",)
