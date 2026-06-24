"""Validation utilities for detecting common solution mistakes.

This module provides functions to validate bash scripts before submission,
detecting issues that would cause silent failures or hangs during evaluation.
"""

import re
import subprocess
from typing import List, Tuple


def validate_bash_syntax(solution: str) -> Tuple[bool, str]:
    """Validate bash syntax using bash -n (syntax check only).
    
    Args:
        solution: The bash script content to validate.
        
    Returns:
        Tuple of (is_valid, error_message).
        If is_valid is True, error_message will be empty.
        If is_valid is False, error_message contains the bash error.
    """
    if not solution or not solution.strip():
        # Empty scripts are technically valid
        return True, ""
    
    try:
        result = subprocess.run(
            ["bash", "-n"],
            input=solution,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False, result.stderr.strip()
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Syntax check timed out"
    except FileNotFoundError:
        # bash not available, skip validation
        return True, ""
    except Exception as e:
        # Don't block on unexpected errors
        return True, ""


# Patterns for interactive commands that will hang in non-interactive Docker containers
# Each tuple: (pattern, message, exclusion_pattern)
# If exclusion_pattern is provided and matches, the issue is not reported
INTERACTIVE_PATTERNS = [
    # git rebase -i/--interactive without GIT_SEQUENCE_EDITOR
    (r'\bgit\s+rebase\s+(-i|--interactive)\b', 
     'git rebase -i requires GIT_SEQUENCE_EDITOR. Example: GIT_SEQUENCE_EDITOR="sed -i \'s/pick/squash/\'" git rebase -i HEAD~3',
     r'GIT_SEQUENCE_EDITOR'),
    # git commit --amend without -m/--message or --no-edit (will open editor)
    # Matches --amend anywhere in the command (e.g., git commit -a --amend)
    (r'\bgit\s+commit\b.*--amend\b', 
     'git commit --amend requires -m or --no-edit flag for non-interactive use. Example: git commit --amend -m "message" or git commit --amend --no-edit',
     r'(-m\s|--message\s|--no-edit)'),
    # Interactive editors
    (r'\b(vim|nano|vi|emacs)\s+', 
     'Interactive editors not available in Docker - use sed or echo instead',
     None),
    # git add -p (interactive patch mode)
    (r'\bgit\s+add\s+-p\b', 
     'git add -p is interactive - use git add <file> instead',
     None),
    # git add --patch
    (r'\bgit\s+add\s+--patch\b', 
     'git add --patch is interactive - use git add <file> instead',
     None),
]


def check_interactive_commands(solution: str) -> List[Tuple[str, str, int]]:
    """Detect interactive commands that will hang in Docker.
    
    Args:
        solution: The bash script content to check.
        
    Returns:
        List of (matched_text, warning_message, line_number) tuples.
        Empty list if no interactive commands found.
    """
    issues = []
    lines = solution.split('\n')
    
    for line_num, line in enumerate(lines, 1):
        # Skip comments and empty lines
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
            
        for pattern, message, exclusion in INTERACTIVE_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                # Check if exclusion pattern is present (allows the command)
                if exclusion and re.search(exclusion, line, re.IGNORECASE):
                    continue
                issues.append((stripped, message, line_num))
                break  # Only report one issue per line
    
    return issues


def check_hardcoded_hashes(solution: str) -> List[Tuple[str, int]]:
    """Detect potential hardcoded commit hashes in git commands.
    
    Commit hashes differ between container runs, so hardcoded hashes
    will cause solutions to fail on fresh containers.
    
    Only detects hashes in git command context to avoid false positives
    from hex strings in filenames, build IDs, etc.
    
    Args:
        solution: The bash script content to check.
        
    Returns:
        List of (hash, line_number) tuples.
        Empty list if no hardcoded hashes found.
    """
    # Full SHA-1 (40 hex chars)
    full_sha_pattern = r'\b([0-9a-f]{40})\b'
    # Abbreviated commit hash (7-12 hex chars)
    short_sha_pattern = r'\b([0-9a-f]{7,12})\b'
    
    # Git commands that typically take commit hashes as arguments
    git_hash_commands = r'\bgit\s+(checkout|cherry-pick|reset|revert|show|diff|log|rebase|merge|branch|tag)\b'
    
    # Exclusion patterns - things that look like hashes but aren't
    uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}'
    color_pattern = r'#[0-9a-f]{6}\b'
    
    hashes = []
    lines = solution.split('\n')
    
    for line_num, line in enumerate(lines, 1):
        # Skip comments
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
            
        # Skip if line contains exclusion patterns
        if re.search(uuid_pattern, line, re.IGNORECASE):
            continue
        if re.search(color_pattern, line, re.IGNORECASE):
            continue
        
        # Full 40-char hashes are almost always commit hashes, detect anywhere
        for match in re.finditer(full_sha_pattern, line, re.IGNORECASE):
            hashes.append((match.group(1), line_num))
        
        # Short hashes: only detect in git command context to avoid false positives
        # (e.g., filenames like "deadbeef.log" or build IDs)
        if re.search(git_hash_commands, line, re.IGNORECASE):
            for match in re.finditer(short_sha_pattern, line, re.IGNORECASE):
                candidate = match.group(1)
                
                # Skip if it looks like a version number context
                if re.search(r'\d+\.\d+', line):
                    continue
                
                # Skip if already captured as full hash
                if any(candidate in h[0] for h in hashes):
                    continue
                    
                hashes.append((candidate, line_num))
    
    return hashes


def check_unpinned_git_clones(script: str) -> List[Tuple[str, int, str]]:
    """Detect git clone commands that don't pin to a specific commit/tag.
    
    Task creators should pin git clones to specific commits to ensure
    reproducibility across runs.
    
    Args:
        script: The bash script content to check.
        
    Returns:
        List of (matched_line, line_number, suggestion) tuples.
        Empty list if all git clones are properly pinned.
    """
    issues = []
    lines = script.split('\n')
    
    # Pattern for checkout with commit hash (to check if clone is followed by checkout)
    checkout_after_clone = r'git\s+checkout\s+[a-f0-9]{7,40}'
    
    for i, line in enumerate(lines):
        line_num = i + 1
        stripped = line.strip()
        
        # Skip comments and empty lines
        if not stripped or stripped.startswith('#'):
            continue
        
        # Check for git clone
        if not re.search(r'\bgit\s+clone\b', stripped, re.IGNORECASE):
            continue
        
        # Check if it has -b or --branch with a tag/version (like v1.2.3)
        has_version_tag = re.search(r'(-b|--branch)\s+v?\d+\.', stripped)
        
        # Check if it has --branch with any value (might still be unstable like 'main')
        has_branch_flag = re.search(r'(-b|--branch)\s+\S+', stripped)
        branch_match = re.search(r'(-b|--branch)\s+(\S+)', stripped)
        
        # If branch is 'main', 'master', 'develop', etc., it's not pinned
        unstable_branches = {'main', 'master', 'develop', 'dev', 'trunk', 'HEAD'}
        is_unstable_branch = False
        if branch_match:
            branch_name = branch_match.group(2)
            if branch_name.lower() in unstable_branches:
                is_unstable_branch = True
        
        # Look ahead for checkout with commit hash (within next 3 lines)
        has_checkout = False
        for j in range(i + 1, min(i + 4, len(lines))):
            if re.search(checkout_after_clone, lines[j], re.IGNORECASE):
                has_checkout = True
                break
        
        # Report issue if:
        # - No branch flag at all
        # - Branch flag with unstable branch name
        # - No version tag AND no checkout following
        if not has_branch_flag and not has_checkout:
            issues.append((
                stripped,
                line_num,
                "Pin to exact commit: git clone <repo> && cd <dir> && git checkout <commit-sha>"
            ))
        elif is_unstable_branch and not has_checkout:
            issues.append((
                stripped,
                line_num,
                f"Branch '{branch_match.group(2)}' may change. Pin to exact commit or version tag."
            ))
        elif has_branch_flag and not has_version_tag and not has_checkout and not is_unstable_branch:
            # Has a branch but not a version tag - might be okay, just warn
            pass  # Don't warn for specific non-unstable branches
    
    return issues


def validate_terminal_solution(
    solution: str, 
    is_task_creation: bool = False
) -> Tuple[bool, List[str]]:
    """Run all validations on a terminal solution.
    
    Args:
        solution: The bash script content to validate.
        is_task_creation: If True, also check for unpinned git clones.
        
    Returns:
        Tuple of (can_proceed, list_of_warnings).
        can_proceed is False if there are blocking issues (syntax errors,
        interactive commands).
    """
    warnings = []
    can_proceed = True
    
    # 1. Bash syntax (blocking)
    is_valid, error = validate_bash_syntax(solution)
    if not is_valid:
        warnings.append(f"SYNTAX ERROR: {error}")
        can_proceed = False

    # 1.5 Empty/no-op solution (blocking)
    # Require at least one non-comment, non-empty command beyond the shebang,
    # and beyond a lone "cd /workdir/repo" (common boilerplate).
    meaningful_lines = []
    for raw in solution.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            continue
        meaningful_lines.append(stripped)

    non_boilerplate = [l for l in meaningful_lines if l != "cd /workdir/repo"]
    if len(non_boilerplate) == 0:
        warnings.append(
            "EMPTY SOLUTION: your_solution.sh contains no executable commands.\n"
            "  Add the full set of commands needed to solve the task from scratch."
        )
        can_proceed = False
    
    # 2. Interactive commands (warning only - may have false positives)
    interactive = check_interactive_commands(solution)
    for text, msg, line in interactive:
        warnings.append(f"WARNING Line {line}: {msg}\n  Found: {text}")
    
    # 3. Hardcoded hashes (warning only)
    hashes = check_hardcoded_hashes(solution)
    if hashes:
        hash_list = ", ".join(h[0][:8] + "..." for h in hashes[:3])
        more = f" (and {len(hashes) - 3} more)" if len(hashes) > 3 else ""
        warnings.append(
            f"WARNING: Possible hardcoded hashes detected: {hash_list}{more}\n"
            f"  Commit hashes differ between runs. Use HEAD~N or branch names instead."
        )
    
    # 4. Unpinned git clones (warning for task creation)
    if is_task_creation:
        unpinned = check_unpinned_git_clones(solution)
        for text, line, suggestion in unpinned:
            warnings.append(
                f"WARNING Line {line}: Unpinned git clone detected\n"
                f"  Found: {text}\n"
                f"  {suggestion}"
            )
    
    return can_proceed, warnings
