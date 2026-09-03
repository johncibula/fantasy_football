#!/usr/bin/env python3
"""Strict-tier lint and format gate, ratcheted to the lines you changed.

Three entry modes:

* ``gate_changed_lines.py FILE...``  pre-commit: gate the named files.
* ``gate_changed_lines.py``          branch: gate every file changed since the
  ratchet base.
* ``gate_changed_lines.py --hook``   Claude Code PostToolUse: read the hook JSON
  from stdin, format the edited file's changed ranges, gate it, run the custom
  checks, and exit 2 with the findings on stderr so the model fixes them.

``--format`` rewrites changed ranges with the formatter instead of linting;
``--format-check`` reports ranges the formatter would change; ``--base-tier``
runs the whole-repo base config instead of the strict one, for repos whose
legacy tree is not yet base-clean.

The ratchet base is the merge-base with the default branch, advanced to the
commit that first added ``ruff-strict.toml`` so code written before the gate
existed is legacy by definition. Untracked files count as all-new.

Standard library only, apart from the ruff binary.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

STRICT_CONFIG_NAME = "ruff-strict.toml"
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_HOOK_BLOCK = 2
DEFAULT_BRANCH_FALLBACK = "main"


class GateEnvironmentError(RuntimeError):
    """The gate cannot run here (no git repo, no ruff); never a code finding."""


@dataclass(frozen=True)
class LineRange:
    """An inclusive 1-based range of lines."""

    start: int
    end: int

    def __contains__(self, line: object) -> bool:
        """Return whether ``line`` falls inside the range."""
        return isinstance(line, int) and self.start <= line <= self.end


@dataclass
class ChangedLines:
    """Lines added or modified per file, relative to the ratchet base."""

    ranges: dict[Path, list[LineRange]] = field(default_factory=dict)
    whole_files: set[Path] = field(default_factory=set)

    def files(self) -> list[Path]:
        """Every file with at least one changed line."""
        return sorted(set(self.ranges) | self.whole_files)

    def includes(self, path: Path, line: int) -> bool:
        """Return whether ``line`` of ``path`` was added or modified."""
        if path in self.whole_files:
            return True
        return any(line in rng for rng in self.ranges.get(path, []))

    def ranges_for(self, path: Path, total_lines: int) -> list[LineRange]:
        """Ranges to hand to the formatter, whole file when it is all new."""
        if path in self.whole_files:
            return [LineRange(1, max(total_lines, 1))]
        return sorted(self.ranges.get(path, []), key=lambda r: r.start)


class GitRepo:
    """Thin wrapper over the git commands the gate needs."""

    def __init__(self, cwd: Path | None = None) -> None:
        """Locate the repository containing ``cwd`` (default: current dir)."""
        try:
            top = self._run(["rev-parse", "--show-toplevel"], cwd=cwd or Path.cwd())
        except subprocess.CalledProcessError as exc:
            msg = "not inside a git repository"
            raise GateEnvironmentError(msg) from exc
        self.root = Path(top.strip())

    def _run(self, args: list[str], cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def _try(self, args: list[str]) -> str | None:
        try:
            return self._run(args).strip()
        except subprocess.CalledProcessError:
            return None

    def has_head(self) -> bool:
        """Return whether the repository has at least one commit."""
        return self._try(["rev-parse", "--verify", "HEAD"]) is not None

    def default_branch(self) -> str:
        """Name of the default branch, from origin/HEAD when it is set."""
        ref = self._try(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        if ref and "/" in ref:
            return ref.split("/", 1)[1]
        return DEFAULT_BRANCH_FALLBACK

    def merge_base_with_default(self) -> str | None:
        """Merge-base with the first of origin/<default>, <default>, main."""
        default = self.default_branch()
        candidates = [f"origin/{default}", default, DEFAULT_BRANCH_FALLBACK]
        for ref in dict.fromkeys(candidates):
            base = self._try(["merge-base", "HEAD", ref])
            if base:
                return base
        return None

    def commit_that_added(self, relative_path: str) -> str | None:
        """The commit that first ADDED ``relative_path``, or None.

        Keyed off the add, never a later touch: a one-line edit to the config
        must not move the ratchet and exempt everything before it.
        """
        out = self._try(["log", "--diff-filter=A", "--format=%H", "--", relative_path])
        if not out:
            return None
        return out.splitlines()[-1]

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Return whether ``ancestor`` is reachable from ``descendant``."""
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.root,
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    def ratchet_base(self) -> str | None:
        """Commit to diff against; None when the repo has no commits yet."""
        if not self.has_head():
            return None
        base = self.merge_base_with_default() or "HEAD"
        adoption = self.commit_that_added(STRICT_CONFIG_NAME)
        if adoption and self.is_ancestor(base, adoption) and self.is_ancestor(adoption, "HEAD"):
            return adoption
        return base

    def untracked_files(self) -> set[Path]:
        """Untracked, non-ignored files as absolute paths."""
        out = self._try(["ls-files", "--others", "--exclude-standard"]) or ""
        return {self.root / line for line in out.splitlines() if line}

    def tracked_files(self) -> set[Path]:
        """Tracked files as absolute paths."""
        out = self._try(["ls-files"]) or ""
        return {self.root / line for line in out.splitlines() if line}

    def diff_unified0(self, base: str, paths: list[Path] | None = None) -> str:
        """Zero-context diff of the working tree against ``base``."""
        args = ["diff", "-U0", "--no-color", "--no-ext-diff", base]
        if paths:
            args += ["--", *[str(p) for p in paths]]
        return self._try(args) or ""

    def changed_files_since(self, base: str) -> set[Path]:
        """Files added or modified in the working tree relative to ``base``."""
        out = self._try(["diff", "--name-only", "--diff-filter=AM", base]) or ""
        return {self.root / line for line in out.splitlines() if line}


def parse_added_ranges(diff_text: str, root: Path) -> dict[Path, list[LineRange]]:
    """Map each file in a ``-U0`` diff to the line ranges its hunks added."""
    ranges: dict[Path, list[LineRange]] = {}
    current: Path | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else root / target.removeprefix("b/")
            continue
        match = HUNK_HEADER.match(line)
        if not match or current is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            continue
        ranges.setdefault(current, []).append(LineRange(start, start + count - 1))
    return ranges


def is_python(path: Path) -> bool:
    """Return whether the gate should look at ``path``."""
    return path.suffix == ".py" and path.is_file()


class ChangeCollector:
    """Compute the changed-line set for the requested scope."""

    def __init__(self, repo: GitRepo) -> None:
        """Bind to ``repo`` and resolve its ratchet base once."""
        self.repo = repo
        self.base = repo.ratchet_base()

    def collect(self, only: list[Path] | None = None) -> ChangedLines:
        """Changed lines for ``only`` (absolute paths) or the whole branch."""
        changed = ChangedLines()
        untracked = self.repo.untracked_files()
        if self.base is None:
            candidates = set(only) if only else untracked | self.repo.tracked_files()
            changed.whole_files = {p for p in candidates if is_python(p)}
            return changed
        scope = set(only) if only else self.repo.changed_files_since(self.base) | untracked
        for path in scope:
            if not is_python(path):
                continue
            if path in untracked:
                changed.whole_files.add(path)
        tracked_scope = [p for p in scope if is_python(p) and p not in untracked]
        if tracked_scope:
            diff = self.repo.diff_unified0(self.base, tracked_scope)
            changed.ranges = parse_added_ranges(diff, self.repo.root)
        return changed


class Ruff:
    """Locate and invoke the ruff binary."""

    def __init__(self, root: Path, *, base_tier: bool = False) -> None:
        """Find ruff in the repo's venv, then on PATH; raise if absent."""
        self.root = root
        self.binary = self._locate()
        self.config: Path | None = None if base_tier else root / STRICT_CONFIG_NAME
        if self.config is not None and not self.config.exists():
            msg = f"{STRICT_CONFIG_NAME} not found at {root}"
            raise GateEnvironmentError(msg)

    def _locate(self) -> Path:
        for candidate in (self.root / ".venv/bin/ruff", self.root / "venv/bin/ruff"):
            if candidate.exists():
                return candidate
        found = shutil.which("ruff")
        if found:
            return Path(found)
        msg = "ruff binary not found (install it in the repo venv)"
        raise GateEnvironmentError(msg)

    def check(self, files: list[Path]) -> list[dict]:
        """Strict-tier findings for ``files`` as ruff's JSON records."""
        if not files:
            return []
        config = ["--config", str(self.config)] if self.config else []
        result = subprocess.run(
            [
                str(self.binary),
                "check",
                *config,
                "--output-format",
                "json",
                "--no-fix",
                "--exit-zero",
                *[str(f) for f in files],
            ],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = f"ruff failed: {result.stderr.strip()}"
            raise GateEnvironmentError(msg)
        return json.loads(result.stdout or "[]")

    def format_range(self, path: Path, rng: LineRange, *, check: bool) -> bool:
        """Format one range; with ``check`` only report. Returns True if clean."""
        # "start:end" would read as line:column and reformat to end of file.
        args = [str(self.binary), "format", "--range", f"{rng.start}-{rng.end}"]
        if check:
            args.append("--check")
        args.append(str(path))
        result = subprocess.run(args, cwd=self.root, check=False, capture_output=True, text=True)
        if result.returncode > 1:
            msg = f"ruff format failed: {result.stderr.strip()}"
            raise GateEnvironmentError(msg)
        return result.returncode == 0


@dataclass(frozen=True)
class Finding:
    """One gate finding, printed as ``path:line:col: CODE message``."""

    path: Path
    line: int
    column: int
    code: str
    message: str

    def render(self, root: Path) -> str:
        """Human-readable one-liner with a repo-relative path."""
        rel = self.path.relative_to(root) if self.path.is_relative_to(root) else self.path
        return f"{rel}:{self.line}:{self.column}: {self.code} {self.message}"


class Gate:
    """Run the strict tier and the formatter over changed lines only."""

    def __init__(self, repo: GitRepo, ruff: Ruff) -> None:
        """Bind the gate to a repository and a ruff binary."""
        self.repo = repo
        self.ruff = ruff

    def lint(self, changed: ChangedLines) -> list[Finding]:
        """Strict findings restricted to changed lines."""
        findings = []
        for record in self.ruff.check(changed.files()):
            path = Path(record["filename"])
            line = record["location"]["row"]
            if not changed.includes(path, line):
                continue
            findings.append(
                Finding(path, line, record["location"]["column"], record["code"], record["message"])
            )
        return sorted(findings, key=lambda f: (str(f.path), f.line, f.column))

    def format(self, changed: ChangedLines, *, check: bool) -> list[Finding]:
        """Format (or check) changed ranges, bottom-up so numbering holds."""
        findings = []
        for path in changed.files():
            total = len(path.read_text(encoding="utf-8").splitlines())
            for rng in reversed(changed.ranges_for(path, total)):
                if not self.ruff.format_range(path, rng, check=check):
                    findings.append(Finding(path, rng.start, 1, "FORMAT", "range needs formatting"))
        return findings


def run_custom_checks(root: Path, files: list[Path], base: str | None) -> list[str]:
    """Run tools/quality/custom_checks.py scoped to ``files``; return its lines."""
    script = Path(__file__).with_name("custom_checks.py")
    if not script.exists():
        return []
    args = [sys.executable, str(script)]
    args += ["--diff-base", base] if base else ["--all-new"]
    args += ["--", *[str(f) for f in files]]
    result = subprocess.run(args, cwd=root, check=False, capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def resolve_paths(repo: GitRepo, names: list[str]) -> list[Path]:
    """Absolute paths for ``names``, relative names resolved against cwd."""
    return [
        Path(name).resolve() for name in names if Path(name).resolve().is_relative_to(repo.root)
    ]


def run_hook(stdin_text: str) -> int:
    """PostToolUse mode: format, lint, and custom-check the edited file."""
    try:
        payload = json.loads(stdin_text or "{}")
    except json.JSONDecodeError:
        return EXIT_OK
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or not Path(file_path).exists() or not is_python(Path(file_path)):
        return EXIT_OK
    repo = GitRepo(Path(file_path).parent)
    ruff = Ruff(repo.root)
    gate = Gate(repo, ruff)
    collector = ChangeCollector(repo)
    target = Path(file_path).resolve()
    gate.format(collector.collect([target]), check=False)
    changed = collector.collect([target])
    lines = [f.render(repo.root) for f in gate.lint(changed)]
    lines += run_custom_checks(repo.root, [target], collector.base)
    if not lines:
        return EXIT_OK
    sys.stderr.write(
        "Quality gate: fix these findings on the lines you just wrote "
        "(rules: .claude/skills/shared-refs/code-style.md). Do not weaken the config.\n"
    )
    sys.stderr.write("\n".join(lines) + "\n")
    return EXIT_HOOK_BLOCK


def run_cli(args: argparse.Namespace) -> int:
    """Pre-commit and branch modes."""
    repo = GitRepo()
    ruff = Ruff(repo.root, base_tier=args.base_tier)
    gate = Gate(repo, ruff)
    collector = ChangeCollector(repo)
    only = resolve_paths(repo, args.files) if args.files else None
    changed = collector.collect(only)
    if args.format:
        findings = gate.format(changed, check=False)
    elif args.format_check:
        findings = gate.format(changed, check=True)
    else:
        findings = gate.lint(changed)
    for finding in findings:
        print(finding.render(repo.root))
    if findings:
        print(f"{len(findings)} finding(s) on changed lines (base: {collector.base or 'none'})")
        return EXIT_FINDINGS
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """CLI definition."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to gate (default: whole branch)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--hook", action="store_true", help="Claude Code PostToolUse mode")
    mode.add_argument("--format", action="store_true", help="format changed ranges")
    mode.add_argument("--format-check", action="store_true", help="check changed ranges")
    parser.add_argument(
        "--base-tier", action="store_true", help="lint with the base config instead of strict"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point; environment problems warn and never block."""
    args = build_parser().parse_args(argv)
    try:
        if args.hook:
            return run_hook(sys.stdin.read())
        return run_cli(args)
    except GateEnvironmentError as exc:
        sys.stderr.write(f"quality gate skipped: {exc}\n")
        return EXIT_OK if args.hook else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
