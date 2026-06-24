#!/usr/bin/env python3
"""Run tests for the current baseline attempt by submitting the solution to the eval server."""

import asyncio
import base64
import gzip
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

from config import BASE_URL, WORKSPACE_DIR, TERMINAL_SOLUTION_FILE, get_api_headers
from utils.session_management import load_session_info
from utils.validation import validate_terminal_solution

V2_BENCHMARK_IDS = {
    # "imperium", 
    # "imperium-qc", 
    "theta-blind", 
    # "imperium-blind",
}


def _get_ws_url(version: str = "v1") -> str:
    base = BASE_URL.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    if version == "v2":
        return f"{base}/internal/code-data/v1/baseline/test/v2"
    return f"{base}/internal/code-data/v1/baseline/test"


@dataclass
class WSSessionResult:
    """Result container populated by a single WebSocket grading session."""

    connected: bool = False
    stdout: str = ""
    stderr: str = ""
    test_summary: dict | None = None
    rubric_grade_summary: dict | None = None
    score: float | None = None
    rubric_score: float | None = None
    result_status: str | None = None
    result_error: str | None = None
    received_result_or_error: bool = False


def load_gitignore_patterns() -> list[str]:
    """Load all gitignore patterns from utils/gitignores directory.

    Returns:
        List of unique gitignore patterns from all gitignore files.
    """
    gitignores_dir = Path(__file__).parent / "utils" / "gitignores"
    patterns = []

    if not gitignores_dir.exists():
        return patterns

    for gitignore_file in sorted(gitignores_dir.glob("*.gitignore")):
        with open(gitignore_file, "r") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines, comments, and negation patterns
                if line and not line.startswith("#") and not line.startswith("!"):
                    patterns.append(line)

    # Remove duplicates while preserving order
    seen = set()
    unique_patterns = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            unique_patterns.append(p)

    return unique_patterns


def convert_to_pathspec_excludes(patterns: list[str]) -> list[str]:
    """Convert gitignore patterns to git pathspec exclude patterns.

    Git pathspec excludes use the ':!' prefix. To match gitignore behavior
    where patterns match anywhere in the tree, we prepend '**/' to patterns
    that don't already have it.

    Args:
        patterns: List of gitignore patterns.

    Returns:
        List of pathspec exclude arguments for git diff.
    """
    excludes = []

    for pattern in patterns:
        # Handle directory patterns (ending with /)
        if pattern.endswith("/"):
            # Match directory anywhere and all contents
            dir_name = pattern.rstrip("/")
            if not pattern.startswith("**/"):
                excludes.append(f":(exclude)**/{dir_name}/**")
            else:
                excludes.append(f":(exclude){dir_name}/**")
        elif pattern.startswith("/"):
            # Anchored to root - remove leading slash
            excludes.append(f":(exclude){pattern[1:]}")
        elif "**" in pattern:
            # Already has globstar, use as-is
            excludes.append(f":(exclude){pattern}")
        elif "/" in pattern:
            # Contains path separator - match from root and anywhere
            excludes.append(f":(exclude){pattern}")
            excludes.append(f":(exclude)**/{pattern}")
        else:
            # Simple pattern - match anywhere
            excludes.append(f":(exclude)**/{pattern}")
            # For patterns that could be directories (no wildcards suggesting
            # file extensions), also exclude contents. In gitignore, matching
            # a directory prevents traversal, but pathspec requires explicit
            # /** to match contents (e.g., target/** for target/debug/myapp).
            if "*" not in pattern:
                excludes.append(f":(exclude)**/{pattern}/**")

    return excludes


def decompress_log(compressed_data: str) -> str:
    """Decompress a gzip + base64 encoded string.

    The compression flow is:
    1. UTF-8 encode the string to bytes
    2. gzip compress the bytes
    3. Base64 encode the compressed bytes

    This function reverses that process.

    Args:
        compressed_data: Base64 encoded gzip compressed string.

    Returns:
        Decompressed string, or empty string if decompression fails.
    """
    if not compressed_data:
        return ""

    try:
        # Base64 decode
        compressed_bytes = base64.b64decode(compressed_data)
        # Gzip decompress
        decompressed_bytes = gzip.decompress(compressed_bytes)
        # UTF-8 decode
        return decompressed_bytes.decode("utf-8")
    except Exception as e:
        print(f"Warning: Failed to decompress log: {e}")
        return ""


def get_terminal_solution() -> str | None:
    """Get the solution from the terminal solution file.

    Returns:
        The contents of the solution file, or None if not found.
    """
    solution_path = os.path.join(WORKSPACE_DIR, TERMINAL_SOLUTION_FILE)

    # Also check in task dir if not in workspace
    if not os.path.exists(solution_path):
        from config import TASK_DIR

        solution_path = os.path.join(TASK_DIR, TERMINAL_SOLUTION_FILE)

    if not os.path.exists(solution_path):
        print(f"Error: Solution file not found at {solution_path}")
        return None

    with open(solution_path, "r") as f:
        return f.read()


def filter_binary_diffs(diff_output: str) -> str:
    """Remove binary file diffs from git diff output.

    Binary file diffs look like:
        diff --git a/path/to/file b/path/to/file
        new file mode 100644
        index 0000000..abc1234
        Binary files /dev/null and b/path/to/file differ

    This function removes entire diff entries for binary files.

    Args:
        diff_output: The raw git diff output.

    Returns:
        The diff output with binary file entries removed.
    """
    if not diff_output:
        return diff_output

    # Split by "diff --git" to get individual file diffs
    parts = diff_output.split("diff --git ")

    filtered_parts = []
    for i, part in enumerate(parts):
        if i == 0:
            # First part is anything before the first "diff --git"
            # (usually empty, but preserve if there's content)
            if part:
                filtered_parts.append(part)
            continue

        # Check if this diff entry is for a binary file
        # Binary diffs contain "Binary files ... differ"
        if "Binary files " in part and " differ" in part:
            continue

        # Keep this diff entry, adding back the prefix
        filtered_parts.append("diff --git " + part)

    return "".join(filtered_parts)


def filter_large_diffs(diff_output: str, max_chars: int = 200_000) -> str:
    """Remove individual file diffs that exceed the character limit.

    Args:
        diff_output: The raw git diff output.
        max_chars: Maximum characters allowed per file diff (default 200k).

    Returns:
        The diff output with oversized file entries removed.
    """
    if not diff_output:
        return diff_output

    # Split by "diff --git" to get individual file diffs
    parts = diff_output.split("diff --git ")

    filtered_parts = []
    removed_files = []

    for i, part in enumerate(parts):
        if i == 0:
            # First part is anything before the first "diff --git"
            # (usually empty, but preserve if there's content)
            if part:
                filtered_parts.append(part)
            continue

        # Check if this diff entry exceeds the size limit
        full_diff = "diff --git " + part
        if len(full_diff) > max_chars:
            # Extract file path for logging (first line after "diff --git ")
            first_line = part.split("\n")[0] if part else "unknown"
            removed_files.append((first_line, len(full_diff)))
            continue

        # Keep this diff entry
        filtered_parts.append(full_diff)

    # Log removed files
    if removed_files:
        print(f"\nFiltered out {len(removed_files)} large file diff(s) (>{max_chars} chars):")
        for file_path, size in removed_files:
            print(f"  - {file_path} ({size:,} chars)")

    return "".join(filtered_parts)


def get_code_solution(container_id: str, target_workdir: str) -> str | None:
    """Get the git diff solution from the container.

    Stages all changes and gets the diff from the first commit.
    Excludes files matching patterns from utils/gitignores/*.gitignore.

    Args:
        container_id: The Docker container ID.
        target_workdir: The working directory inside the container.

    Returns:
        The git diff output, or None if failed.
    """
    if not container_id:
        print("Error: No container ID available.")
        return None

    try:
        # Stage all changes
        subprocess.run(
            ["docker", "exec", "-w", target_workdir, container_id, "git", "add", "."],
            capture_output=True,
            text=True,
            check=True,
        )

        # Get the first commit hash
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-w",
                target_workdir,
                container_id,
                "git",
                "rev-list",
                "--max-parents=0",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        first_commit = result.stdout.strip()

        if not first_commit:
            print("Error: Could not find first commit.")
            return None

        # Load gitignore patterns and convert to pathspec excludes
        patterns = load_gitignore_patterns()
        excludes = convert_to_pathspec_excludes(patterns)

        # Build git diff command with excludes
        diff_cmd = [
            "docker",
            "exec",
            "-w",
            target_workdir,
            container_id,
            "git",
            "diff",
            "--cached",
            first_commit,
            "--",
            ".",  # Include all files first
        ]
        # Add exclude pathspecs
        diff_cmd.extend(excludes)

        # Get the diff from the first commit (staged changes)
        result = subprocess.run(
            diff_cmd,
            capture_output=True,
            text=True,
            check=True,
        )

        # Filter out binary file diffs and large file diffs
        filtered = filter_binary_diffs(result.stdout)
        return filter_large_diffs(filtered)

    except subprocess.CalledProcessError as e:
        print(f"Error getting git diff: {e.stderr if e.stderr else e}")
        return None
    except FileNotFoundError:
        print("Error: Docker is not installed or not in PATH.")
        return None


def display_test_results(
    score: float | None,
    rubric_score: float | None,
    test_summary: dict | None,
    rubric_grade_summary: dict | None,
    stdout: str,
    stderr: str,
    log_dir: Path,
    all_empty: bool,
    result_status: str | None = None,
    result_error: str | None = None,
) -> None:
    """Display detailed test results with actionable information.
    
    Args:
        score: Test score (0.0 to 1.0) or None if evaluation failed.
        rubric_score: Rubric score if available.
        test_summary: Test summary dictionary.
        rubric_grade_summary: Rubric grade summary dictionary.
        stdout: Decompressed stdout from test run.
        stderr: Decompressed stderr from test run.
        log_dir: Directory where logs will be saved.
        all_empty: Whether all results are empty.
        result_status: Status of the result (e.g., "failed", "success").
        result_error: Error message if status is "failed".
    """
    print("\n" + "=" * 60)
    
    # Handle explicit failure status with error message
    if result_status == "failed" and result_error:
        print("TEST EXECUTION FAILED")
        print("=" * 60)
        print(f"\nError: {result_error}")
        print("\nThe test execution failed before producing results.")
        print("Check the error message above for details.")
        print("\nIf the issue persists, please contact the Mercor team for assistance.")
        return
    
    if all_empty:
        print("TEST RESULTS HIDDEN")
        print("=" * 60)
        print("\nThe stdout, stderr, and test summary are all empty.")
        print("Test results may be hidden for this benchmark type.")
        return
    
    # Handle case where score is None (evaluation error)
    if score is None:
        print("TEST EVALUATION ERROR")
        print("=" * 60)
        print("\nNo test results returned. Common causes:")
        print("  1. Docker image build failed")
        print("  2. Solution script syntax error")
        print("  3. Task configuration error")
        print("  4. Container timeout or resource issue")
        print("\nNext steps:")
        print(f"  - Check logs at: {log_dir.resolve()}")
        print("  - Verify your_solution.sh has valid bash syntax")
        print("  - Run 'bash -n your_solution.sh' locally to check syntax")
        print("  - Contact engineering if issue persists")
        
        # Save what we have (even if mostly empty) for debugging
        save_logs(log_dir, stdout, stderr, test_summary, rubric_grade_summary)
        if stdout or stderr:
            print(f"\nPartial logs saved to: {log_dir.resolve()}")
        return
    
    # Save logs for all other cases
    save_logs(log_dir, stdout, stderr, test_summary, rubric_grade_summary)

    # Display debug output (solution output, errors, failed tests)
    display_debug_output(stdout, stderr, test_summary)
    
    if score == 1.0:
        print("✅ TESTS PASSED")
        print("=" * 60)
    else:
        print("❌ TESTS FAILED")
        print("=" * 60)
        print(f"\nScore: {score:.0%}")
        
        # Show failed tests if available
        if test_summary:
            statuses = test_summary.get("test_statuses", {})
            failed = [(k, v) for k, v in statuses.items() if v != "pass"]
            
            if failed:
                print(f"\nFailed tests ({len(failed)}):")
                for name, status in failed[:10]:
                    print(f"  ✗ {name}: {status}")
                if len(failed) > 10:
                    print(f"  ... and {len(failed) - 10} more (see logs)")
        
        # Show hint from stderr if there's an obvious error
        if stderr:
            error_hints = _extract_error_hints(stderr)
            if error_hints:
                print("\nPossible issues detected:")
                for hint in error_hints[:3]:
                    print(f"  → {hint}")
    
    # Print rubric score if available
    if rubric_score is not None:
        print()
        if rubric_score == 1.0:
            print("✅ RUBRIC PASSED")
        else:
            print("❌ RUBRIC FAILED")
            print(f"   Rubric Score: {rubric_score:.0%}")
            
            # Show rubric details if available
            if rubric_grade_summary:
                grades = rubric_grade_summary.get("grades", {})
                failed_criteria = [
                    (k, v) for k, v in grades.items() 
                    if isinstance(v, dict) and v.get("score", 1.0) < 1.0
                ]
                if failed_criteria:
                    print("\n   Failed criteria:")
                    for name, data in failed_criteria[:5]:
                        print(f"     ✗ {name}: {data.get('score', 0):.0%}")
    
    print("=" * 60)
    
    # Print log path
    abs_log_dir = log_dir.resolve()
    print(f"\nLogs saved to: {abs_log_dir}")
    print("  - stdout.log")
    print("  - stderr.log")
    print("  - test_summary.json")
    if rubric_grade_summary:
        print("  - rubric_grade_summary.json")


def _extract_error_hints(stderr: str) -> list[str]:
    """Extract actionable hints from stderr.
    
    Args:
        stderr: The stderr content.
        
    Returns:
        List of hint strings.
    """
    hints = []
    stderr_lower = stderr.lower()
    
    error_patterns = {
        "syntax error": "Check your solution for bash syntax errors (run 'bash -n' locally)",
        "command not found": "Required command not available in container",
        "permission denied": "File permission issue - check file permissions",
        "no such file": "File or directory path does not exist",
        "fatal:": "Git operation failed - check git command syntax",
        "merge conflict": "Merge conflict encountered - resolve conflicts manually",
        "cannot lock ref": "Git ref lock issue - another git process may be running",
        "connection refused": "Network connection failed - service may not be running",
        "timeout": "Operation timed out - solution may be too slow",
    }
    
    for pattern, hint in error_patterns.items():
        if pattern in stderr_lower:
            hints.append(hint)
    
    return hints


def save_logs(
    log_dir: Path,
    stdout: str,
    stderr: str,
    test_summary: dict | None,
    rubric_grade_summary: dict | None = None,
) -> None:
    """Save logs to the specified directory.

    Args:
        log_dir: Directory to save logs to.
        stdout: Standard output content.
        stderr: Standard error content.
        test_summary: Test summary dictionary.
        rubric_grade_summary: Rubric grade summary dictionary.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # Save stdout
    with open(log_dir / "stdout.log", "w") as f:
        f.write(stdout)

    # Save stderr
    with open(log_dir / "stderr.log", "w") as f:
        f.write(stderr)

    # Save test summary
    with open(log_dir / "test_summary.json", "w") as f:
        json.dump(test_summary or {}, f, indent=2)

    # Save rubric grade summary if present
    if rubric_grade_summary:
        with open(log_dir / "rubric_grade_summary.json", "w") as f:
            json.dump(rubric_grade_summary, f, indent=2)


def extract_solution_output(stdout: str) -> str:
    """Extract the [apply_solution] section from stdout.

    The stdout contains sections like [apply_solution], [grading_setup_N_of_M],
    [test_run]. This extracts just the solution execution output.

    Args:
        stdout: Full stdout content from test run.

    Returns:
        The content between [apply_solution] header and the next section,
        or empty string if not found.
    """
    if not stdout:
        return ""

    lines = stdout.split('\n')
    in_solution = False
    solution_lines = []

    for line in lines:
        if '[apply_solution]' in line:
            in_solution = True
            continue
        elif in_solution and line.startswith('[') and '(exit code:' in line:
            # Hit the next section header (e.g., [grading_setup_1_of_7] (exit code: 0))
            break
        elif in_solution and line.strip() == '=' * 60:
            # Skip separator lines
            continue
        elif in_solution:
            solution_lines.append(line)

    return '\n'.join(solution_lines).strip()


def display_debug_output(
    stdout: str,
    stderr: str,
    test_summary: dict | None,
) -> None:
    """Display debug output to help users understand what happened.

    Shows:
    - Solution execution output (from [apply_solution] section)
    - Stderr errors (if any meaningful content)
    - Failed test names (from test_summary)

    Args:
        stdout: Full stdout content from test run.
        stderr: Full stderr content from test run.
        test_summary: Test summary dictionary with test_statuses.
    """
    print("\n" + "=" * 60)
    print("DEBUG OUTPUT")
    print("=" * 60)

    # 1. Solution execution output
    solution_output = extract_solution_output(stdout)
    if solution_output:
        print("\n" + "-" * 60)
        print("SOLUTION EXECUTION OUTPUT")
        print("-" * 60)
        print(solution_output)
    else:
        print("\n" + "-" * 60)
        print("SOLUTION EXECUTION OUTPUT")
        print("-" * 60)
        print("(no solution output captured)")

    # 2. Stderr errors (filter out section markers and exit codes)
    if stderr:
        stderr_lines = stderr.strip().split('\n')
        meaningful_lines = []
        for line in stderr_lines:
            stripped = line.strip()
            # Skip empty lines
            if not stripped:
                continue
            # Skip separator lines (===...)
            if stripped == '=' * 60 or stripped == '=' * 50 or (
                len(stripped) > 10 and stripped == '=' * len(stripped)
            ):
                continue
            # Skip section header lines like [apply_solution] (exit code: 0)
            if stripped.startswith('[') and '(exit code:' in stripped:
                continue
            meaningful_lines.append(line)
        
        if meaningful_lines:
            print("\n" + "-" * 60)
            print("ERRORS")
            print("-" * 60)
            print('\n'.join(meaningful_lines))

    # 3. Failed tests
    if test_summary:
        statuses = test_summary.get("test_statuses", {})
        failed_tests = [
            name for name, status in statuses.items() 
            if status != "pass"
        ]
        
        if failed_tests:
            print("\n" + "-" * 60)
            print(f"FAILED TESTS ({len(failed_tests)})")
            print("-" * 60)
            for test_name in failed_tests:
                # Shorten the test name for readability
                short_name = test_name.split("::")[-1] if "::" in test_name else test_name
                print(f"  - {short_name}")


async def _run_ws_session(ws_url: str, prediction: dict) -> WSSessionResult:
    """Run a single WebSocket grading session.

    Returns WSSessionResult with connected=False on connection-level failure
    (suitable for fallback), or populated results on success.
    """
    result = WSSessionResult()
    connection_start_time = datetime.now()
    print(f"Connecting to {ws_url}...")

    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(
                ws_url,
                timeout=aiohttp.ClientTimeout(total=600, sock_read=600),
                heartbeat=30,
                headers=get_api_headers(),
            ) as ws:
                result.connected = True
                ws_connect_duration = (datetime.now() - connection_start_time).total_seconds()
                print(f"Connected! (connection established in {ws_connect_duration:.2f}s)\n")

                ready_msg = await ws.receive_json()
                print(f"[SERVER] {json.dumps(ready_msg, indent=2)}\n")

                print("[CLIENT] Sending prediction...")
                await ws.send_json(prediction)
                print("[CLIENT] Sent!\n")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        msg_type = data.get("type", "unknown")

                        if msg_type == "progress":
                            print(
                                f"[PROGRESS] {data.get('message', '')} "
                                f"({data.get('tasks_completed', 0)}/{data.get('tasks_total', 0)})"
                            )
                        elif msg_type == "error":
                            print(f"[ERROR] {data.get('error', '')}")
                            result.received_result_or_error = True
                        elif msg_type == "result":
                            result.received_result_or_error = True
                            elapsed = (datetime.now() - connection_start_time).total_seconds()
                            print(f"\n[RESULT] (received after {elapsed:.2f}s)")
                            print(f"  Pass rate: {data.get('pass_rate', 0):.2%}")
                            print(f"  Passed: {data.get('passed_count', 0)}/{data.get('total_results', 0)}")
                            print(f"  Duration: {data.get('total_duration_seconds', 0):.2f}s")

                            results = data.get("results", [])
                            if results and len(results) > 0:
                                first_result = results[0]
                                result_data = first_result.get("result")
                                result.result_status = first_result.get("status")
                                result.result_error = first_result.get("error")

                                if result_data is None:
                                    print("  [WARNING] result_data is None - server returned no result details")
                                    result_data = {}

                                result.stdout = decompress_log(result_data.get("stdout_compressed", ""))
                                result.stderr = decompress_log(result_data.get("stderr_compressed", ""))
                                result.test_summary = result_data.get("test_summary", {})
                                result.rubric_grade_summary = result_data.get("rubric_grade_summary")

                                if result.test_summary:
                                    result.score = result.test_summary.get("score")
                                if result.rubric_grade_summary:
                                    result.rubric_score = result.rubric_grade_summary.get("total_score")
                            else:
                                print("  [WARNING] No results in response - results array is empty")

                        elif msg_type == "complete":
                            print(f"\n[COMPLETE] {data.get('message', '')}")
                        else:
                            print(f"[{msg_type.upper()}] {json.dumps(data, indent=2)}")

                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        elapsed = (datetime.now() - connection_start_time).total_seconds()
                        print(f"\n[CONNECTION] Closed by server (total duration: {elapsed:.2f}s)")
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        elapsed = (datetime.now() - connection_start_time).total_seconds()
                        print(f"\n[CONNECTION ERROR] {ws.exception()} (after {elapsed:.2f}s)")
                        break

        except aiohttp.ClientError as e:
            elapsed = (datetime.now() - connection_start_time).total_seconds()
            print(f"Error connecting to WebSocket: {e} (after {elapsed:.2f}s)")
            return result

    elapsed = (datetime.now() - connection_start_time).total_seconds()
    print(f"\n[DEBUG] Total WebSocket session duration: {elapsed:.2f}s")
    return result


async def run_tests():
    """Main function to run tests via WebSocket."""
    # Load session info
    session_info = load_session_info()

    if session_info is None:
        print("Error: No active session found. Run start.py first.")
        return

    print("Session found:")
    print(f"  Baseline Attempt ID: {session_info.baseline_attempt_id}")
    print(
        f"  Container ID: {session_info.container_id[:12] if session_info.container_id else 'N/A'}"
    )
    print(f"  Benchmark Type: {session_info.benchmark_metadata.type}")
    print()

    # Get solution based on benchmark type
    benchmark_type = session_info.benchmark_metadata.type

    if benchmark_type == "terminal":
        print("Getting terminal solution...")
        solution = get_terminal_solution()
    else:  # "code" or default
        print("Getting code solution (git diff)...")
        solution = get_code_solution(
            session_info.container_id,
            session_info.benchmark_metadata.target_workdir,
        )

    if solution is None:
        print("Error: Could not get solution.")
        return

    print(f"Solution length: {len(solution)} characters")
    
    # Save solution to output.log before running tests
    output_log_path = Path("output.log")
    with open(output_log_path, "w") as f:
        f.write(solution)
    print(f"Solution saved to: {output_log_path.resolve()}")
    
    # Validate terminal solutions before sending
    if benchmark_type == "terminal":
        print("\nValidating solution...")
        can_proceed, warnings = validate_terminal_solution(solution)
        
        if warnings:
            print("\n" + "=" * 50)
            print("VALIDATION RESULTS")
            print("=" * 50)
            for warning in warnings:
                print(f"\n{warning}")
            print()
        
        if not can_proceed:
            print("=" * 50)
            print("VALIDATION FAILED - Cannot proceed with tests")
            print("=" * 50)
            print("\nFix the issues above and try again.")
            print("See QUICK_REFERENCE.md for non-interactive command alternatives.")
            return
        
        if not warnings:
            print("  ✓ No issues found")
    
    print()

    # Build prediction
    prediction = {
        "baseline_attempt_id": session_info.baseline_attempt_id,
        "solution": solution,
    }

    # Route to v2 for known benchmark ids, v1 for everything else
    benchmark_id = session_info.benchmark_metadata.benchmark_id
    benchmark_label = benchmark_id or "unknown-benchmark"
    use_v2 = benchmark_id in V2_BENCHMARK_IDS
    result: WSSessionResult | None = None

    if use_v2:
        ws_url = _get_ws_url("v2")
        print(f"Using v2 grading endpoint for {benchmark_label}")
        result = await _run_ws_session(ws_url, prediction)
        if not result.connected:
            print("\nv2 connection failed, falling back to v1...")
            result = None

    if result is None:
        ws_url = _get_ws_url("v1")
        if use_v2:
            print(f"Using v1 grading endpoint (fallback) for {benchmark_label}")
        else:
            print(f"Using v1 grading endpoint for {benchmark_label}")
        result = await _run_ws_session(ws_url, prediction)
        if not result.connected:
            return

    if not result.received_result_or_error:
        print("\n" + "=" * 60)
        print("CONNECTION ISSUE - NO RESULT RECEIVED")
        print("=" * 60)
        print("\nThe WebSocket connection closed without receiving a result or error message.")
        print("This may be caused by:")
        print("  - Server timeout during test execution")
        print("  - Network connectivity issues")
        print("  - Server-side processing error")
        print("\nPlease try again. If the issue persists, contact the Mercor team for assistance.")
        return

    # Create log directory with current datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_dir = Path("logs") / timestamp

    # Check if all results are empty
    all_empty = (
        (not result.stdout)
        and (not result.stderr)
        and (not result.test_summary)
        and (not result.rubric_grade_summary)
    )

    display_test_results(
        score=result.score,
        rubric_score=result.rubric_score,
        test_summary=result.test_summary,
        rubric_grade_summary=result.rubric_grade_summary,
        stdout=result.stdout,
        stderr=result.stderr,
        log_dir=log_dir,
        all_empty=all_empty,
        result_status=result.result_status,
        result_error=result.result_error,
    )

    print("\nDone!")


def main():
    """Entry point."""
    asyncio.run(run_tests())


if __name__ == "__main__":
    main()
