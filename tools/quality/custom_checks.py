#!/usr/bin/env python3
"""House rules the linter cannot express, checked on ADDED lines only.

Comment rules (Python ``#`` comments):

* block     three or more consecutive added comment lines
* narrative an added comment that narrates WHAT the code does
* banner    an added section banner such as ``# ==== helpers ====``

A ticket key, plan-doc path, PR number, or URL anywhere on the line marks it as
a *why* by convention and exempts it. Shebangs, coding cookies, lint
suppressions, and TODO/FIXME lines are exempt from all three.

Forbidden constructs are the repo's incident-driven list; it starts empty. The
allowlist below is frozen at adoption: pre-existing violations are
grandfathered and nothing new is ever added to silence a finding.

Diff source: ``git diff --cached`` (pre-commit), falling back to ``git diff
HEAD``; ``--diff-base REF`` for CI, which has nothing staged and wants the
whole branch; ``--all-new`` when the repo has no commits yet. Untracked files
count as all-new in every mode except ``--cached``.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_FINDINGS = 1
BLOCK_THRESHOLD = 3

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
WHOLE_LINE_COMMENT = re.compile(r"^\s*#(?P<text>.*)$")
TRAILING_COMMENT = re.compile(
    r"^(?P<code>[^#'\"]*(?:(?:'[^']*'|\"[^\"]*\")[^#'\"]*)*)#(?P<text>.*)$"
)
BANNER = re.compile(r"^\s*#\s*(?:[-=*#~_]\s*){4,}(?:[\w /]*(?:[-=*#~_]\s*){2,})?\s*$")
WHY_MARKER = re.compile(
    r"\b[A-Z][A-Z0-9]+-\d+\b"  # ticket key such as ENG-123
    r"|#\d+\b"  # pull request or issue number
    r"|https?://"  # link
    r"|\bdocs?/[\w./-]+"  # plan or design doc path
)
EXEMPT = re.compile(
    r"^\s*#!"  # shebang
    r"|^\s*#.*coding[:=]"  # coding cookie
    r"|\bnoqa\b|\btype:\s*ignore\b|\bpragma\b|\bfmt:\s*(off|on|skip)\b|\bruff:\b"
    r"|\bTODO\b|\bFIXME\b"
)
NARRATIVE_OPENERS = (
    "check",
    "checks",
    "checking",
    "now",
    "then",
    "next",
    "first",
    "finally",
    "loop",
    "loops",
    "iterate",
    "iterates",
    "this function",
    "this method",
    "this class",
    "this module",
    "this file",
    "here we",
    "we now",
    "we then",
    "we just",
    "get",
    "gets",
    "set",
    "sets",
    "return",
    "returns",
    "call",
    "calls",
    "create",
    "creates",
    "initialise",
    "initialize",
    "compute",
    "computes",
    "calculate",
    "calculates",
    "increment",
    "append",
    "appends",
    "add",
    "adds",
    "remove",
    "removes",
    "update",
    "updates",
    "load",
    "loads",
    "save",
    "saves",
    "print",
    "prints",
    "convert",
    "converts",
    "parse",
    "parses",
    "build",
    "builds",
    "open",
    "opens",
    "close",
    "closes",
    "read",
    "reads",
    "write",
    "writes",
    "make",
    "makes",
    "start",
    "starts",
    "stop",
    "stops",
    "define",
    "defines",
    "import",
    "imports",
    "if the",
    "else",
    "otherwise",
)
NARRATIVE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(w) for w in NARRATIVE_OPENERS) + r")\b", re.IGNORECASE
)


@dataclass(frozen=True)
class ForbiddenConstruct:
    """A repo-specific construct that once caused a real failure."""

    name: str
    pattern: re.Pattern[str]
    reason: str


# Incident-driven; starts empty. Add an entry only with the failure it caused,
# and record the same entry in .claude/skills/shared-refs/code-style.md.
FORBIDDEN: tuple[ForbiddenConstruct, ...] = ()

# Frozen at adoption: construct name -> repo-relative paths that already
# violated it. Never add to this to silence a new finding.
ALLOWLIST_FROZEN: dict[str, frozenset[str]] = {}


@dataclass(frozen=True)
class AddedLine:
    """One added line of one file."""

    path: Path
    number: int
    text: str


@dataclass(frozen=True)
class Finding:
    """One custom-check finding."""

    path: Path
    line: int
    rule: str
    message: str

    def render(self, root: Path) -> str:
        """Human-readable one-liner with a repo-relative path."""
        rel = self.path.relative_to(root) if self.path.is_relative_to(root) else self.path
        return f"{rel}:{self.line}: {self.rule}: {self.message}"


class DiffSource:
    """Collect added lines from git for the chosen mode."""

    def __init__(self, root: Path) -> None:
        """Bind to the repository at ``root``."""
        self.root = root

    def _git(self, args: list[str]) -> str | None:
        result = subprocess.run(
            ["git", *args], cwd=self.root, check=False, capture_output=True, text=True
        )
        return result.stdout if result.returncode == 0 else None

    def untracked(self) -> set[Path]:
        """Untracked, non-ignored files."""
        out = self._git(["ls-files", "--others", "--exclude-standard"]) or ""
        return {self.root / line for line in out.splitlines() if line}

    def diff_text(self, mode: str, base: str | None, files: list[Path]) -> str | None:
        """Zero-context diff for ``mode`` (cached, head, base) or None."""
        args = ["diff", "-U0", "--no-color", "--no-ext-diff"]
        if mode == "cached":
            args.append("--cached")
        elif mode == "base":
            args.append(base or "HEAD")
        else:
            args.append("HEAD")
        if files:
            args += ["--", *[str(f) for f in files]]
        return self._git(args)

    def added_lines(self, mode: str, base: str | None, files: list[Path]) -> list[AddedLine]:
        """Added lines for the mode, plus whole untracked files when apt."""
        lines: list[AddedLine] = []
        if mode != "all-new":
            diff = self.diff_text(mode, base, files)
            if diff is None and mode == "cached":
                diff = self.diff_text("head", None, files)
            lines.extend(self._parse(diff or ""))
        if mode != "cached":
            lines.extend(self._whole_files(files))
        return lines

    def _whole_files(self, files: list[Path]) -> list[AddedLine]:
        candidates = self.untracked() if not files else {f for f in files if f in self.untracked()}
        out: list[AddedLine] = []
        for path in sorted(candidates):
            if path.suffix != ".py" or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            out.extend(AddedLine(path, i, t) for i, t in enumerate(text.splitlines(), 1))
        return out

    def _parse(self, diff: str) -> list[AddedLine]:
        lines: list[AddedLine] = []
        current: Path | None = None
        number = 0
        for raw in diff.splitlines():
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                current = None if target == "/dev/null" else self.root / target.removeprefix("b/")
                continue
            match = HUNK_HEADER.match(raw)
            if match:
                number = int(match.group(1))
                continue
            if current is None or current.suffix != ".py":
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                lines.append(AddedLine(current, number, raw[1:]))
                number += 1
        return lines


class CommentRules:
    """Block, narrative, and banner checks over added lines."""

    @staticmethod
    def comment_text(line: str) -> str | None:
        """The comment body of ``line`` or None when it has no comment."""
        whole = WHOLE_LINE_COMMENT.match(line)
        if whole:
            return whole.group("text")
        trailing = TRAILING_COMMENT.match(line)
        return trailing.group("text") if trailing else None

    @staticmethod
    def is_exempt(line: str) -> bool:
        """Shebangs, cookies, suppressions, TODOs, and keyed why-lines."""
        return bool(EXEMPT.search(line) or WHY_MARKER.search(line))

    def check(self, added: list[AddedLine]) -> list[Finding]:
        """All comment findings for ``added``."""
        findings = self._narrative_and_banner(added)
        findings.extend(self._blocks(added))
        return findings

    def _narrative_and_banner(self, added: list[AddedLine]) -> list[Finding]:
        findings = []
        for line in added:
            text = self.comment_text(line.text)
            if text is None or self.is_exempt(line.text):
                continue
            if BANNER.match(line.text):
                findings.append(
                    Finding(
                        line.path,
                        line.number,
                        "banner",
                        "section banner; structure comes from names and small functions",
                    )
                )
            elif NARRATIVE.match(text):
                findings.append(
                    Finding(
                        line.path,
                        line.number,
                        "narrative",
                        f"comment narrates what the code does: {text.strip()!r}; delete it or state the why",
                    )
                )
        return findings

    def _blocks(self, added: list[AddedLine]) -> list[Finding]:
        findings = []
        run_start: AddedLine | None = None
        run_len = 0
        previous: AddedLine | None = None
        for line in added:
            consecutive = (
                previous is not None
                and previous.path == line.path
                and previous.number == line.number - 1
            )
            is_comment = WHOLE_LINE_COMMENT.match(line.text) is not None and not self.is_exempt(
                line.text
            )
            if is_comment and consecutive and run_start is not None:
                run_len += 1
            elif is_comment:
                run_start, run_len = line, 1
            else:
                run_start, run_len = None, 0
            if run_len == BLOCK_THRESHOLD and run_start is not None:
                findings.append(
                    Finding(
                        run_start.path,
                        run_start.number,
                        "block",
                        f"{BLOCK_THRESHOLD}+ consecutive comment lines; move the narrative to the commit message or a docstring",
                    )
                )
            previous = line
        return findings


class ForbiddenRules:
    """Repo-specific forbidden constructs with a frozen allowlist."""

    def __init__(self, root: Path) -> None:
        """Bind to ``root`` so allowlist paths can be compared."""
        self.root = root

    def check(self, added: list[AddedLine]) -> list[Finding]:
        """Findings for every forbidden construct on an added line."""
        findings = []
        for line in added:
            rel = (
                str(line.path.relative_to(self.root))
                if line.path.is_relative_to(self.root)
                else str(line.path)
            )
            for construct in FORBIDDEN:
                if rel in ALLOWLIST_FROZEN.get(construct.name, frozenset()):
                    continue
                if construct.pattern.search(line.text):
                    findings.append(
                        Finding(line.path, line.number, construct.name, construct.reason)
                    )
        return findings


def repo_root() -> Path | None:
    """Top-level directory of the current git repository, or None."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], check=False, capture_output=True, text=True
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def build_parser() -> argparse.ArgumentParser:
    """CLI definition."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="limit to these files")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--diff-base", metavar="REF", help="diff against REF (CI)")
    mode.add_argument("--cached", action="store_true", help="staged changes only (pre-commit)")
    mode.add_argument("--all-new", action="store_true", help="treat every named file as added")
    return parser


def select_mode(args: argparse.Namespace) -> tuple[str, str | None]:
    """Translate CLI flags into (mode, base)."""
    if args.all_new:
        return "all-new", None
    if args.diff_base:
        return "base", args.diff_base
    if args.cached:
        return "cached", None
    return "cached", None


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    root = repo_root()
    if root is None:
        sys.stderr.write("custom checks skipped: not a git repository\n")
        return EXIT_OK
    files = [Path(f).resolve() for f in args.files]
    mode, base = select_mode(args)
    added = DiffSource(root).added_lines(mode, base, files)
    if mode == "all-new" and files:
        added = [
            AddedLine(p, i, t)
            for p in files
            if p.suffix == ".py" and p.is_file()
            for i, t in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        ]
    findings = CommentRules().check(added) + ForbiddenRules(root).check(added)
    for finding in sorted(findings, key=lambda f: (str(f.path), f.line)):
        print(finding.render(root))
    return EXIT_FINDINGS if findings else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
