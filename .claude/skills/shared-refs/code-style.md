# Code style — the single written standard

This is the only style document in the repo. Skills, agent definitions, and
contributor guides point here and carry no style prose of their own. When this
file and another document disagree, this file wins; fix the other document.

Every rule is tagged:

- **[M]** mechanized — a gate fails it (ruff rule code or script name given).
- **[J]** judgement — a reviewer fails it. The goal is to move [J] rules to [M].

## Read these first

Open each exemplar this session, before writing any code, and match its idiom.
Prose below describes what these files already show; it never substitutes for them.

| File | What it teaches |
|---|---|
| `src/injuries.py` | Module docstring that states the data source and the scoring convention once. Named constants at top. Narrow `except` tuples. Guard clauses (`continue`/early `return`) instead of nested `if`. One-line docstrings on public functions. |
| `src/espn_client.py` | A small interface: three cached accessors, each raising a specific `RuntimeError` whose message says what to fix. No logic beyond the seam it owns. |
| `tests/test_snake_math.py` | One behaviour per test, named for the property it proves. Constants named at top. No network, no fixtures it does not need. |
| `tests/test_injuries.py` | Offline testing of a network module: a literal fake payload and `monkeypatch`, never a real request. |

Exemplars predate the gates, so a few lines in them fail strict rules that are
now enforced (bare `open`, `print`, a `sys.path` shim at the top of tests).
Where an exemplar and a mechanized rule disagree, the rule wins for new lines.

## Comments

**Default is no comment. [M: `custom_checks.py` narrative/block/banner; ruff ERA001]**
Failure prevented: comments that restate the code drift from it within a few
edits and then lie to the next reader.

- One line survives only for a non-obvious *why* that the names, docstring, and
  error messages do not already convey.
- A ticket key or plan reference anywhere on the line marks it as a *why* by
  convention (`# see docs/plans/02-injuries.md`).
- Root-cause narratives, incident history, and "this used to be X" go in the
  commit message and the plan doc, never in source.
- Tests are held to the same bar. `# no duplicate pick numbers` beside an assert
  is a narrative comment; name the test for the property instead.
- No section banners (`# ---- pull: network ----`). Structure comes from names
  and small functions.
- No legacy exemption inside a region you touch: moved code's old comments read
  as yours in the diff. Delete or rewrite them as you move them.
- Exempt: shebangs, coding cookies, `noqa`/`type: ignore` lines, `TODO`.

## Docstrings

**Every module, class, and function has one. [M: ruff D1xx]** One line when that
is enough. Failure prevented: an undocumented function forces the reader to
reconstruct intent from the body, and a comment gets added to do the job badly.

Say a thing once: the docstring covers intent, so no comment repeats it.
Google convention. Summary line ends without a period requirement; the rest is
prose, not a parameter table, unless a parameter is genuinely surprising.

## Types

**Full annotations on every signature, modern syntax only. [M: ruff ANN, UP]**
`str | None`, `list[dict]`, `from __future__ import annotations` where needed.
Failure prevented: an unannotated boundary hides the shape of the data crossing
it, and the type checker cannot help at exactly the seam where bugs live.

No blanket suppressions: `# type: ignore` and `noqa` carry a code and, unless
the code is self-explanatory, a reason. [M: ruff PGH003, RUF100]

## Shape

- **Guard clauses over nesting. [M: ruff C901 max 10, PLR0912, PLR0915]**
  Failure prevented: a three-deep `if` inside a loop is where the off-by-one
  and the missed `continue` hide.
- **Small single-purpose functions. [J]** If a function needs a comment to mark
  its phases, split it at those marks.
- **Keyword-only booleans. [M: ruff FBT001, FBT002]** No bare `True` at a call
  site: `refresh(force=True)`, never `refresh(True)`. Failure prevented: a
  positional flag is unreadable at the call site and silently wrong when a
  second flag is added.
- **No mutable default arguments. [M: ruff B006]**
- **Constants named at module top. [M: ruff PLR2004 in src, off in tests]**
  A literal in an expression needs a name unless it is 0, 1, or the point of a
  test. Failure prevented: the same threshold typed twice, changed once.
- **Object-oriented, used well. [J]** A class earns its place when data and
  behaviour share state; a one-off transform stays a function. Keep the public
  surface small, keep state private to the class that owns it, and prefer
  composition over inheritance (inherit only for a true is-a). Failure
  prevented: a bag of module-level functions passing the same six arguments
  around, or a five-level class tree nobody can change safely.
- **Modularity. [J]** One module owns one concern, and a module's public names
  are the ones another module actually imports. Failure prevented: the
  800-line file that every change has to touch.

## Errors, resources, output

- **Narrow, chained exceptions. [M: ruff BLE001, B904, TRY, EM]** Catch the
  types you can handle, `raise ... from exc` when translating, message text in a
  named variable. Failure prevented: a bare `except` swallows a `NameError`
  or `KeyError` from a typo and the caller sees a stale-data warning instead
  of the bug.
- **Resources via scope-managed constructs. [M: ruff SIM115]** `with` for
  files, sockets, and locks.
- **`pathlib`, not `open()`/`os.path`. [M: ruff PTH]**
- **Logging, not printing, in library code. [M: ruff T201]** CLI entry points
  under `if __name__ == "__main__":` and the `verify_*` helpers that exist to
  print are the exception and carry a per-file ignore with a reason.
- **Imports at the top. [M: ruff PLC0415, I001]** An in-function import is
  allowed only to break a real cycle or defer an optional heavy dependency, with
  a one-line why.
- **Timezone-aware datetimes. [M: ruff DTZ]** Sleeper and ESPN timestamps are
  epoch milliseconds; a naive `datetime` compared against them is a silent
  off-by-hours.

## Tests

- Tests never touch the network. Use a literal payload and `monkeypatch`
  (`tests/test_injuries.py`). [J, from `docs/plans/00-overview.md`]
- Tests never write to `data/board.json`, `data/udk/`, `data/notes/`, or
  `.env`. [J]
- Test names state the property proved. Literal expected values are the point
  of a test, so magic-number rules are off in `tests/`.

## Forbidden constructs (repo-specific)

This list is incident-driven and starts empty. Nothing on this machine has yet
failed a gate, because the gates did not exist. An entry is added when a
construct causes a real failure, with one line naming that failure, and its
check goes into `custom_checks.py` with an allowlist frozen at the time of
adoption. Existing violations are grandfathered there; nothing is ever added to
the allowlist to silence a new finding.

| Construct | Failure it caused | Since |
|---|---|---|
| _(none yet)_ | | |

## Adding or changing a rule

A rule states the failure it prevents. A rule with no failure story gets cut.
Change the rule here first, then the gate config, then the code. Never weaken a
gate config to make code pass.
