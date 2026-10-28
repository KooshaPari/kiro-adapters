"""Kiro Code adapter — drive the Kiro coding CLI from jcode.

Verification status (be precise about this):

* The ``kirocrew`` adapter in this package is verified against a live install.
* This adapter is **not** verified against a live ``kirocode`` binary on this
  machine: neither ``kirocode`` nor ``kiro-cli`` is installed. What *is*
  implemented is capability **probing** — the adapter resolves whichever Kiro
  coding binary exists, reads its ``--help``, and picks an invocation the
  binary actually advertises. Nothing here assumes a flag it has not seen.

Override order:
1. ``KIROCODE_BIN`` environment variable
2. ``~/.kirocode/adapter.json`` -> ``{"bin": "...", "argv": ["chat", "{prompt}"]}``
3. PATH lookup: ``kirocode``, ``kiro-cli``, ``kiro``

With a config ``argv`` template, ``{prompt}`` is substituted and the template is
used verbatim, which is how an unanticipated CLI shape is supported without a
code change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import result as R
from .redact import redact
from .runner import CommandResult, first_line, resolve_binary, run

HARNESS = "kirocode"
BINARY_CANDIDATES = ("kirocode", "kiro-cli", "kiro")
ENV_VAR = "KIROCODE_BIN"
DEFAULT_CONFIG = "~/.kirocode/adapter.json"
DEFAULT_TIMEOUT = 900.0
PROBE_TIMEOUT = 30.0


class KiroCodeAdapter:
    """Resolve, probe and invoke the Kiro coding CLI."""

    harness = HARNESS

    def __init__(
        self,
        binary: Optional[str] = None,
        config_path: Optional[str] = None,
        env_var: str = ENV_VAR,
    ) -> None:
        self._binary = binary
        self.config_path = Path(os.path.expanduser(config_path or DEFAULT_CONFIG))
        self.env_var = env_var
        self._resolved: Optional[str] = None
        self._tried: List[str] = []
        self._help_text: Optional[str] = None
        self._config: Optional[Dict[str, Any]] = None

    # -- config -----------------------------------------------------------
    @property
    def config(self) -> Dict[str, Any]:
        if self._config is None:
            payload: Dict[str, Any] = {}
            try:
                with open(self.config_path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    payload = loaded
            except FileNotFoundError:
                payload = {}
            except (OSError, ValueError):
                payload = {"_error": "unreadable config: {}".format(self.config_path)}
            self._config = payload
        return self._config

    # -- resolution -------------------------------------------------------
    @property
    def binary(self) -> Optional[str]:
        if self._binary:
            return self._binary
        if self._resolved is None:
            configured = self.config.get("bin")
            if configured and not os.environ.get(self.env_var):
                self._resolved = configured if os.path.isfile(str(configured)) else None
                self._tried = ["config.bin={}".format(configured)]
                if self._resolved is None:
                    from shutil import which

                    self._resolved = which(str(configured))
            else:
                self._resolved, self._tried = resolve_binary(BINARY_CANDIDATES, self.env_var)
        return self._resolved

    def resolve_info(self) -> Dict[str, Any]:
        return {
            "binary": self.binary,
            "found": self.binary is not None,
            "tried": self._tried if not self._binary else ["explicit:{}".format(self._binary)],
            "config_path": str(self.config_path),
            "config_present": self.config_path.is_file(),
        }

    def _missing(self, action: str) -> R.AdapterResult:
        return R.AdapterResult.failure(
            HARNESS,
            action,
            R.NOT_INSTALLED,
            "no Kiro coding CLI found (kirocode / kiro-cli / kiro)",
            data=self.resolve_info(),
            hints=[
                "Install the Kiro coding CLI, or point at it explicitly: "
                "export {}=/path/to/kiro-cli".format(self.env_var),
                "For an unusual CLI shape, write {} with "
                '{{"bin": "kiro-cli", "argv": ["chat", "--no-interactive", "{{prompt}}"]}}'.format(
                    self.config_path
                ),
            ],
        )

    # -- probing ----------------------------------------------------------
    def help_text(self, refresh: bool = False) -> str:
        if self._help_text is None or refresh:
            if self.binary is None:
                self._help_text = ""
            else:
                cmd = run([self.binary, "--help"], timeout=PROBE_TIMEOUT)
                self._help_text = (cmd.stdout or "") + "\n" + (cmd.stderr or "")
        return self._help_text or ""

    def version(self) -> Optional[str]:
        if self.binary is None:
            return None
        cmd = run([self.binary, "--version"], timeout=PROBE_TIMEOUT)
        text = first_line(cmd.stdout) or first_line(cmd.stderr)
        return text or None

    def capabilities(self) -> Dict[str, Any]:
        text = self.help_text()
        return {
            "has_chat": "chat" in text,
            "has_exec": "exec" in text or "run" in text,
            "supports_no_interactive": "--no-interactive" in text,
            "supports_print": "--print" in text or " -p" in text,
            "help_bytes": len(text),
        }

    def status(self) -> R.AdapterResult:
        if self.binary is None:
            return self._missing("status")
        caps = self.capabilities()
        argv = self._build_ask_argv("ping", caps)
        unsupported = argv is None
        data = {
            "binary": self.binary,
            "version": self.version(),
            "capabilities": caps,
            "ask_argv_template": argv if not unsupported else None,
            "argv_source": self._argv_source(),
            "verified_live": False,
        }
        hints: List[str] = []
        if unsupported:
            hints.append(
                "Could not infer a non-interactive invocation; write {}".format(
                    self.config_path
                )
            )
        return R.AdapterResult.success(
            HARNESS,
            "status",
            "{} ({} argv)".format(
                self.version() or self.binary,
                "configured" if not unsupported else "unresolved",
            ),
            data=data,
            hints=hints,
        )

    def _argv_source(self) -> str:
        template = self.config.get("argv")
        if isinstance(template, list) and template:
            return "config"
        return "help-probe"

    def _build_ask_argv(
        self, prompt: str, caps: Optional[Dict[str, Any]] = None
    ) -> Optional[List[str]]:
        """Return argv for a one-shot prompt, or None when nothing is inferable."""
        if self.binary is None:
            return None
        template = self.config.get("argv")
        if isinstance(template, list) and template:
            parts = [str(part) for part in template]
            # A template describes the subcommand and its flags, not the
            # executable: prepend the resolved binary unless the template
            # already says where it is. Without this a template like
            # ["chat", "{prompt}"] would run whatever `chat` is on PATH.
            uses_bin_slot = any("{bin}" in part for part in parts)
            if not uses_bin_slot and not os.path.isabs(parts[0]):
                parts = [self.binary] + parts
            return [
                part.replace("{prompt}", prompt).replace("{bin}", self.binary)
                for part in parts
            ]
        caps = caps if caps is not None else self.capabilities()
        if caps.get("supports_no_interactive"):
            return [self.binary, "chat", "--no-interactive", prompt]
        if caps.get("supports_print"):
            return [self.binary, "chat", "--print", prompt]
        if caps.get("has_chat"):
            # Last resort: chat with the prompt as a positional argument. Only
            # reached when the binary advertises no non-interactive flag at all.
            return [self.binary, "chat", prompt]
        if caps.get("has_exec"):
            return [self.binary, "exec", prompt]
        return None

    # -- actions ----------------------------------------------------------
    def ask(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> R.AdapterResult:
        if self.binary is None:
            return self._missing("ask")
        if not (prompt or "").strip():
            return R.AdapterResult.failure(
                HARNESS, "ask", R.ERROR, "prompt must not be empty"
            )
        argv = self._build_ask_argv(prompt)
        if argv is None:
            return R.AdapterResult.failure(
                HARNESS,
                "ask",
                R.UNSUPPORTED,
                "could not infer a non-interactive invocation for {}".format(self.binary),
                data={"capabilities": self.capabilities()},
                hints=[
                    "Write {} with an explicit argv template".format(self.config_path),
                    "Run `{} --help` to inspect the real flags".format(self.binary),
                ],
            )
        cmd: CommandResult = run(argv, timeout=timeout, cwd=cwd)
        payload = {
            "argv": cmd.argv,
            "exit_code": cmd.code,
            "answer": redact(cmd.stdout).strip(),
        }
        if cmd.spawn_error:
            return R.AdapterResult.failure(
                HARNESS,
                "ask",
                R.NOT_INSTALLED,
                "failed to launch {}: {}".format(argv[0], cmd.spawn_error),
                data=payload,
                duration_ms=cmd.duration_ms,
            )
        if cmd.timed_out:
            return R.AdapterResult.failure(
                HARNESS,
                "ask",
                R.TIMEOUT,
                "timed out after {}s".format(int(timeout)),
                data=payload,
                stdout=redact(cmd.stdout),
                stderr=redact(cmd.stderr),
                duration_ms=cmd.duration_ms,
            )
        if cmd.code != 0:
            return R.AdapterResult.failure(
                HARNESS,
                "ask",
                R.ERROR,
                "{} exited {}".format(os.path.basename(str(argv[0])), cmd.code),
                data=payload,
                stdout=redact(cmd.stdout),
                stderr=redact(cmd.stderr),
                duration_ms=cmd.duration_ms,
            )
        return R.AdapterResult.success(
            HARNESS,
            "ask",
            "{} chars".format(len(payload["answer"])),
            data=payload,
            stdout=redact(cmd.stdout),
            stderr=redact(cmd.stderr),
            duration_ms=cmd.duration_ms,
        )
