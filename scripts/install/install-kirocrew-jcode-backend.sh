#!/bin/bash
# install-kirocrew-jcode-backend.sh — make Kiro Crew run turns on jcode.
#
# Sets the two things that BOTH have to be true, which is the whole point:
#
#   1. ~/.kiro/crew/.env        CODEX_ACP_BIN=<shim>   (which binary the codex slot runs)
#   2. ~/.kiro/crew/config.json agent.acp_backend=codex (that the codex slot is the one selected)
#
# Setting only (1) is silently useless. Kiro Crew's default agent.acp_backend is
# "" (kiro-cli), the codex harness is never resolved, the shim is never spawned,
# and turns are served by the KAS engine while everything appears configured.
# That failure mode cost a full session to diagnose; see
# acceptance/codex-backend-activation.md.
#
# Idempotent. Existing values are reported and replaced, other config keys are
# preserved. Use --dry-run to see what would change.
set -euo pipefail

DRY_RUN=0
JCODE_BIN="${JCODE_ACP_SHIM_BINARY:-}"
PROVIDER="${JCODE_ACP_SHIM_PROVIDER:-opencode-go}"
MODEL="${JCODE_ACP_SHIM_MODEL:-deepseek-v4.1-flash}"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --jcode=*) JCODE_BIN="${arg#--jcode=}" ;;
    --provider=*) PROVIDER="${arg#--provider=}" ;;
    --model=*) MODEL="${arg#--model=}" ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "install: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

KIRO_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
ENV_FILE="$KIRO_HOME/.env"
CONFIG_FILE="$KIRO_HOME/config.json"
SHIM="${JCODE_ACP_SHIM_PATH:-$HOME/bin/jcode-acp-shim}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM_TEMPLATE="$SCRIPT_DIR/jcode-acp-shim"

die() { printf 'install: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

[ -f "$SHIM_TEMPLATE" ] || die "shim template missing: $SHIM_TEMPLATE"
[ -d "$KIRO_HOME" ] || die "Kiro Crew home not found: $KIRO_HOME (is Kiro Crew installed?)"

# --- resolve the jcode binary -------------------------------------------------
# The ACP `mode` option is what arms the read-only gate, so an arbitrary jcode
# build is not good enough. Prefer an explicitly versioned build, then the fork,
# then PATH.
if [ -z "$JCODE_BIN" ]; then
  for candidate in \
    "$HOME/.jcode/builds/versions/0.99.1-kcode-mode/jcode" \
    "$HOME/.kcode/builds/current/kcode" \
    "$(command -v kcode 2>/dev/null || true)" \
    "$(command -v jcode 2>/dev/null || true)"
  do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then JCODE_BIN="$candidate"; break; fi
  done
fi
[ -n "$JCODE_BIN" ] || die "no jcode binary found; pass --jcode=/path/to/jcode"
[ -x "$JCODE_BIN" ] || die "jcode binary is not executable: $JCODE_BIN"

say "jcode binary : $JCODE_BIN"
say "provider     : $PROVIDER"
say "model        : $MODEL"
say "shim         : $SHIM"
say "kiro home    : $KIRO_HOME"

# --- render the shim ----------------------------------------------------------
rendered="$(mktemp "${TMPDIR:-/tmp}/jcode-acp-shim.XXXXXX")"
trap 'rm -f "$rendered"' EXIT
sed -e "s|@JCODE_BINARY@|$JCODE_BIN|g" \
    -e "s|@PROVIDER@|$PROVIDER|g" \
    -e "s|@MODEL@|$MODEL|g" \
    "$SHIM_TEMPLATE" > "$rendered"
chmod 0755 "$rendered"
bash -n "$rendered" || die "rendered shim has a syntax error"

if [ "$DRY_RUN" -eq 1 ]; then
  say ""
  say "--- dry run: shim would be written to $SHIM ---"
  say "--- dry run: $ENV_FILE would get CODEX_ACP_BIN=$SHIM ---"
  say "--- dry run: $CONFIG_FILE would get agent.acp_backend=\"codex\" ---"
  say ""
  say "no changes made."
  exit 0
fi

mkdir -p "$(dirname "$SHIM")"
install -m 0755 "$rendered" "$SHIM"
say "wrote shim   : $SHIM"

# --- .env ---------------------------------------------------------------------
touch "$ENV_FILE"
chmod 0600 "$ENV_FILE"
if grep -q '^CODEX_ACP_BIN=' "$ENV_FILE"; then
  say "updated env  : CODEX_ACP_BIN (was $(grep '^CODEX_ACP_BIN=' "$ENV_FILE" | head -1 | cut -d= -f2-))"
  tmp_env="$(mktemp "$ENV_FILE.XXXXXX")"
  grep -v '^CODEX_ACP_BIN=' "$ENV_FILE" > "$tmp_env" || true
  printf 'CODEX_ACP_BIN=%s\n' "$SHIM" >> "$tmp_env"
  chmod 0600 "$tmp_env"
  mv "$tmp_env" "$ENV_FILE"
else
  printf 'CODEX_ACP_BIN=%s\n' "$SHIM" >> "$ENV_FILE"
  say "set env      : CODEX_ACP_BIN=$SHIM"
fi

# --- config.json: agent.acp_backend ------------------------------------------
# Config is read at boot, so the gateway must be restarted afterwards.
python3 - "$CONFIG_FILE" <<'PY'
import json, os, sys

path = sys.argv[1]
if not os.path.exists(path):
    raise SystemExit(f"install: config not found: {path}")
with open(path, encoding="utf-8") as fh:
    cfg = json.load(fh)

agent = cfg.setdefault("agent", {})
before = agent.get("acp_backend")
if before == "codex":
    print("config       : agent.acp_backend already \"codex\"")
else:
    agent["acp_backend"] = "codex"
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    print(f"config       : agent.acp_backend {before!r} -> 'codex'")
PY

say ""
say "Restart the gateway so the config and .env are re-read:"
say "    kirocrew restart"
say ""
say "Then verify the whole chain:"
say "    ./scripts/install/verify-kirocrew-jcode-backend.sh"
