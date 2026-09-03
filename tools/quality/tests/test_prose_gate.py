"""Two-pass behaviour and mechanical checks of the plain-language gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from prose_gate import (
    EXIT_BLOCK,
    EXIT_OK,
    Extractor,
    Jargon,
    ProseChecker,
    WindowState,
    gate,
)

JARGON_HEAVY = (
    "Refactored the ratchet so merge-base resolution keys off the adoption "
    "hunk instead of the touch commit, which un-grandfathers 1722 findings "
    "across 14 files in 3 modules. The gate_changed_lines.py entrypoint now "
    "walks parse_added_ranges before the pre-commit stage so the linter's "
    "stdlib-only diff path and the hook path share one code path, and "
    "frontmatter in every SKILL.md is validated by skill_lint.py on --all-files."
)

PLAIN = (
    "The quality gate now measures new code against the commit that first "
    "added the strict config. Before this change it measured against any "
    "commit that edited the config. A later one-line edit therefore hid "
    "every earlier commit on the branch from the gate. The fix is small. "
    "One test covers it."
)


def checker(tmp_path: Path) -> ProseChecker:
    """A checker whose jargon list contains a few known terms."""
    words = tmp_path / "jargon.txt"
    words.write_text("# test list\nratchet\nmerge-base\nhunk\nlinter\nfrontmatter\n")
    return ProseChecker(Jargon(words))


def rules(findings: list) -> set[str]:
    """Rule names that fired."""
    return {f.rule for f in findings}


def test_jargon_heavy_paragraph_fails_the_mechanical_check(tmp_path: Path) -> None:
    fired = rules(checker(tmp_path).check(JARGON_HEAVY))
    assert {"long-sentence", "numbers", "identifier", "jargon"} <= fired


def test_plain_rewrite_passes(tmp_path: Path) -> None:
    assert checker(tmp_path).check(PLAIN) == []


def test_explained_jargon_is_not_a_finding(tmp_path: Path) -> None:
    text = "The ratchet, in other words a rule that only ever tightens, stays put."
    assert rules(checker(tmp_path).check(text)) == set()


def test_code_blocks_tables_and_urls_are_stripped(tmp_path: Path) -> None:
    text = (
        "Run this:\n\n```\nmake lint-strict && gate_changed_lines.py --hook\n```\n\n"
        "| col_a | col_b |\n|---|---|\n| 1 | 2 |\n\n"
        "> quoted_identifier here\n\nSee https://example.test/a_b_c for the write-up."
    )
    assert checker(tmp_path).check(text) == []


def test_ticket_keys_dates_and_percentages_are_not_numbers(tmp_path: Path) -> None:
    text = "ENG-142 shipped on 2026-09-03 at 7:30 with 12% fewer retries and PR #88."
    assert rules(checker(tmp_path).check(text)) == set()


def test_paragraph_addressed_to_an_agent_is_skipped(tmp_path: Path) -> None:
    text = "## Notes for the subagent\n\nrun gate_changed_lines.py then custom_checks.py on every hunk.\n"
    assert checker(tmp_path).check(text) == []


def test_first_attempt_is_refused_and_second_is_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = WindowState(tmp_path / "state.json")
    chk = checker(tmp_path)
    assert gate(JARGON_HEAVY, "ticket", state, chk, min_words=10) == EXIT_BLOCK
    assert "Rewrite" in capsys.readouterr().err
    assert gate(JARGON_HEAVY, "ticket", state, chk, min_words=10) == EXIT_BLOCK
    assert "unchanged" in capsys.readouterr().err
    assert gate(PLAIN, "ticket", state, chk, min_words=10) == EXIT_OK


def test_short_text_is_not_gated(tmp_path: Path) -> None:
    state = WindowState(tmp_path / "state.json")
    assert gate("fix typo", "git-commit", state, checker(tmp_path), min_words=25) == EXIT_OK


def test_commit_message_is_extracted_from_bash_command() -> None:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": 'git commit -m "first line" -m "second line"'},
    }
    extracted = Extractor.extract(payload)
    assert extracted is not None
    assert extracted.channel == "git-commit"
    assert extracted.texts == ["first line", "second line"]


def test_heredoc_commit_message_is_extracted() -> None:
    command = "git commit -F - <<'EOF'\nSubject line\n\nBody paragraph here.\nEOF"
    extracted = Extractor.extract({"tool_name": "Bash", "tool_input": {"command": command}})
    assert extracted is not None
    assert "Body paragraph here." in extracted.joined()


def test_non_commit_bash_and_read_only_tools_are_ignored() -> None:
    assert Extractor.extract({"tool_name": "Bash", "tool_input": {"command": "git status"}}) is None
    assert Extractor.extract({"tool_name": "mcp__x__search", "tool_input": {"text": "q"}}) is None


def test_outward_tool_prose_keys_are_walked() -> None:
    payload = {
        "tool_name": "mcp__jira__create_issue",
        "tool_input": {"fields": {"summary": "s", "description": "d"}},
    }
    extracted = Extractor.extract(payload)
    assert extracted is not None
    assert extracted.texts == ["s", "d"]


def test_commit_scope_prefix_and_trailers_are_not_prose(tmp_path: Path) -> None:
    text = (
        "data_streams: record every tick\n\nThe feed now writes each tick it receives.\n\n"
        "Co-Authored-By: Someone <someone@example.test>\n"
    )
    assert checker(tmp_path).check(text) == []
