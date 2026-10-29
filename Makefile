# kiro-adapters developer tasks.
#
# Override the interpreter with:  make PY=/path/to/python3 test
#
# Targets: help (default), test, lint, smoke, mcp-selftest, kiro-backend-gate.

PY ?= /opt/homebrew/bin/python3.12
SRC := src
TESTS := tests
#: The jcode build to check against the Kiro Crew backend gate. Override with:
#:   make kiro-backend-gate JCODE=/path/to/jcode
JCODE ?= $(HOME)/.jcode/builds/versions/0.99.1-kcode-mode/jcode

.DEFAULT_GOAL := help
.PHONY: help test lint smoke mcp-selftest kiro-backend-gate

help: ## Show this help
	@echo "kiro-adapters targets:"
	@echo "  help         Show this help (default)"
	@echo "  test         Run the unittest suite in $(TESTS)"
	@echo "  lint         Byte-compile every source file under $(SRC)"
	@echo "  smoke        Run the CLI status command and validate its JSON"
	@echo "  mcp-selftest Print the MCP tools/list payload and validate it"
	@echo "  kiro-backend-gate  Check a jcode build against Kiro Crew's backend gate"
	@echo ""
	@echo "Interpreter: $(PY)"

test: ## Run the unittest suite
	PYTHONPATH=$(SRC) $(PY) -m unittest discover -s $(TESTS) -t . -v

lint: ## Byte-compile every source file under $(SRC)
	@$(PY) -m py_compile $$(find $(SRC) -name '*.py')
	@echo "lint ok: $$(find $(SRC) -name '*.py' | wc -l | tr -d ' ') file(s) compiled"

smoke: ## Run the CLI status command and validate its JSON
	@PYTHONPATH=$(SRC) $(PY) -m kiro_adapters.cli status --json \
		| $(PY) -c "import json,sys; d=json.load(sys.stdin); print('smoke ok: harness={0} action={1} code={2} ok={3}'.format(d['harness'], d['action'], d['code'], d['ok']))"

mcp-selftest: ## Print the MCP tools/list payload and validate it
	@PYTHONPATH=$(SRC) $(PY) -m kiro_adapters.mcp_server --selftest \
		| $(PY) -c "import json,sys; d=json.load(sys.stdin); names=[t['name'] for t in d['tools']]; print('mcp-selftest ok: {0} tool(s)'.format(len(names))); print('\n'.join(names))"

kiro-backend-gate: ## Check that a jcode build satisfies Kiro Crew's backend gate
	@$(PY) scripts/acceptance/kiro_backend_gate.py $(JCODE)
