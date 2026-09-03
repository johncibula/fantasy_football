"""Ratchet-base and changed-line behaviour of the strict gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import gate_changed_lines as gcl
import pytest
from gate_changed_lines import (
    STRICT_CONFIG_NAME,
    ChangeCollector,
    GitRepo,
    LineRange,
    parse_added_ranges,
)


class Repo:
    """A throwaway git repository with helpers for building history."""

    def __init__(self, root: Path) -> None:
        """Initialise an empty repository on ``main`` under ``root``."""
        self.root = root
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args: str) -> str:
        """Run git in the repo and return stdout."""
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, name: str, text: str) -> Path:
        """Write ``text`` to ``name`` relative to the root."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, message: str) -> str:
        """Stage everything and commit; return the full hash."""
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A repo with one legacy commit on main."""
    r = Repo(tmp_path)
    r.write("pkg/legacy.py", "def f(a, b):\n    return a\n")
    r.commit("legacy baseline")
    return r


def test_base_is_merge_base_with_main_when_no_strict_config(repo: Repo) -> None:
    main_tip = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-qb", "feature")
    repo.write("pkg/new.py", "x = 1\n")
    repo.commit("feature work")
    assert GitRepo(repo.root).ratchet_base() == main_tip


def test_adoption_branch_advances_base_to_the_commit_that_adds_the_config(repo: Repo) -> None:
    repo.git("checkout", "-qb", "adopt-gate")
    repo.write("pkg/before.py", "before = 1\n")
    repo.commit("work before the gate existed")
    repo.write(STRICT_CONFIG_NAME, "[lint]\nselect = ['D']\n")
    adoption = repo.commit("add strict tier")
    repo.write("pkg/after.py", "after = 1\n")
    repo.commit("work after the gate")
    assert GitRepo(repo.root).ratchet_base() == adoption


def test_a_later_touch_of_the_config_does_not_move_the_base(repo: Repo) -> None:
    repo.git("checkout", "-qb", "adopt-gate")
    repo.write(STRICT_CONFIG_NAME, "[lint]\nselect = ['D']\n")
    adoption = repo.commit("add strict tier")
    repo.write("pkg/after.py", "after = 1\n")
    repo.commit("work after the gate")
    repo.write(STRICT_CONFIG_NAME, "[lint]\nselect = ['D', 'ANN']\n")
    touch = repo.commit("one-line config edit")
    base = GitRepo(repo.root).ratchet_base()
    assert base == adoption
    assert base != touch


def test_config_added_before_the_merge_base_leaves_the_base_alone(repo: Repo) -> None:
    repo.write(STRICT_CONFIG_NAME, "[lint]\nselect = ['D']\n")
    repo.commit("add strict tier on main")
    main_tip = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-qb", "feature")
    repo.write("pkg/new.py", "x = 1\n")
    repo.commit("feature work")
    assert GitRepo(repo.root).ratchet_base() == main_tip


def test_repo_without_commits_has_no_base_and_treats_files_as_new(tmp_path: Path) -> None:
    r = Repo(tmp_path)
    r.write("pkg/fresh.py", "x = 1\n")
    git_repo = GitRepo(r.root)
    assert git_repo.ratchet_base() is None
    changed = ChangeCollector(git_repo).collect()
    assert changed.whole_files == {r.root / "pkg/fresh.py"}


def test_untracked_file_counts_as_all_new(repo: Repo) -> None:
    path = repo.write("pkg/untracked.py", "x = 1\ny = 2\n")
    changed = ChangeCollector(GitRepo(repo.root)).collect()
    assert path in changed.whole_files
    assert changed.includes(path, 2)


def test_only_modified_lines_of_a_tracked_file_are_in_scope(repo: Repo) -> None:
    path = repo.write("pkg/legacy.py", "def f(a, b):\n    return b\n\n\nnew = 1\n")
    changed = ChangeCollector(GitRepo(repo.root)).collect()
    assert changed.includes(path, 2)
    assert changed.includes(path, 5)
    assert not changed.includes(path, 1)


def test_parse_added_ranges_reads_hunk_headers(tmp_path: Path) -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,0 +2,3 @@\n+a\n+b\n+c\n@@ -9 +12 @@\n+d\n@@ -20,2 +23,0 @@\n-gone\n-gone\n"
    ranges = parse_added_ranges(diff, tmp_path)
    assert ranges == {tmp_path / "x.py": [LineRange(2, 4), LineRange(12, 12)]}


def test_format_range_uses_line_to_line_syntax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A colon would mean line:column and format to the end of the file."""
    seen: list[list[str]] = []

    class Done:
        returncode = 0
        stderr = ""

    def fake_run(args: list[str], **_: object) -> Done:
        seen.append(args)
        return Done()

    monkeypatch.setattr(gcl.subprocess, "run", fake_run)
    monkeypatch.setattr(gcl.Ruff, "_locate", lambda _self: tmp_path / "ruff")
    (tmp_path / STRICT_CONFIG_NAME).write_text("")
    gcl.Ruff(tmp_path).format_range(tmp_path / "x.py", LineRange(108, 110), check=True)
    assert "108-110" in seen[0]
    assert not any(":" in a for a in seen[0] if a.startswith("1"))
