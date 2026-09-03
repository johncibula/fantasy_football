#!/usr/bin/env python3
"""Plain-language gate for outward writing.

Two hook entry points:

* ``--pre-tool-use``  PreToolUse on tools that post outward (issue tracker,
  wiki, chat, mail, PR comments) and on ``git commit`` via the Bash tool. The
  prose is pulled from known keys of the tool input, or from the commit
  message in the command string.
* ``--stop``          Stop hook on the assistant's own reply, read from the
  transcript, only when it is fresh and over the word threshold.

Two-pass design. The first attempt inside a 30-minute window is refused with a
question, not a rule, so the writer rewrites rather than patching. The second
attempt is checked mechanically: sentences over 30 words, three or more
numbers in one sentence, bare code identifiers used as prose, and unexplained
terms of art from ``jargon.txt`` beside this script.

``--check FILE`` (or ``-`` for stdin) runs the mechanical check directly.
``--reset`` clears the window state. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_BLOCK = 2
WINDOW_SECONDS = 30 * 60
FRESH_SECONDS = 2 * 60
STOP_MIN_WORDS = 120
POST_MIN_WORDS = 25
MAX_SENTENCE_WORDS = 30
MAX_NUMBERS_PER_SENTENCE = 2
MAX_JARGON_PER_PARAGRAPH = 2
MILLISECOND_EPOCH_THRESHOLD = 1e11
JARGON_FILE = Path(__file__).with_name("jargon.txt")
PROSE_KEYS = {
    "body",
    "description",
    "summary",
    "commentBody",
    "comment_body",
    "text",
    "message",
    "title",
    "comment",
    "content",
    "note",
}
OUTWARD_TOOL = re.compile(
    r"(send|post|create|reply|comment|update|add|write|publish|forward|draft)", re.IGNORECASE
)
GIT_COMMIT = re.compile(r"\bgit\s+(?:-[-\w=]+\s+)*commit\b")
COMMIT_FLAG_MESSAGE = re.compile(
    r"(?:^|\s)-(?:m|-message)(?:=|\s+)(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'|(\S+))"
)
STDIN_MESSAGE = re.compile(r"(?:-F|--file)[= ]+(?:-(?!\S)|/dev/stdin)")
HEREDOC = re.compile(
    r"<<-?\s*['\"]?(?P<tag>\w+)['\"]?\n(?P<body>.*?)\n\s*(?P=tag)\s*$", re.DOTALL | re.MULTILINE
)
REFUSAL = (
    "Prose gate: first attempt refused. Reread this as a smart colleague who did "
    "not watch the work. Would they follow it? Rewrite, then post again.\n"
)
EXPLANATION_CUES = re.compile(
    r"\b(?:like a|think of|in other words|that is,|i\.e\.|which means|meaning)\b|\(", re.IGNORECASE
)
NUMBER = re.compile(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?(?![\w.:%/-])")
IDENTIFIER = re.compile(
    r"(?<![`\w/.-])(?:[a-z]+_[a-z_\d]+|[a-z]+[A-Z][A-Za-z\d]+|--[a-z][\w-]+|[\w/.-]+\.(?:py|toml|yaml|yml|json|md|txt|js|ts|html))(?![`\w/])"
)
AGENT_AUDIENCE = re.compile(r"\b(?:sub)?agent\b|\bsession\b", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\d\"'(])")
FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
URL = re.compile(r"https?://\S+")
SCOPE_PREFIX = re.compile(r"^[\w./-]+(?:\([\w./-]+\))?!?: ")
TRAILER = re.compile(r"^[A-Z][\w-]+: \S.*$", re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    """One mechanical finding on a piece of prose."""

    rule: str
    detail: str

    def render(self) -> str:
        """One line for stderr or stdout."""
        return f"{self.rule}: {self.detail}"


class Jargon:
    """The editable list of terms of art, one per line, ``#`` comments."""

    def __init__(self, path: Path = JARGON_FILE) -> None:
        """Load ``path``; a missing file means an empty list."""
        self.terms: list[str] = []
        if path.exists():
            for raw in path.read_text(encoding="utf-8").splitlines():
                term = raw.split("#", 1)[0].strip()
                if term:
                    self.terms.append(term)
        self.pattern = self._compile()

    def _compile(self) -> re.Pattern[str] | None:
        if not self.terms:
            return None
        alternation = "|".join(re.escape(t) for t in sorted(self.terms, key=len, reverse=True))
        return re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])", re.IGNORECASE)


class ProseChecker:
    """Mechanical plain-language checks on markdown-ish text."""

    def __init__(self, jargon: Jargon | None = None) -> None:
        """Use ``jargon`` (default: the file beside this script)."""
        self.jargon = jargon or Jargon()

    @staticmethod
    def strip(text: str) -> str:
        """Remove fences, inline code, tables, block quotes, URLs, and git framing.

        A leading ``scope:`` on the first line and ``Key: value`` trailers are
        commit-message structure, not prose, so they are not judged as words.
        """
        text = SCOPE_PREFIX.sub("", text.lstrip(), count=1)
        text = TRAILER.sub(" ", text)
        text = FENCE.sub(" ", text)
        text = INLINE_CODE.sub(" ", text)
        text = URL.sub(" ", text)
        kept = [line for line in text.splitlines() if not line.lstrip().startswith(("|", ">"))]
        return "\n".join(kept)

    @staticmethod
    def paragraphs(text: str) -> list[str]:
        """Paragraphs, skipping any whose audience is another agent session."""
        out: list[str] = []
        skipping = False
        for block in re.split(r"\n\s*\n", text):
            stripped = block.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                skipping = bool(AGENT_AUDIENCE.search(stripped))
                continue
            if skipping or AGENT_AUDIENCE.match(stripped.split(":")[0]):
                continue
            out.append(stripped)
        return out

    @staticmethod
    def sentences(paragraph: str) -> list[str]:
        """Split a paragraph into sentences."""
        flat = re.sub(r"\s+", " ", paragraph)
        return [s.strip() for s in SENTENCE_END.split(flat) if s.strip()]

    def check(self, text: str) -> list[Finding]:
        """All findings for ``text``."""
        findings: list[Finding] = []
        for paragraph in self.paragraphs(self.strip(text)):
            for sentence in self.sentences(paragraph):
                findings.extend(self._check_sentence(sentence))
            findings.extend(self._check_jargon(paragraph))
        return findings

    def _check_sentence(self, sentence: str) -> list[Finding]:
        findings = []
        words = sentence.split()
        if len(words) > MAX_SENTENCE_WORDS:
            findings.append(Finding("long-sentence", f"{len(words)} words: {sentence[:70]}..."))
        numbers = NUMBER.findall(sentence)
        if len(numbers) > MAX_NUMBERS_PER_SENTENCE:
            findings.append(
                Finding(
                    "numbers",
                    f"{len(numbers)} numbers in one sentence; use a table or one per line: {sentence[:70]}...",
                )
            )
        for ident in sorted(set(IDENTIFIER.findall(sentence))):
            findings.append(
                Finding(
                    "identifier",
                    f"bare code identifier used as a word: {ident!r}; describe it in words or put it in backticks",
                )
            )
        return findings

    def _check_jargon(self, paragraph: str) -> list[Finding]:
        if self.jargon.pattern is None:
            return []
        findings = []
        for match in self.jargon.pattern.finditer(paragraph):
            window = paragraph[match.end() : match.end() + 80]
            if EXPLANATION_CUES.search(window):
                continue
            findings.append(
                Finding(
                    "jargon",
                    f"{match.group(0)!r} has no everyday explanation nearby (add 'like a...', 'in other words...', or a parenthetical)",
                )
            )
            if len(findings) >= MAX_JARGON_PER_PARAGRAPH:
                break
        return findings


class WindowState:
    """Per-repo state file recording the last refusal per channel."""

    def __init__(self, path: Path | None = None) -> None:
        """Use ``path`` or a per-repo file under the temp directory."""
        self.path = path or Path(tempfile.gettempdir()) / f"prose_gate_{_repo_key()}.json"
        self.data: dict[str, dict[str, float | str]] = self._load()

    def _load(self) -> dict[str, dict[str, float | str]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self) -> None:
        """Persist the state; failure to write is not the writer's problem."""
        with contextlib.suppress(OSError):
            self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def refused_recently(self, channel: str, digest: str) -> tuple[bool, bool]:
        """(a refusal is in the window, the text is identical to the refused one)."""
        entry = self.data.get(channel)
        if not entry:
            return False, False
        recent = time.time() - float(entry.get("at", 0)) < WINDOW_SECONDS
        return recent, recent and entry.get("digest") == digest

    def record(self, channel: str, digest: str) -> None:
        """Remember that ``digest`` was refused on ``channel`` now."""
        self.data[channel] = {"at": time.time(), "digest": digest}
        self.save()

    def clear(self, channel: str) -> None:
        """Forget the channel after a pass so the next post is refused again."""
        self.data.pop(channel, None)
        self.save()


def _repo_key() -> str:
    return hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:12]


def _digest(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.strip()).encode()).hexdigest()


@dataclass
class Extracted:
    """Prose found in a tool call plus the channel it belongs to."""

    channel: str
    texts: list[str] = field(default_factory=list)

    def joined(self) -> str:
        """All prose as one document."""
        return "\n\n".join(t for t in self.texts if t.strip())


class Extractor:
    """Pull outward prose out of a PreToolUse payload."""

    @classmethod
    def extract(cls, payload: dict) -> Extracted | None:
        """Prose for the call, or None when the tool is not outward-facing."""
        tool = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") or {}
        if tool == "Bash":
            return cls._from_command(str(tool_input.get("command", "")))
        if not OUTWARD_TOOL.search(tool):
            return None
        found: list[str] = []
        cls._walk(tool_input, found)
        return Extracted(channel=tool, texts=found) if found else None

    @classmethod
    def _walk(cls, node: object, found: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PROSE_KEYS and isinstance(value, str):
                    found.append(value)
                else:
                    cls._walk(value, found)
        elif isinstance(node, list):
            for item in node:
                cls._walk(item, found)

    @staticmethod
    def _from_command(command: str) -> Extracted | None:
        if not GIT_COMMIT.search(command):
            return None
        parts = ["".join(g for g in m.groups() if g) for m in COMMIT_FLAG_MESSAGE.finditer(command)]
        heredoc = HEREDOC.search(command)
        if heredoc and STDIN_MESSAGE.search(command):
            parts.append(heredoc.group("body"))
        return Extracted(channel="git-commit", texts=parts) if parts else None


def word_count(text: str) -> int:
    """Words in ``text`` after stripping code and tables."""
    return len(ProseChecker.strip(text).split())


def gate(
    text: str, channel: str, state: WindowState, checker: ProseChecker, *, min_words: int
) -> int:
    """Apply the two-pass rule; write to stderr; return an exit code."""
    if word_count(text) < min_words:
        return EXIT_OK
    digest = _digest(text)
    in_window, identical = state.refused_recently(channel, digest)
    if not in_window or identical:
        state.record(channel, digest)
        sys.stderr.write(REFUSAL)
        if identical:
            sys.stderr.write(
                "The text is unchanged since the refusal; it must be rewritten, not re-sent.\n"
            )
        return EXIT_BLOCK
    findings = checker.check(text)
    if findings:
        sys.stderr.write(
            "Prose gate: second attempt fails the mechanical check. Fix these, then post again:\n"
        )
        sys.stderr.write("\n".join(f.render() for f in findings) + "\n")
        return EXIT_BLOCK
    state.clear(channel)
    return EXIT_OK


def run_pre_tool_use(payload: dict) -> int:
    """PreToolUse entry: gate the prose in an outward tool call."""
    extracted = Extractor.extract(payload)
    if extracted is None:
        return EXIT_OK
    return gate(
        extracted.joined(),
        extracted.channel,
        WindowState(),
        ProseChecker(),
        min_words=POST_MIN_WORDS,
    )


def last_assistant_text(transcript: Path) -> tuple[str, float] | None:
    """(text, unix timestamp) of the last assistant message, or None."""
    latest: tuple[str, float] | None = None
    for raw in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content") or []
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        if text.strip():
            latest = (text, _parse_timestamp(entry.get("timestamp")))
    return latest


def _parse_timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > MILLISECOND_EPOCH_THRESHOLD else 1)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def run_stop(payload: dict) -> int:
    """Stop entry: gate the assistant's own fresh reply."""
    if payload.get("stop_hook_active"):
        return EXIT_OK
    transcript = Path(str(payload.get("transcript_path", "")))
    if not transcript.is_file():
        return EXIT_OK
    found = last_assistant_text(transcript)
    if found is None:
        return EXIT_OK
    text, stamp = found
    if stamp and time.time() - stamp > FRESH_SECONDS:
        return EXIT_OK
    return gate(text, "assistant-reply", WindowState(), ProseChecker(), min_words=STOP_MIN_WORDS)


def run_check(source: str) -> int:
    """Manual mode: print findings for a file (or stdin with ``-``)."""
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    findings = ProseChecker().check(text)
    for finding in findings:
        print(finding.render())
    print(f"{len(findings)} finding(s)")
    return EXIT_FINDINGS if findings else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """CLI definition."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pre-tool-use", action="store_true")
    mode.add_argument("--stop", action="store_true")
    mode.add_argument("--check", metavar="FILE")
    mode.add_argument("--reset", action="store_true")
    return parser


def read_payload() -> dict:
    """Hook JSON from stdin; malformed input means nothing to gate."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    if args.check:
        return run_check(args.check)
    if args.reset:
        WindowState().data.clear()
        WindowState().save()
        return EXIT_OK
    if os.environ.get("PROSE_GATE_DISABLED"):
        return EXIT_OK
    payload = read_payload()
    return run_stop(payload) if args.stop else run_pre_tool_use(payload)


if __name__ == "__main__":
    sys.exit(main())
