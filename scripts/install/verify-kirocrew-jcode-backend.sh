#!/bin/bash
# verify-kirocrew-jcode-backend.sh — check the whole Kiro Crew -> jcode chain.
#
# Every link is checked separately and reported PASS/FAIL/UNKNOWN, because the
# failure this exists to catch is "everything looks configured but the shim is
# never spawned". A single green light is not enough: the config must select the
# codex slot AND the env must name a working shim AND that shim must point at a
# real jcode binary AND the gateway must have been restarted since.
#
# Exits 0 only if every required link passes. --live additionally runs one real
# turn, which is the only check that proves the whole path end to end.
set -uo pipefail

LIVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

KIRO_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
ENV_FILE="$KIRO_HOME/.env"
CONFIG_FILE="$KIRO_HOME/config.json"
SHIM="${JCODE_ACP_SHIM_PATH:-$HOME/bin/jcode-acp-shim}"
PORT="${KIROCREW_PORT:-5476}"

fails=0
warns=0
pass() { printf 'PASS  %-22s %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %-22s %s\n' "$1" "$2"; fails=$((fails+1)); }
warn() { printf 'WARN  %-22s %s\n' "$1" "$2"; warns=$((warns+1)); }
info() { printf '      %-22s %s\n' "$1" "$2"; }

echo "Kiro Crew -> jcode backend"
echo "--------------------------"

# 1. Kiro Crew present
if [ -f "$CONFIG_FILE" ]; then
  pass "kiro-crew-home" "$KIRO_HOME"
else
  fail "kiro-crew-home" "no config.json at $CONFIG_FILE (is Kiro Crew installed?)"
  echo
  echo "RESULT: FAIL (cannot continue)"
  exit 1
fi

# 2. config selects the codex slot -- the link that was silently wrong for a whole session
backend="$(python3 -c '
import json,sys
try:
    cfg=json.load(open(sys.argv[1],encoding="utf-8"))
except Exception as e:
    print("PARSE_ERROR:"+type(e).__name__); raise SystemExit
print(cfg.get("agent",{}).get("acp_backend") or "")
' "$CONFIG_FILE" 2>/dev/null)"

case "$backend" in
  codex) pass "acp_backend" "agent.acp_backend = \"codex\" (codex slot selected)" ;;
  PARSE_ERROR:*) fail "acp_backend" "config.json did not parse ($backend)" ;;
  "") fail "acp_backend" 'agent.acp_backend is unset (defaults to "" = kiro-cli, so the shim is NEVER spawned)' ;;
  *) fail "acp_backend" "agent.acp_backend = \"$backend\"; must be \"codex\" for CODEX_ACP_BIN to be consulted" ;;
esac

# 3. .env names the shim
env_bin=""
if [ -f "$ENV_FILE" ]; then
  env_bin="$(grep '^CODEX_ACP_BIN=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)"
fi
if [ -z "$env_bin" ]; then
  fail "CODEX_ACP_BIN" "not set in $ENV_FILE"
elif [ "$env_bin" != "$SHIM" ]; then
  warn "CODEX_ACP_BIN" "set to $env_bin (expected $SHIM)"
  SHIM="$env_bin"
else
  pass "CODEX_ACP_BIN" "$env_bin"
fi

# 4. shim executable + syntactically valid
shim_ok=0
if [ ! -f "$SHIM" ]; then
  fail "shim" "missing: $SHIM"
elif [ ! -x "$SHIM" ]; then
  fail "shim" "not executable: $SHIM"
elif ! bash -n "$SHIM" 2>/dev/null; then
  fail "shim" "syntax error: $SHIM"
else
  pass "shim" "$SHIM"
  shim_ok=1
fi

# 5. the jcode binary the shim will exec
if [ "$shim_ok" -eq 1 ]; then
  jcode_bin="$(bash -c '
    set -uo pipefail
    # Read the default out of the shim without executing its exec line.
    grep -oE "JCODE_ACP_SHIM_BINARY:-[^}]+" "'"$SHIM"'" 2>/dev/null | head -1 | sed "s/^JCODE_ACP_SHIM_BINARY:-//"
  ')"
  # The shim writes this default with $HOME unexpanded so it survives a move of
  # the home directory. Expand exactly $HOME/${HOME} here and nothing else: an
  # earlier version tested the literal string "$HOME/..." with -x, which failed
  # on a shim that was in fact working. Do not generalise this to eval.
  jcode_bin="${jcode_bin/\$\{HOME\}/$HOME}"
  jcode_bin="${jcode_bin/\$HOME/$HOME}"
  if [ -n "$jcode_bin" ] && [ -x "$jcode_bin" ]; then
    pass "jcode-binary" "$jcode_bin"
  elif [ -n "$jcode_bin" ]; then
    fail "jcode-binary" "not executable: $jcode_bin"
  else
    info "jcode-binary" "could not read default from shim; it may be overridden at runtime"
  fi
  # Provider/model pin: a Bedrock default is what produced the misleading 5xx.
  prov="$(grep -oE 'JCODE_ACP_SHIM_PROVIDER:-[^}]+' "$SHIM" 2>/dev/null | head -1 | sed 's/^JCODE_ACP_SHIM_PROVIDER:-//')"
  modl="$(grep -oE 'JCODE_ACP_SHIM_MODEL:-[^}]+' "$SHIM" 2>/dev/null | head -1 | sed 's/^JCODE_ACP_SHIM_MODEL:-//')"
  if [ -n "$prov" ] && [ -n "$modl" ]; then
    pass "provider-model" "$prov / $modl"
  else
    warn "provider-model" "no pin found; the adapter default may be unservable here"
  fi
fi

# 6. gateway listening
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  gw_pid="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  pass "gateway" "listening on 127.0.0.1:$PORT (pid $gw_pid)"
  # .env is only read at boot: a running gateway may predate the current file.
  if [ -f "$ENV_FILE" ] && [ -n "$gw_pid" ]; then
    if ps eww -p "$gw_pid" 2>/dev/null | tr ' ' '\n' | grep -qx "CODEX_ACP_BIN=$SHIM"; then
      pass "env-loaded" "gateway process has CODEX_ACP_BIN=$SHIM"
    else
      warn "env-loaded" "gateway pid $gw_pid does not show CODEX_ACP_BIN=$SHIM; .env is read at boot -- run 'kirocrew restart'"
    fi
  fi
else
  fail "gateway" "nothing listening on 127.0.0.1:$PORT"
fi

# 7. optional live turn -- the only proof of the whole path
if [ "$LIVE" -eq 1 ]; then
  if command -v kirocrew >/dev/null 2>&1; then
    out="$(timeout 300 kirocrew chat -m "Reply with exactly: BACKEND_VERIFY_OK" 2>&1)"
    if printf '%s' "$out" | grep -q 'BACKEND_VERIFY_OK'; then
      pass "live-turn" "gateway answered BACKEND_VERIFY_OK"
    else
      fail "live-turn" "no BACKEND_VERIFY_OK in response: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
    fi
  else
    warn "live-turn" "kirocrew not on PATH"
  fi
else
  info "live-turn" "skipped (pass --live to run one real turn)"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "RESULT: PASS${warns:+ ($warns warning(s))}"
  [ "$LIVE" -eq 0 ] && echo "NOTE:   the shim is configured but no turn was executed. Use --live to prove the path."
  exit 0
fi
echo "RESULT: FAIL ($fails failure(s), $warns warning(s))"
exit 1
