#!/usr/bin/env python3
"""Byl's Voice AI-ism guard.

Keeps Claude's stock prose habits out of Byl's fiction, in real time, at
three points in every exchange:

  prompt  UserPromptSubmit: briefs Claude on the kill list before it writes.
  stop    Stop: scans the prose Claude just wrote in chat. If it finds a
          [block] AI-ism, Claude is sent back to deliver it clean.
  file    PostToolUse (Write/Edit/MultiEdit): scans prose Claude just wrote
          into a manuscript or character file, and sends it back to fix it.

Manual use:
  python3 .claude/hooks/aism_guard.py check <file>   scan a file
  python3 .claude/hooks/aism_guard.py check -        scan stdin

Dialogue is never scanned: the Golden Rule says it stays word for word.

The kill list lives in ai-isms.txt next to this script. Edit it freely.

To switch the guard off, create .claude/hooks/aism-guard.off (or set
AISM_GUARD=off). Delete the file to switch it back on.
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERN_FILE = os.path.join(HERE, "ai-isms.txt")
OFF_FILE = os.path.join(HERE, "aism-guard.off")

# Chat replies shorter than this (after stripping markdown) aren't prose.
MIN_PROSE_WORDS = 120
MAX_REPORTED_HITS = 30

# Files Claude writes that are not fiction.
SKIP_EXTENSIONS = {
    ".py", ".js", ".ts", ".json", ".sh", ".yml", ".yaml", ".toml",
    ".html", ".css", ".cfg", ".ini", ".lock",
}
SKIP_NAMES = {"CLAUDE.md", "README.md"}

HEADER_RE = re.compile(r"^##\s*\[(block|warn)\]\s*(.+?)\s*\|\s*(.+)$")


def guard_off():
    return os.environ.get("AISM_GUARD", "").lower() == "off" or os.path.exists(OFF_FILE)


def load_rules():
    """Return a list of categories: {name, severity, fix, patterns}."""
    categories = []
    current = None
    with open(PATTERN_FILE, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            header = HEADER_RE.match(line.strip())
            if header:
                current = {
                    "severity": header.group(1),
                    "name": header.group(2),
                    "fix": header.group(3),
                    "patterns": [],
                }
                categories.append(current)
                continue
            if not line.strip() or line.lstrip().startswith("#") or current is None:
                continue
            try:
                current["patterns"].append(re.compile(line.strip(), re.IGNORECASE | re.MULTILINE))
            except re.error as err:
                print(f"aism_guard: bad pattern {line.strip()!r}: {err}", file=sys.stderr)
    return categories


# ---------------------------------------------------------------------------
# Text preparation


def strip_dialogue(paragraph):
    """Blank out everything inside quotation marks, keeping offsets intact."""
    out = []
    in_quote = False
    for ch in paragraph:
        if ch in "“”\"":
            if ch == "“":
                in_quote = True
            elif ch == "”":
                in_quote = False
            else:
                in_quote = not in_quote
            out.append(" ")
            continue
        out.append(" " if in_quote and ch != "\n" else ch)
    # An unclosed quote runs to the end of the paragraph (multi-paragraph
    # speeches reopen with a fresh quote mark), which the loop above handles.
    return "".join(out)


def narration_only(text):
    paragraphs = re.split(r"(\n\s*\n)", text)
    return "".join(strip_dialogue(p) for p in paragraphs)


def strip_markdown(text):
    """Reduce a chat reply to its prose: drop code, lists, headings, tables."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^([-*+]\s|\d+[.)]\s|#|\||>|---|\*\*\*)", s):
            kept.append("")
            continue
        kept.append(line)
    return "\n".join(kept)


def is_prose(text):
    words = re.findall(r"[A-Za-z']+", text)
    return len(words) >= MIN_PROSE_WORDS


# ---------------------------------------------------------------------------
# Scanning


def sentence_around(text, start, end):
    left = max(text.rfind(c, 0, start) for c in ".!?\n")
    rights = [i for i in (text.find(c, end) for c in ".!?\n") if i != -1]
    right = min(rights) + 1 if rights else len(text)
    snippet = " ".join(text[left + 1:right].split())
    if len(snippet) > 220:
        snippet = snippet[:217] + "..."
    return snippet


def scan(original, categories):
    """Return hits as dicts: severity, category, fix, match, sentence."""
    text = narration_only(original)
    hits = []
    seen = set()
    for cat in categories:
        for pat in cat["patterns"]:
            for m in pat.finditer(text):
                matched = original[m.start():m.end()].strip(" ,.;")
                if not matched.strip():
                    continue
                key = (m.start(), cat["name"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append({
                    "severity": cat["severity"],
                    "category": cat["name"],
                    "fix": cat["fix"],
                    "match": matched,
                    "sentence": sentence_around(original, m.start(), m.end()),
                    "pos": m.start(),
                })
    hits.sort(key=lambda h: h["pos"])
    return hits


def format_report(hits):
    blocking = [h for h in hits if h["severity"] == "block"]
    warnings = [h for h in hits if h["severity"] == "warn"]
    lines = []
    fixes = {}
    for label, group in (("KILL", blocking), ("WATCH", warnings)):
        if not group:
            continue
        lines.append(f"{label}:")
        for h in group[:MAX_REPORTED_HITS]:
            lines.append(f'  [{h["category"]}] "{h["match"]}" in: {h["sentence"]}')
            fixes[h["category"]] = h["fix"]
        if len(group) > MAX_REPORTED_HITS:
            lines.append(f"  ...and {len(group) - MAX_REPORTED_HITS} more.")
    if fixes:
        lines.append("How to fix:")
        for name, fix in fixes.items():
            lines.append(f"  {name}: {fix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hook modes


def read_hook_input():
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def mode_prompt(categories):
    """UserPromptSubmit: stdout is added to Claude's context."""
    lines = [
        "Byl's Voice guard is on. Narration (never dialogue) is scanned for these AI-isms after you write:"
    ]
    for cat in categories:
        tag = "" if cat["severity"] == "block" else " (watch)"
        lines.append(f"- {cat['name']}{tag}: {cat['fix']}")
    lines.append(
        "Write it clean the first time: grounded images, confident declarative sentences, "
        "emotion carried by the body. Dialogue inside quotes is never touched."
    )
    print("\n".join(lines))
    return 0


def last_assistant_text(data):
    """The text of Claude's final reply in this turn."""
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        return msg
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return ""
    entries = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    chunks = []
    for entry in reversed(entries):
        etype = entry.get("type")
        content = (entry.get("message") or {}).get("content")
        if etype == "user":
            # A tool result belongs to this turn; a real prompt ends it.
            if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                continue
            break
        if etype != "assistant":
            continue
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.append("\n\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ))
    return "\n\n".join(c for c in reversed(chunks) if c.strip())


def mode_stop(categories):
    data = read_hook_input()
    # One rewrite per turn. Never trap Claude in a loop.
    if data.get("stop_hook_active"):
        return 0
    prose = strip_markdown(last_assistant_text(data))
    if not is_prose(prose):
        return 0
    hits = [h for h in scan(prose, categories) if h["severity"] == "block"]
    if not hits:
        return 0
    reason = (
        "Byl's Voice guard caught AI-isms in the narration you just wrote.\n\n"
        + format_report(hits)
        + "\n\nDeliver the passage again in full, with every hit rewritten in Byl's voice. "
        "Leave dialogue inside quotes exactly as written, word for word. "
        "Output only the clean passage: no apology, no changelog, no mention of this check."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def is_fiction_path(path):
    if not path:
        return False
    name = os.path.basename(path)
    if name in SKIP_NAMES or name.startswith("."):
        return False
    if os.path.splitext(name)[1].lower() in SKIP_EXTENSIONS:
        return False
    parts = os.path.normpath(path).split(os.sep)
    return ".claude" not in parts and ".git" not in parts


def written_text(tool_input):
    if "content" in tool_input:
        return tool_input.get("content") or ""
    if "new_string" in tool_input:
        return tool_input.get("new_string") or ""
    edits = tool_input.get("edits") or []
    return "\n\n".join(e.get("new_string", "") for e in edits if isinstance(e, dict))


def mode_file(categories):
    data = read_hook_input()
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path", "")
    if not is_fiction_path(path):
        return 0
    text = written_text(tool_input)
    hits = [h for h in scan(text, categories) if h["severity"] == "block"]
    if not hits:
        return 0
    reason = (
        f"Byl's Voice guard caught AI-isms in the narration just written to {os.path.basename(path)}.\n\n"
        + format_report(hits)
        + "\n\nEdit the file now and rewrite each hit in Byl's voice. "
        "Leave dialogue inside quotes exactly as written. Don't mention this check to Byl."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def mode_check(categories, target):
    if target == "-":
        text = sys.stdin.read()
    else:
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    hits = scan(text, categories)
    if not hits:
        print("Clean. No AI-isms found in the narration.")
        return 0
    blocking = sum(1 for h in hits if h["severity"] == "block")
    print(f"{blocking} to kill, {len(hits) - blocking} to watch.\n")
    print(format_report(hits))
    return 1 if blocking else 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    mode = sys.argv[1]
    if mode != "check" and guard_off():
        return 0
    categories = load_rules()
    if mode == "prompt":
        return mode_prompt(categories)
    if mode == "stop":
        return mode_stop(categories)
    if mode == "file":
        return mode_file(categories)
    if mode == "check":
        return mode_check(categories, sys.argv[2] if len(sys.argv) > 2 else "-")
    print(f"aism_guard: unknown mode {mode!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
