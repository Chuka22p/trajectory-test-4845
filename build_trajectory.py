#!/usr/bin/env python3
"""
Trajectory Builder for Cursor / VS Code Copilot Sessions
=========================================================
Merges two sources:
  1. Git log (--patch) → code diffs, timestamps, files changed
  2. IDE chat export (Markdown, JSON, or VS Code JSON) → prompts per step

Supports:
  - Cursor Markdown export (.md) — **User** / **Cursor** blocks
  - Cursor JSON export (.json) — from state.vscdb or JSON export
  - VS Code Copilot JSON export (.json) — from "Chat: Export Chat"
  - Plain text with User: / Assistant: prefixes

Usage:
  python build_trajectory.py \
    --repo /path/to/your/repo \
    --chat cursor_export.md \
    --tool cursor_composer_2.0 \
    --task-id "celo-org-celo-blockchain-2203" \
    --output trajectory.json

Git requirements:
  Each AI-assisted commit should be prefixed with a tag:
    [AI:accepted] Added three-body config params
    [AI:partial]  Validation logic (moved to _check_params)
    [AI:rejected] Bad suggestion, didn't use
    [manual]      Fixed import ordering
    [test]        All 14 tests pass

Shell functions (add to ~/.zshrc or ~/.bashrc):
  gai()  { git add -A && git commit -m "[AI:accepted] $*"; }
  gaip() { git add -A && git commit -m "[AI:partial] $*"; }
  gair() { git add -A && git commit -m "[AI:rejected] $*"; }
  gman() { git add -A && git commit -m "[manual] $*"; }
  gtest(){ git add -A && git commit --allow-empty -m "[test] $*"; }
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: str) -> str:
    """Run a shell command and return stdout."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True)
    stdout = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        print(f"[warn] Command failed: {' '.join(cmd)}\n{stderr}", file=sys.stderr)
    return stdout.strip()


def iso(ts: str) -> str:
    """Normalise a git timestamp to ISO-8601 with Z suffix."""
    ts = ts.strip()
    try:
        dt = datetime.fromisoformat(ts.replace(" +0000", "+00:00").replace(" -0000", "+00:00"))
    except ValueError:
        for fmt in ["%a %b %d %H:%M:%S %Y %z", "%Y-%m-%d %H:%M:%S %z"]:
            try:
                dt = datetime.strptime(ts, fmt)
                break
            except ValueError:
                continue
        else:
            return ts
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_to_iso(ms: int) -> str:
    """Convert millisecond epoch timestamp to ISO-8601 UTC string."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def truncate_diff(diff: str, max_lines: int = 0) -> str:
    """Optionally truncate long diffs. max_lines=0 means no truncation (full diff)."""
    if max_lines <= 0:
        return diff
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"


# ── Git parsing ──────────────────────────────────────────────────────────────

COMMIT_SEP = "<<COMMIT_SEP>>"

# Commits matching these subjects are auto-skipped (initial repo state)
SKIP_SUBJECTS = {"initial commit", "initial task state"}


def get_commits(repo: str, since: str = None, until: str = None) -> list[dict]:
    """
    Extract commits with diffs from the git log.
    Returns a list of commit dicts in chronological order (oldest first).
    Automatically skips the initial commit (full repo snapshot).
    """
    fmt = f"{COMMIT_SEP}%n%H%n%aI%n%s%n%b%n<<BODY_END>>"
    cmd = ["git", "log", f"--format={fmt}", "--patch", "--no-merges"]
    if since:
        cmd += [f"--after={since}"]
    if until:
        cmd += [f"--before={until}"]

    raw = run(cmd, cwd=repo)
    if not raw:
        print("[info] No commits found in the given time range.", file=sys.stderr)
        return []

    blocks = raw.split(COMMIT_SEP)
    commits = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        if len(lines) < 3:
            continue

        sha = lines[0].strip()
        timestamp = iso(lines[1].strip())
        subject = lines[2].strip()

        # Skip initial commits (huge repo snapshots with no useful diff)
        if subject.lower().strip() in SKIP_SUBJECTS:
            print(f"       Skipping initial commit: {sha[:8]} '{subject}'")
            continue

        # Body ends at <<BODY_END>>, diff follows
        body_lines = []
        diff_lines = []
        in_diff = False
        for line in lines[3:]:
            if line == "<<BODY_END>>":
                in_diff = True
                continue
            if in_diff:
                diff_lines.append(line)
            else:
                body_lines.append(line)

        diff = "\n".join(diff_lines).strip()

        # Extract changed files from diff header lines
        files = sorted(set(
            re.sub(r"^b/", "", m.group(1)).strip()
            for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)
        ))

        # Detect action tag from commit subject
        subject_lower = subject.lower()
        if subject_lower.startswith("[ai:accepted]") or subject_lower.startswith("[ai]"):
            action = "accepted"
            step_type = "ai_suggestion"
        elif subject_lower.startswith("[ai:partial]"):
            action = "partially_accepted"
            step_type = "ai_suggestion"
        elif subject_lower.startswith("[ai:rejected]"):
            action = "rejected"
            step_type = "ai_suggestion"
        elif subject_lower.startswith("[revert]"):
            action = "reverted"
            step_type = "ai_suggestion"
        elif subject_lower.startswith("[test]"):
            action = None
            step_type = "test_run"
        elif subject_lower.startswith("[manual]"):
            action = None
            step_type = "manual_edit"
        else:
            action = None
            step_type = "manual_edit"

        # Clean subject of tag prefix
        clean_subject = re.sub(r"^\[.*?\]\s*", "", subject).strip()

        commits.append({
            "sha":         sha,
            "timestamp":   timestamp,
            "type":        step_type,
            "action":      action,
            "description": clean_subject,
            "body":        "\n".join(body_lines).strip(),
            "files":       files,
            "diff":        diff,
        })

    return list(reversed(commits))  # chronological order


# ── Chat parsing ─────────────────────────────────────────────────────────────

# Prompts that are clearly not task-related (greetings, unrelated chatter)
NOISE_PATTERNS = [
    r"^(hi|hello|hey|thanks|thank you|ok|okay|yes|no|perfect|perfecf|done)$",
    r"^@/",                          # file reference with no actual prompt
    r"please open this folder",
    r"paraphrase this",
    r"patraphrse this",
]


def is_noise(text: str) -> bool:
    """Check if a prompt is noise (greeting, file ref, etc.) not a real task prompt."""
    text_clean = text.strip().lower()
    if len(text_clean) < 5:
        return True
    for pattern in NOISE_PATTERNS:
        if re.search(pattern, text_clean, re.IGNORECASE):
            return True
    return False


def detect_chat_format(path: str) -> str:
    """
    Detect the chat export format based on file extension and content.
    Returns one of: 'vscode_json', 'cursor_json', 'cursor_markdown', 'plain_text'
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")

    # JSON files
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                # VS Code Copilot export has 'responderUsername' and 'requests'
                if "requests" in data and "responderUsername" in data:
                    return "vscode_json"
                # Cursor JSON export has 'conversations' with 'messages'
                if "conversations" in data:
                    return "cursor_json"
                # Generic JSON with messages array
                if "messages" in data:
                    return "cursor_json"
            # JSON array of messages
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict) and "role" in data[0]:
                return "cursor_json"
        except json.JSONDecodeError:
            pass

    # Markdown files or fallback for JSON that didn't parse
    if "**User**" in text and ("**Cursor**" in text or "**Assistant**" in text):
        return "cursor_markdown"

    return "plain_text"


def load_vscode_json(path: str) -> tuple[list[dict], dict]:
    """
    Parse VS Code Copilot Chat JSON export.
    Returns:
      - list of {role, content, timestamp_ms} dicts (user prompts, noise filtered)
      - token_usage dict with {input_tokens, output_tokens, total_tokens, per_request: [...]}
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)

    requests = data.get("requests", [])
    messages = []
    token_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "models_used": [],
        "per_request": [],
        "latency": {
            "total_elapsed_ms": 0,
            "total_first_progress_ms": 0,
            "per_request": [],
            "note": "total_elapsed_ms is the sum of per-request AI working time only, excludes idle time between requests",
        },
    }
    models_seen = set()

    for req in requests:
        # Extract user prompt
        msg = req.get("message", {})
        user_text = msg.get("text", "").strip()

        # Extract timestamp
        timestamp_ms = req.get("timestamp")

        # Extract token counts from result.metadata
        result = req.get("result", {})
        meta = result.get("metadata", {})
        prompt_tokens = meta.get("promptTokens", 0) or 0
        output_tokens = meta.get("outputTokens", 0) or 0
        model = meta.get("resolvedModel", "unknown")

        # Extract latency from result.timings
        timings = result.get("timings", {})
        first_progress_ms = timings.get("firstProgress", 0) or 0
        total_elapsed_ms = timings.get("totalElapsed", 0) or 0
        # fallback to elapsedMs at request level if timings not available
        if total_elapsed_ms == 0:
            total_elapsed_ms = req.get("elapsedMs", 0) or 0

        # Accumulate tokens
        token_usage["input_tokens"] += prompt_tokens
        token_usage["output_tokens"] += output_tokens
        token_usage["total_tokens"] += prompt_tokens + output_tokens

        # Accumulate latency (only AI working time, not idle time between requests)
        token_usage["latency"]["total_elapsed_ms"] += total_elapsed_ms
        token_usage["latency"]["total_first_progress_ms"] += first_progress_ms

        if model and model != "unknown":
            models_seen.add(model)

        # Per-request breakdown (tokens + latency + full prompt)
        token_usage["per_request"].append({
            "prompt": user_text if user_text else "(empty)",
            "input_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "model": model,
            "first_progress_ms": first_progress_ms,
            "elapsed_ms": total_elapsed_ms,
            "timestamp_ms": timestamp_ms,
        })

        # Per-request latency breakdown
        token_usage["latency"]["per_request"].append({
            "request_index": len(token_usage["latency"]["per_request"]) + 1,
            "first_progress_ms": first_progress_ms,
            "elapsed_ms": total_elapsed_ms,
            "timestamp_ms": timestamp_ms,
        })

        # Add user message if not noise
        if user_text and not is_noise(user_text):
            messages.append({
                "role": "user",
                "content": user_text,
                "timestamp_ms": timestamp_ms,
            })

    token_usage["models_used"] = sorted(models_seen)

    return messages, token_usage


def load_cursor_json(path: str) -> tuple[list[dict], dict]:
    """
    Parse Cursor JSON export (from state.vscdb or direct JSON export).
    Returns:
      - list of {role, content} dicts (user prompts, noise filtered)
      - token_usage dict (usually empty for Cursor since it doesn't populate tokens)
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)

    messages = []
    token_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "models_used": [],
        "per_request": [],
        "note": "Cursor export does not provide token counts",
    }

    # Handle different Cursor JSON structures
    if isinstance(data, list):
        messages = [m for m in data if m.get("role") == "user"]
    elif isinstance(data, dict):
        if "messages" in data:
            messages = [m for m in data["messages"] if m.get("role") == "user"]
        elif "conversations" in data:
            # Cursor chat export with conversations array
            for conv in data.get("conversations", []):
                for msg in conv.get("messages", []):
                    if msg.get("role") == "user":
                        # Extract text from bubble_data if present
                        bubble = msg.get("bubble_data", {})
                        text_content = bubble.get("text", "")
                        if not text_content:
                            # Try message-level content
                            text_content = msg.get("content", "")
                        if text_content:
                            messages.append({"role": "user", "content": text_content})

                    # Try to extract token counts (usually zeros but try anyway)
                    bubble = msg.get("bubble_data", {})
                    tc = bubble.get("tokenCount", {})
                    inp = tc.get("inputTokens", 0) or 0
                    out = tc.get("outputTokens", 0) or 0
                    if inp > 0 or out > 0:
                        token_usage["input_tokens"] += inp
                        token_usage["output_tokens"] += out
                        token_usage["total_tokens"] += inp + out
        else:
            # Try to find any list of messages in the dict
            for key, val in data.items():
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict) and "role" in val[0]:
                    messages = [m for m in val if m.get("role") == "user"]
                    break

    # If tokens were found, remove the note
    if token_usage["total_tokens"] > 0:
        token_usage.pop("note", None)

    filtered = [m for m in messages if not is_noise(m.get("content", ""))]
    return filtered, token_usage


def load_cursor_markdown(path: str) -> tuple[list[dict], dict]:
    """
    Parse Cursor markdown export (**User** / **Cursor** blocks separated by ---).
    Returns:
      - list of {role, content} dicts (user prompts, noise filtered)
      - token_usage dict (empty — markdown has no token info)
    """
    text = Path(path).read_text(encoding="utf-8")
    token_usage = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "note": "Cursor markdown export does not include token counts",
    }

    messages = []

    # Split on --- separators
    blocks = re.split(r"\n---\n", text)
    current_role = None
    current_content = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        if block.startswith("**User**"):
            if current_role == "user" and current_content:
                content = "\n".join(current_content).strip()
                if content:
                    messages.append({"role": "user", "content": content})
            current_role = "user"
            lines = block.splitlines()
            current_content = []
            for line in lines[1:]:
                stripped = line.strip()
                if stripped:
                    current_content.append(stripped)

        elif block.startswith("**Cursor**") or block.startswith("**Assistant**"):
            if current_role == "user" and current_content:
                content = "\n".join(current_content).strip()
                if content:
                    messages.append({"role": "user", "content": content})
            current_role = "assistant"
            current_content = []

        elif current_role == "user":
            current_content.append(block)

    # Save last user block
    if current_role == "user" and current_content:
        content = "\n".join(current_content).strip()
        if content:
            messages.append({"role": "user", "content": content})

    if messages:
        cleaned = []
        for m in messages:
            content = m["content"]
            parts = [p.strip() for p in re.split(r"\n{2,}", content) if p.strip()]
            substantive = [p for p in parts if not is_noise(p)]
            if substantive:
                cleaned.append({"role": "user", "content": "\n".join(substantive)})
        return cleaned, token_usage

    return messages, token_usage


def load_plain_text(path: str) -> tuple[list[dict], dict]:
    """
    Parse plain text with User: / Assistant: prefixes.
    Returns:
      - list of {role, content} dicts
      - token_usage dict (empty)
    """
    text = Path(path).read_text(encoding="utf-8")
    token_usage = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "note": "Plain text export does not include token counts",
    }

    messages = []
    current_role = None
    current_lines = []

    for line in text.splitlines():
        if line.startswith("User:"):
            if current_role == "user" and current_lines:
                messages.append({"role": "user", "content": "\n".join(current_lines).strip()})
            current_role = "user"
            current_lines = [line[5:].strip()]
        elif line.startswith("Assistant:"):
            if current_role == "user" and current_lines:
                messages.append({"role": "user", "content": "\n".join(current_lines).strip()})
            current_role = "assistant"
            current_lines = []
        elif current_role == "user":
            current_lines.append(line)

    if current_role == "user" and current_lines:
        messages.append({"role": "user", "content": "\n".join(current_lines).strip()})

    filtered = [m for m in messages if not is_noise(m.get("content", ""))]
    return filtered, token_usage


def load_chat(path: str) -> tuple[list[dict], dict]:
    """
    Auto-detect chat export format and parse it.
    Returns:
      - list of {role, content} dicts (user prompts, noise filtered)
      - token_usage dict
    """
    fmt = detect_chat_format(path)
    print(f"       Detected format: {fmt}")

    if fmt == "vscode_json":
        return load_vscode_json(path)
    elif fmt == "cursor_json":
        return load_cursor_json(path)
    elif fmt == "cursor_markdown":
        return load_cursor_markdown(path)
    else:
        return load_plain_text(path)


# ── Merge commits + prompts ──────────────────────────────────────────────────

def merge(commits: list[dict], prompts: list[dict], max_diff_lines: int = 0) -> list[dict]:
    """
    Pair each AI commit with the nearest user prompt.
    Manual edits and test runs get no prompt attached.
    """
    steps = []
    prompt_idx = 0

    for i, commit in enumerate(commits):
        step = {
            "step":        i + 1,
            "timestamp":   commit["timestamp"],
            "type":        commit["type"],
            "description": commit["description"],
        }

        if commit["action"]:
            step["action"] = commit["action"]

        # Attach user prompt for AI suggestion steps
        if commit["type"] == "ai_suggestion" and prompt_idx < len(prompts):
            step["user_prompt"] = prompts[prompt_idx]["content"]
            prompt_idx += 1

        # Test run steps: parse pass/fail counts from description
        if commit["type"] == "test_run":
            pass_match = re.search(r"(\d+)\s*pass", commit["description"], re.IGNORECASE)
            fail_match = re.search(r"(\d+)\s*fail", commit["description"], re.IGNORECASE)
            if pass_match or fail_match:
                step["tests_passed"] = int(pass_match.group(1)) if pass_match else 0
                step["tests_failed"] = int(fail_match.group(1)) if fail_match else 0
                step["result"] = "pass" if (not fail_match or int(fail_match.group(1)) == 0) else "fail"

        if commit["files"]:
            step["files_modified"] = commit["files"]

        if commit["diff"]:
            step["code_diff"] = truncate_diff(commit["diff"], max_diff_lines)

        steps.append(step)

    return steps


# ── Summary ──────────────────────────────────────────────────────────────────

def count_added_lines(diff: str) -> int:
    """Count lines added (starting with +, excluding +++ header) in a diff."""
    count = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            count += 1
    return count


def make_summary(steps: list[dict], commits: list[dict]) -> dict:
    """Build a summary of the trajectory."""
    ai_steps = [s for s in steps if s.get("type") == "ai_suggestion"]
    accepted = sum(1 for s in ai_steps if s.get("action") == "accepted")
    partial  = sum(1 for s in ai_steps if s.get("action") == "partially_accepted")
    rejected = sum(1 for s in ai_steps if s.get("action") == "rejected")
    reverted = sum(1 for s in ai_steps if s.get("action") == "reverted")
    manual   = sum(1 for s in steps if s.get("type") == "manual_edit")

    ai_lines = 0
    manual_lines = 0
    for commit in commits:
        added = count_added_lines(commit.get("diff", ""))
        if commit["type"] == "ai_suggestion":
            ai_lines += added
        else:
            manual_lines += added

    all_files = set()
    for s in steps:
        all_files.update(s.get("files_modified", []))

    return {
        "total_ai_suggestions":  len(ai_steps),
        "accepted":              accepted,
        "partially_accepted":    partial,
        "rejected":              rejected,
        "reverted":              reverted,
        "manual_edits":          manual,
        "ai_lines_added":        ai_lines,
        "manual_lines_added":    manual_lines,
        "files_modified":        sorted(all_files),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build LLM session trajectory from git commits + IDE chat export"
    )
    parser.add_argument("--repo",    required=True,              help="Path to the git repo")
    parser.add_argument("--chat",    default=None,               help="Path to chat export (.md, .json, or VS Code JSON)")
    parser.add_argument("--since",   default=None,               help="Start time, e.g. '2026-03-20T10:00:00'")
    parser.add_argument("--until",   default=None,               help="End time, e.g. '2026-03-20T11:00:00'")
    parser.add_argument("--output",  default="trajectory.json",  help="Output JSON file path")
    parser.add_argument("--tool",    default="cursor_composer_2.0", help="Tool name")
    parser.add_argument("--task-id", default="",                 help="Task ID")
    parser.add_argument("--max-diff-lines", type=int, default=0, help="Max diff lines per step (0 = no truncation, full diffs)")
    args = parser.parse_args()

    # ── Step 1: Git log ──
    print(f"[1/4] Reading git log from {args.repo} ...")
    commits = get_commits(args.repo, args.since, args.until)
    print(f"       Found {len(commits)} commits (after skipping initial).")

    if not commits:
        print("\n[ERROR] No commits found. Check that:")
        print("  - You're pointing --repo at the right directory")
        print("  - Your --since/--until range covers your solve session")
        print("  - You made tagged commits (gai, gaip, gair, gman, gtest)")
        sys.exit(1)

    # ── Step 2: Chat export ──
    prompts = []
    token_usage = {}
    if args.chat:
        print(f"[2/4] Loading chat export from {args.chat} ...")
        prompts, token_usage = load_chat(args.chat)
        print(f"       Found {len(prompts)} task-related prompts (noise filtered).")
        for i, p in enumerate(prompts):
            preview = p["content"][:60].replace("\n", " ")
            print(f"         {i+1}. \"{preview}...\"")

        # Print token summary
        if token_usage.get("total_tokens"):
            print(f"       Token usage: {token_usage['input_tokens']:,} input + {token_usage['output_tokens']:,} output = {token_usage['total_tokens']:,} total")
            if token_usage.get("models_used"):
                print(f"       Models used: {', '.join(token_usage['models_used'])}")
        elif token_usage.get("note"):
            print(f"       Token usage: {token_usage['note']}")
    else:
        print("[2/4] No chat export provided — steps will have no prompt field.")

    # ── Step 3: Merge ──
    ai_commit_count = sum(1 for c in commits if c["type"] == "ai_suggestion")
    if prompts and len(prompts) != ai_commit_count:
        print(f"\n[WARN] Prompt count ({len(prompts)}) != AI commit count ({ai_commit_count}).")
        print(f"       Prompts will be matched in order; some may be unmatched.")

    print("[3/4] Merging commits and prompts ...")
    steps = merge(commits, prompts, max_diff_lines=args.max_diff_lines)

    # Add final submit step
    if steps:
        last_ts = steps[-1]["timestamp"]
        steps.append({
            "step":        len(steps) + 1,
            "timestamp":   last_ts,
            "type":        "submit",
            "description": "Final solution submitted",
        })

    summary = make_summary(steps, commits)

    start_time = steps[0]["timestamp"] if steps else ""
    end_time   = steps[-1]["timestamp"] if steps else ""

    trajectory = {
        "tool":       args.tool,
        "task_id":    args.task_id,
        "start_time": start_time,
        "end_time":   end_time,
        "steps":      steps,
        "summary":    summary,
    }

    # Add cost/token usage if available
    if token_usage:
        cost = {
            "input_tokens": token_usage.get("input_tokens"),
            "output_tokens": token_usage.get("output_tokens"),
            "total_tokens": token_usage.get("total_tokens"),
        }
        # Add models used if present
        if token_usage.get("models_used"):
            cost["models_used"] = token_usage["models_used"]
        # Add per-request breakdown if present (includes full prompts, tokens, and latency per request)
        if token_usage.get("per_request"):
            cost["per_request"] = token_usage["per_request"]
        # Add note if tokens not available
        if token_usage.get("note"):
            cost["note"] = token_usage["note"]

        trajectory["cost"] = cost

    # Add latency data if available (separate top-level key for clarity)
    if token_usage and token_usage.get("latency") and token_usage["latency"].get("total_elapsed_ms", 0) > 0:
        trajectory["latency"] = token_usage["latency"]

    # ── Step 4: Write output ──
    out_path = Path(args.output)
    out_path.write_text(json.dumps(trajectory, indent=2), encoding="utf-8")
    print(f"[4/4] Trajectory written to {out_path}  ({len(steps)} steps)")
    print()
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"  Tool:              {args.tool}")
    print(f"  Task:              {args.task_id}")
    print(f"  Time:              {start_time} → {end_time}")
    print(f"  Total steps:       {len(steps)}")
    print(f"  AI suggestions:    {summary['total_ai_suggestions']} ({summary['accepted']} accepted, {summary['partially_accepted']} partial, {summary['rejected']} rejected)")
    print(f"  Manual edits:      {summary['manual_edits']}")
    print(f"  AI lines added:    {summary['ai_lines_added']}")
    print(f"  Manual lines added:{summary['manual_lines_added']}")
    print(f"  Files modified:    {len(summary['files_modified'])}")

    # Print cost summary
    if token_usage.get("total_tokens"):
        print(f"\n  === TOKEN USAGE ===")
        print(f"  Input tokens:      {token_usage['input_tokens']:,}")
        print(f"  Output tokens:     {token_usage['output_tokens']:,}")
        print(f"  Total tokens:      {token_usage['total_tokens']:,}")
        if token_usage.get("models_used"):
            print(f"  Models:            {', '.join(token_usage['models_used'])}")

    # Print latency summary
    latency = token_usage.get("latency", {})
    if latency.get("total_elapsed_ms", 0) > 0:
        total_secs = latency["total_elapsed_ms"] / 1000
        total_first_secs = latency["total_first_progress_ms"] / 1000
        print(f"\n  === LATENCY (AI working time only, excludes idle time) ===")
        print(f"  Total AI elapsed:      {total_secs:.1f}s ({total_secs/60:.1f}m)")
        print(f"  Total first-token:     {total_first_secs:.1f}s")
        print(f"  Requests:              {len(latency.get('per_request', []))}")

    # Print per-request breakdown (tokens + latency + prompt)
    if token_usage.get("per_request"):
        print(f"\n  === PER-REQUEST BREAKDOWN ===")
        for i, pr in enumerate(token_usage["per_request"]):
            elapsed_s = pr.get("elapsed_ms", 0) / 1000
            first_s = pr.get("first_progress_ms", 0) / 1000
            prompt_preview = pr.get("prompt", "")[:80].replace("\n", " ")
            print(f"    {i+1}. [{pr.get('model', '?')}]")
            print(f"       Tokens:  {pr.get('input_tokens', 0):,} in / {pr.get('output_tokens', 0):,} out")
            print(f"       Latency: {elapsed_s:.1f}s total, {first_s:.1f}s to first token")
            print(f"       Prompt:  \"{prompt_preview}...\"")

    print()
    print("Steps:")
    for s in steps:
        tag = s.get("action", s["type"])
        prompt_icon = " [has prompt]" if s.get("user_prompt") else ""
        print(f"  {s['step']}. [{tag}] {s.get('description', '')[:70]}{prompt_icon}")


if __name__ == "__main__":
    main()
