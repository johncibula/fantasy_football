#!/usr/bin/env python3
"""Lint the agent-facing files under ``.claude/``: skills, agents, commands.

Checks, whole-tree and offline:

* every ``SKILL.md`` has frontmatter with ``name`` and ``description``, and
  ``name`` matches its directory;
* every skill names the style doc as a required pre-read, so no skill grows
  its own style prose;
* every repo path a skill, agent, or ``CLAUDE.md`` names in backticks still
  exists, so moving a file cannot silently orphan a pointer;
* the skill index (``.claude/skills/README.md``, when present) and the skill
  directories agree in both directions.

Run whole-tree: deleting a skill directory or moving a referenced file breaks a
file that is not itself in the commit. Standard library only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_FINDINGS = 1
STYLE_DOC = ".claude/skills/shared-refs/code-style.md"
SKILL_INDEX = ".claude/skills/README.md"
FRONTMATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
BACKTICK_PATH = re.compile(
    r"`(?P<path>(?:\.?[\w-]+/)+[\w.-]*|[\w-]+\.(?:md|py|toml|yaml|yml|json|txt|cfg|ini))`"
)
INDEX_ENTRY = re.compile(r"`(?P<name>[\w-]+)/?`|\[(?P<link>[\w-]+)\]\((?P<target>[\w./-]+)\)")
PLACEHOLDER = re.compile(r"[<>{}*]")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".mypy_cache"}


@dataclass(frozen=True)
class Finding:
    """One skill-lint finding."""

    path: Path
    message: str

    def render(self, root: Path) -> str:
        """Human-readable one-liner with a repo-relative path."""
        return f"{self.path.relative_to(root)}: {self.message}"


class Frontmatter:
    """Minimal ``key: value`` frontmatter reader; no YAML dependency."""

    def __init__(self, text: str) -> None:
        """Parse the leading ``---`` block of ``text`` into ``self.fields``."""
        self.fields: dict[str, str] = {}
        match = FRONTMATTER.match(text)
        self.present = match is not None
        if match is None:
            return
        for line in match.group("body").splitlines():
            if ":" not in line or line.startswith((" ", "\t", "-")):
                continue
            key, _, value = line.partition(":")
            self.fields[key.strip()] = value.strip().strip("'\"")


class SkillLinter:
    """Run every check over the ``.claude`` tree of one repo."""

    def __init__(self, root: Path) -> None:
        """Bind to the repository at ``root``."""
        self.root = root
        self.claude_dir = root / ".claude"
        self.findings: list[Finding] = []

    def run(self) -> list[Finding]:
        """All findings, in path order."""
        for skill in sorted(self.claude_dir.glob("skills/*/SKILL.md")):
            self._check_skill(skill)
        for doc in self._pointer_docs():
            self._check_paths(doc)
        self._check_index()
        return sorted(self.findings, key=lambda f: str(f.path))

    def _pointer_docs(self) -> list[Path]:
        docs = [p for p in self.claude_dir.rglob("*.md") if p.is_file()]
        for name in ("CLAUDE.md", "docs/claude.md"):
            candidate = self.root / name
            if candidate.exists():
                docs.append(candidate)
        return sorted(docs)

    def _check_skill(self, skill: Path) -> None:
        text = skill.read_text(encoding="utf-8")
        meta = Frontmatter(text)
        if not meta.present:
            self.findings.append(Finding(skill, "missing frontmatter block"))
            return
        for key in ("name", "description"):
            if not meta.fields.get(key):
                self.findings.append(Finding(skill, f"frontmatter lacks '{key}'"))
        expected = skill.parent.name
        if meta.fields.get("name") and meta.fields["name"] != expected:
            self.findings.append(
                Finding(
                    skill, f"frontmatter name {meta.fields['name']!r} != directory {expected!r}"
                )
            )
        if meta.fields.get("writes-code", "true").lower() != "false" and STYLE_DOC not in text:
            self.findings.append(
                Finding(
                    skill,
                    f"must name {STYLE_DOC} as a required pre-read (or set 'writes-code: false')",
                )
            )

    def _check_paths(self, doc: Path) -> None:
        text = doc.read_text(encoding="utf-8")
        for match in BACKTICK_PATH.finditer(text):
            candidate = match.group("path")
            if PLACEHOLDER.search(candidate) or candidate.startswith(("http", "~", "/")):
                continue
            if not self._resolves(candidate):
                self.findings.append(Finding(doc, f"names `{candidate}` which does not exist"))

    def _resolves(self, candidate: str) -> bool:
        """Repo-root paths must exist there; a bare filename may live anywhere.

        Existence is judged against git's view, never the bare filesystem,
        so the verdict is the same on every clone and in CI: a path counts
        when git tracks it or would add it, or when the committed ignore
        rules name it (a runtime artifact such as a data directory or a
        secrets file is a deliberate reference, not a dangling one).
        Subsystems here are run from inside their own directory, so prose
        naming ``server.py`` is shorthand, not a root path; it is a finding
        only once no file of that name exists anywhere in the tree.
        """
        if "/" in candidate:
            return candidate.rstrip("/") in self._paths or self._ignored(candidate)
        return candidate in self._filenames or self._ignored(candidate)

    def _ignored(self, candidate: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", candidate],
            cwd=self.root,
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    @property
    def _paths(self) -> set[str]:
        if not hasattr(self, "_path_cache"):
            self._path_cache = self._git_paths()
        return self._path_cache

    @property
    def _filenames(self) -> set[str]:
        return {Path(p).name for p in self._paths}

    def _git_paths(self) -> set[str]:
        """Repo-relative files and every directory above them, per git."""
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            files = [
                str(p.relative_to(self.root))
                for p in self.root.rglob("*")
                if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
            ]
        else:
            files = [line for line in result.stdout.splitlines() if line]
        paths: set[str] = set()
        for file in files:
            paths.add(file)
            paths.update(str(parent) for parent in Path(file).parents if str(parent) != ".")
        return paths

    def _check_index(self) -> None:
        index = self.root / SKILL_INDEX
        if not index.exists():
            return
        listed = self._index_names(index.read_text(encoding="utf-8"))
        present = {
            p.name for p in (self.claude_dir / "skills").iterdir() if (p / "SKILL.md").exists()
        }
        for name in sorted(listed - present):
            self.findings.append(Finding(index, f"lists skill {name!r} which has no SKILL.md"))
        for name in sorted(present - listed):
            self.findings.append(Finding(index, f"does not list skill {name!r}"))

    @staticmethod
    def _index_names(text: str) -> set[str]:
        names: set[str] = set()
        for match in INDEX_ENTRY.finditer(text):
            if match.group("name"):
                names.add(match.group("name"))
            elif match.group("target"):
                names.add(Path(match.group("target")).parts[0])
        return names


def main(argv: list[str] | None = None) -> int:
    """Entry point: lint the repo containing the current directory."""
    root = Path(argv[0]).resolve() if argv else Path.cwd().resolve()
    if not (root / ".claude").exists():
        print(f"skill lint: no .claude directory under {root}")
        return EXIT_OK
    findings = SkillLinter(root).run()
    for finding in findings:
        print(finding.render(root))
    return EXIT_FINDINGS if findings else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
