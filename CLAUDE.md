# Before you finish: the enforced gate

Project narrative, layout, and runbooks live in `docs/claude.md`. Style rules
live in `.claude/skills/shared-refs/code-style.md`; read it before writing code.
This file holds only the gate.

Every change, human or agent, passes these in order before it is done:

1. `make format` then `make lint` — formatter on your changed ranges, base tier green repo-wide.
2. `make lint-strict` — strict tier on the lines you added or changed. Zero findings.
3. `make typecheck` — type checker green.
4. `venv/bin/pre-commit run --all-files` — the whole hook suite green.

The `PostToolUse` hook in `.claude/settings.json` runs steps 1 and 2 on every
file you edit and feeds findings back to you. A session that fixes what the
hook reports cannot fail the commit-time gate.

Hard rules:

- **Do not weaken these configs to make code pass. Fix the code.** Every ignore
  in `ruff-strict.toml` carries a reason; adding one without a reason is a
  defect.
- **Never work around a refusal.** If the prose gate refuses a commit message,
  ticket, comment, or reply, rewrite it and post it through the same tool. Do
  not split it, do not switch tools, do not shorten it below the threshold.
- **Legacy stays legacy.** Never reformat or bulk-fix a file you were not asked
  to change. The strict tier is never run repo-wide.
- When the user says **"too technical"**, follow the protocol in
  `tools/quality/jargon.txt`: find the sentences, add their jargon to the list,
  tighten the rule if the miss was structural, show the checker now fails, then
  rewrite plainly and re-post. Never argue the miss.
