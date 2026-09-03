"""Skill linter checks on a fixture ``.claude`` tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from skill_lint import STYLE_DOC, SkillLinter

GOOD_SKILL = (
    f"---\nname: demo\ndescription: A demo skill\n---\nRead `{STYLE_DOC}` before writing code.\n"
)


def make_tree(root: Path, skill_text: str = GOOD_SKILL) -> Path:
    """A minimal repo with one skill and the style doc."""
    (root / ".claude/skills/demo").mkdir(parents=True)
    (root / ".claude/skills/shared-refs").mkdir(parents=True)
    (root / STYLE_DOC).write_text("# style\n")
    (root / ".claude/skills/demo/SKILL.md").write_text(skill_text)
    return root


def messages(root: Path) -> list[str]:
    """Finding messages for ``root``."""
    return [f.message for f in SkillLinter(root).run()]


def test_well_formed_skill_passes(tmp_path: Path) -> None:
    assert messages(make_tree(tmp_path)) == []


def test_missing_frontmatter_is_a_finding(tmp_path: Path) -> None:
    assert messages(make_tree(tmp_path, "Just prose.\n")) == ["missing frontmatter block"]


def test_name_must_match_directory(tmp_path: Path) -> None:
    text = GOOD_SKILL.replace("name: demo", "name: other")
    assert any("!= directory" in m for m in messages(make_tree(tmp_path, text)))


def test_skill_without_style_pointer_is_a_finding(tmp_path: Path) -> None:
    text = "---\nname: demo\ndescription: d\n---\nWrite code.\n"
    assert any("required pre-read" in m for m in messages(make_tree(tmp_path, text)))


def test_non_code_skill_may_opt_out(tmp_path: Path) -> None:
    text = "---\nname: demo\ndescription: d\nwrites-code: false\n---\nOnly prose.\n"
    assert messages(make_tree(tmp_path, text)) == []


def test_dangling_path_in_a_skill_is_a_finding(tmp_path: Path) -> None:
    text = GOOD_SKILL + "Also see `src/missing_module.py`.\n"
    assert any("src/missing_module.py" in m for m in messages(make_tree(tmp_path, text)))


def test_index_and_directories_must_agree(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / ".claude/skills/README.md").write_text("- `demo` — demo\n- `ghost` — gone\n")
    found = messages(root)
    assert any("'ghost'" in m for m in found)
    (root / ".claude/skills/README.md").write_text("nothing listed\n")
    assert any("does not list skill 'demo'" in m for m in messages(root))


def test_bare_filename_resolves_anywhere_in_the_tree(tmp_path: Path) -> None:
    root = make_tree(tmp_path, GOOD_SKILL + "Run `server.py` from its directory.\n")
    (root / "chart_server").mkdir()
    (root / "chart_server/server.py").write_text("")
    assert messages(root) == []


def test_bare_filename_missing_everywhere_is_a_finding(tmp_path: Path) -> None:
    root = make_tree(tmp_path, GOOD_SKILL + "Run `ghost.py` first.\n")
    assert any("ghost.py" in m for m in messages(root))


def test_existence_follows_git_not_the_local_filesystem(tmp_path: Path) -> None:
    text = (
        GOOD_SKILL
        + "Secrets live in `pkg/.env`; the cache is `data/cache.db`; see `pkg/gone.py`.\n"
    )
    root = make_tree(tmp_path, text)
    (root / ".gitignore").write_text(".env\ndata/\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    found = messages(root)
    assert not any(".env" in m or "cache.db" in m for m in found)
    assert any("pkg/gone.py" in m for m in found)
