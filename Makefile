# Quality targets. The PostToolUse hook, pre-commit, and CI all call these
# same targets so the three places enforce identical commands.
#
# Per-repo knobs (override in the repo's Makefile after `include`, or here):
#   LINT_SCOPE=repo     base tier runs whole-repo (legacy tree is base-clean)
#   LINT_SCOPE=changed  base tier runs on changed lines only (legacy tree is
#                       not base-clean yet; the ratchet applies to both tiers)
#
# fantasy_football knobs, measured 2026-09-03:
#   PY/RUFF           the venv here is `venv/`, not `.venv/`.
#   LINT_SCOPE=repo   `ruff check .` on the base tier is green on the whole
#                     legacy tree, so the floor is enforced everywhere.
#   TYPECHECK_PATHS   tools/quality only. mypy on src/ reports 55 errors in 9
#                     of 17 modules; add src/ here module by module as they are
#                     annotated and cleaned.
#   TEST_PATHS        both suites: the repo's own tests/ and the quality gate's.
PY ?= venv/bin/python
RUFF ?= venv/bin/ruff
GATE = $(PY) tools/quality/gate_changed_lines.py
LINT_SCOPE ?= repo
TYPECHECK_PATHS ?= tools/quality
TEST_PATHS ?= tests tools/quality/tests
RATCHET_BASE = $$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || echo HEAD)

.PHONY: format format-check lint lint-strict custom-checks skill-lint typecheck test check

format:            ## format changed ranges only (legacy lines untouched)
	$(GATE) --format

format-check:      ## fail if a changed range needs formatting
	$(GATE) --format-check

lint:              ## base tier: whole repo, or changed lines when LINT_SCOPE=changed
ifeq ($(LINT_SCOPE),changed)
	$(GATE) --base-tier
else
	$(RUFF) check .
endif

lint-strict:       ## strict tier on changed lines since the ratchet base
	$(GATE)

custom-checks:     ## comment rules and forbidden constructs on added lines
	$(PY) tools/quality/custom_checks.py --diff-base $(RATCHET_BASE)

skill-lint:        ## .claude skills, agents, and pointer docs (whole tree)
	$(PY) tools/quality/skill_lint.py

typecheck:         ## mypy on the in-scope packages
	$(PY) -m mypy $(TYPECHECK_PATHS)

test:
	$(PY) -m pytest -q $(TEST_PATHS)

check: format-check lint lint-strict custom-checks skill-lint typecheck test  ## everything CI runs
