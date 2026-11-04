"""Regression test: the shipped CLI entry point must honour its own argv.

``__main__.main(argv=None)`` is documented to parse the process argv.
Originally the module called ``main()`` with no argument, so
``split_upstream(None)`` returned an empty flag list and the default upstream
``("kcode", "acp")``. Every documented flag then silently did nothing:
``--session-dir`` and ``--socket`` never reached ``gate.assess``, and
``--upstream`` was ignored in favour of a bare ``kcode acp``. That was a live
operator trap - the flags parsed, the process started, the shim ran, and only the
behaviour was wrong.

Fixed by defaulting ``argv`` to ``sys.argv[1:]`` inside ``main``, which covers
both the console-script entry point (called with zero arguments) and
``python -m``. This test would have failed before that fix, so it is kept as a
plain assertion rather than an xfail marker.
"""

from __future__ import annotations

import sys

import pytest

import jcode_bridge.acp_shim.__main__ as cli


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
