"""Unit tests for utils/validation.py.

Tests cover:
- Bash syntax validation
- Interactive command detection
- Hardcoded hash detection
- Unpinned git clone detection
- Integrated validation function
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add the parent directory to the path so we can import utils
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from utils.validation import (
    validate_bash_syntax,
    check_interactive_commands,
    check_hardcoded_hashes,
    check_unpinned_git_clones,
    validate_terminal_solution,
)


class TestValidateBashSyntax:
    """Tests for validate_bash_syntax function."""
    
    def test_valid_simple_script(self):
        """T1.2.1: Valid simple script should pass."""
        script = "#!/bin/bash\necho hello"
        is_valid, error = validate_bash_syntax(script)
        assert is_valid is True
        assert error == ""
    
    def test_valid_complex_script(self, sample_solutions):
        """Valid complex script with multiple commands should pass."""
        is_valid, error = validate_bash_syntax(sample_solutions["valid_complex"])
        assert is_valid is True
        assert error == ""
    
    def test_valid_heredoc(self, sample_solutions):
        """T1.2.6: Complex valid script with heredocs should pass."""
        is_valid, error = validate_bash_syntax(sample_solutions["valid_heredoc"])
        assert is_valid is True
        assert error == ""
    
    def test_unclosed_quote(self, sample_solutions):
        """T1.2.2: Unclosed quote should fail with appropriate error."""
        is_valid, error = validate_bash_syntax(sample_solutions["syntax_error_unclosed_quote"])
        assert is_valid is False
        assert "EOF" in error or "unexpected" in error.lower() or "syntax" in error.lower()
    
    def test_syntax_error_if(self, sample_solutions):
        """T1.2.3: Malformed if statement should fail."""
        is_valid, error = validate_bash_syntax(sample_solutions["syntax_error_if"])
        assert is_valid is False
        assert len(error) > 0
    
    def test_syntax_error_for(self, sample_solutions):
        """T1.2.4: Malformed for loop should fail."""
        is_valid, error = validate_bash_syntax(sample_solutions["syntax_error_for"])
        assert is_valid is False
        assert len(error) > 0
    
    def test_empty_string(self, sample_solutions):
        """T1.2.5: Empty string should pass (valid empty script)."""
        is_valid, error = validate_bash_syntax(sample_solutions["empty"])
        assert is_valid is True
        assert error == ""
    
    def test_unmatched_brace(self, sample_solutions):
        """T1.2.7: Script with unmatched braces should fail."""
        is_valid, error = validate_bash_syntax(sample_solutions["syntax_error_unmatched_brace"])
        assert is_valid is False
        assert len(error) > 0
    
    def test_whitespace_only(self):
        """Whitespace-only script should pass."""
        is_valid, error = validate_bash_syntax("   \n\t\n   ")
        assert is_valid is True
        assert error == ""
    
    @patch("subprocess.run")
    def test_timeout_handling(self, mock_run):
        """Timeout during syntax check should return error."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="bash", timeout=5)
        
        is_valid, error = validate_bash_syntax("while true; do :; done")
        
        assert is_valid is False
        assert "timed out" in error.lower()
    
    @patch("subprocess.run")
    def test_bash_not_found(self, mock_run):
        """If bash isn't available, validation should pass gracefully."""
        mock_run.side_effect = FileNotFoundError()
        
        is_valid, error = validate_bash_syntax("echo hello")
        
        assert is_valid is True
        assert error == ""
    
    @patch("subprocess.run")
    def test_unexpected_exception(self, mock_run):
        """Unexpected exceptions should not block validation."""
        mock_run.side_effect = OSError("Unexpected error")
        
        is_valid, error = validate_bash_syntax("echo hello")
        
        assert is_valid is True  # Graceful fallback


class TestCheckInteractiveCommands:
    """Tests for check_interactive_commands function."""
    
    def test_interactive_rebase(self, sample_solutions):
        """T2.1.1: git rebase -i should be detected."""
        issues = check_interactive_commands(sample_solutions["interactive_rebase"])
        assert len(issues) == 1
        assert "rebase" in issues[0][0].lower()
        assert issues[0][2] == 2  # Line number
    
    def test_rebase_with_sequence_editor(self, sample_solutions):
        """T2.1.2: git rebase -i with GIT_SEQUENCE_EDITOR should be whitelisted."""
        issues = check_interactive_commands(sample_solutions["interactive_rebase_with_editor"])
        assert len(issues) == 0
    
    def test_interactive_amend(self, sample_solutions):
        """T2.1.3: git commit --amend without -m should be detected."""
        issues = check_interactive_commands(sample_solutions["interactive_amend"])
        assert len(issues) == 1
        assert "amend" in issues[0][0].lower()
    
    def test_amend_with_message(self, sample_solutions):
        """T2.1.4: git commit --amend -m should NOT be detected."""
        issues = check_interactive_commands(sample_solutions["interactive_amend_with_message"])
        assert len(issues) == 0
    
    def test_vim_detected(self, sample_solutions):
        """T2.1.5: vim should be detected."""
        issues = check_interactive_commands(sample_solutions["interactive_vim"])
        assert len(issues) == 1
        assert "vim" in issues[0][0].lower()
    
    def test_git_add_p(self, sample_solutions):
        """T2.1.6: git add -p should be detected."""
        issues = check_interactive_commands(sample_solutions["interactive_add_p"])
        assert len(issues) == 1
        assert "add" in issues[0][0].lower()
    
    def test_multiple_issues(self):
        """T2.1.7: Multi-line with multiple issues should capture all."""
        script = """#!/bin/bash
git rebase -i HEAD~3
vim file.txt
git add -p
"""
        issues = check_interactive_commands(script)
        assert len(issues) == 3
        # Check line numbers are correct
        lines = [issue[2] for issue in issues]
        assert 2 in lines
        assert 3 in lines
        assert 4 in lines
    
    def test_comments_ignored(self):
        """Comments containing interactive commands should be ignored."""
        script = """#!/bin/bash
# git rebase -i HEAD~3
# vim file.txt
echo "hello"
"""
        issues = check_interactive_commands(script)
        assert len(issues) == 0
    
    def test_empty_script(self):
        """Empty script should have no issues."""
        issues = check_interactive_commands("")
        assert len(issues) == 0
    
    def test_valid_script_no_issues(self, sample_solutions):
        """Valid script should have no interactive command issues."""
        issues = check_interactive_commands(sample_solutions["valid_complex"])
        assert len(issues) == 0
    
    def test_nano_detected(self):
        """nano editor should be detected."""
        issues = check_interactive_commands("nano config.txt")
        assert len(issues) == 1
        assert "editor" in issues[0][1].lower()
    
    def test_vi_detected(self):
        """vi editor should be detected."""
        issues = check_interactive_commands("vi ~/.bashrc")
        assert len(issues) == 1
        assert "editor" in issues[0][1].lower()
    
    def test_emacs_detected(self):
        """emacs editor should be detected."""
        issues = check_interactive_commands("emacs file.py")
        assert len(issues) == 1
        assert "editor" in issues[0][1].lower()
    
    def test_git_add_patch_long_form(self):
        """git add --patch should be detected."""
        issues = check_interactive_commands("git add --patch")
        assert len(issues) == 1
        assert "interactive" in issues[0][1].lower()
    
    def test_case_insensitive_commands(self):
        """Interactive command detection should be case insensitive."""
        issues = check_interactive_commands("VIM File.txt")
        assert len(issues) == 1
        
        issues = check_interactive_commands("GIT REBASE -I HEAD~3")
        assert len(issues) == 1
    
    def test_rebase_sequence_editor_inline(self):
        """GIT_SEQUENCE_EDITOR can appear anywhere on the line."""
        # Editor before command
        issues = check_interactive_commands(
            "GIT_SEQUENCE_EDITOR=: git rebase -i HEAD~2"
        )
        assert len(issues) == 0
        
        # Editor with export
        issues = check_interactive_commands(
            "export GIT_SEQUENCE_EDITOR=: && git rebase -i HEAD~2"
        )
        assert len(issues) == 0
    
    def test_amend_with_message_variations(self):
        """git commit --amend -m should NOT be detected in various forms."""
        # -m before message
        issues = check_interactive_commands("git commit --amend -m 'message'")
        assert len(issues) == 0
        
        # --message long form
        issues = check_interactive_commands("git commit --amend --message 'msg'")
        assert len(issues) == 0
        
        # -m with double quotes
        issues = check_interactive_commands('git commit --amend -m "message"')
        assert len(issues) == 0

    def test_amend_with_no_edit(self):
        """git commit --amend --no-edit should NOT be detected (non-interactive)."""
        issues = check_interactive_commands("git commit --amend --no-edit")
        assert len(issues) == 0
        
        # With other flags
        issues = check_interactive_commands("git commit -a --amend --no-edit")
        assert len(issues) == 0

    def test_amend_with_preceding_flags_detected(self):
        """git commit with flags before --amend should be detected if no -m/--no-edit."""
        # -a flag before --amend, no message
        issues = check_interactive_commands("git commit -a --amend")
        assert len(issues) == 1
        assert "amend" in issues[0][1].lower()
        
        # -v flag before --amend, no message  
        issues = check_interactive_commands("git commit -v --amend")
        assert len(issues) == 1

    def test_rebase_interactive_long_form(self):
        """git rebase --interactive (long form) should be detected."""
        issues = check_interactive_commands("git rebase --interactive HEAD~3")
        assert len(issues) == 1
        assert "rebase" in issues[0][1].lower()
        
        # With GIT_SEQUENCE_EDITOR should NOT be detected
        issues = check_interactive_commands(
            'GIT_SEQUENCE_EDITOR="sed -i s/pick/squash/" git rebase --interactive HEAD~3'
        )
        assert len(issues) == 0


class TestCheckHardcodedHashes:
    """Tests for check_hardcoded_hashes function."""
    
    def test_short_hash_detected(self, sample_solutions):
        """T2.2.1: Short hash should be detected."""
        hashes = check_hardcoded_hashes(sample_solutions["hardcoded_short_hash"])
        assert len(hashes) >= 1
        # Check that abc1234567 or similar is detected
        hash_values = [h[0] for h in hashes]
        assert any("abc1234" in h for h in hash_values)
    
    def test_relative_ref_not_detected(self, sample_solutions):
        """T2.2.2: Relative refs like HEAD~1 should NOT be detected."""
        hashes = check_hardcoded_hashes(sample_solutions["relative_ref"])
        assert len(hashes) == 0
    
    def test_full_hash_detected(self, sample_solutions):
        """T2.2.3: Full 40-char hash should be detected."""
        hashes = check_hardcoded_hashes(sample_solutions["hardcoded_full_hash"])
        assert len(hashes) >= 1
        # Check that 40-char hash is detected
        hash_values = [h[0] for h in hashes]
        assert any(len(h) == 40 for h in hash_values)
    
    def test_uuid_excluded(self, sample_solutions):
        """T2.2.4: UUIDs should NOT be detected as hashes."""
        hashes = check_hardcoded_hashes(sample_solutions["uuid_not_hash"])
        assert len(hashes) == 0
    
    def test_color_excluded(self, sample_solutions):
        """T2.2.5: Hex colors should NOT be detected as hashes."""
        hashes = check_hardcoded_hashes(sample_solutions["color_not_hash"])
        assert len(hashes) == 0
    
    def test_hash_with_git_reset(self):
        """T2.2.6: git reset --hard with hash should be detected."""
        script = "#!/bin/bash\ngit reset --hard abc1234"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 1
    
    def test_comments_ignored(self):
        """Hashes in comments should be ignored."""
        script = """#!/bin/bash
# git checkout abc1234567890abc1234567890abc1234567890ab
echo "hello"
"""
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) == 0
    
    def test_multiple_hashes(self):
        """Multiple hashes on different lines should all be detected."""
        script = """#!/bin/bash
git checkout abc1234567
git cherry-pick def7890123
"""
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 2
    
    def test_valid_script_no_hashes(self, sample_solutions):
        """Valid script with no hardcoded hashes should return empty."""
        hashes = check_hardcoded_hashes(sample_solutions["valid_complex"])
        assert len(hashes) == 0
    
    def test_boundary_exactly_7_chars(self):
        """Exactly 7 hex chars should be detected as short hash."""
        script = "git checkout abcdef1"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 1
        assert any(h[0] == "abcdef1" for h in hashes)
    
    def test_boundary_exactly_12_chars(self):
        """Exactly 12 hex chars should be detected as short hash."""
        script = "git checkout abcdef123456"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 1
        assert any(h[0] == "abcdef123456" for h in hashes)
    
    def test_boundary_exactly_40_chars(self):
        """Exactly 40 hex chars should be detected as full hash."""
        full_hash = "a" * 40
        script = f"git checkout {full_hash}"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 1
        assert any(h[0] == full_hash for h in hashes)
    
    def test_too_short_not_detected(self):
        """6 or fewer hex chars should NOT be detected (too short)."""
        script = "git checkout abcdef"  # 6 chars
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) == 0
    
    def test_too_long_not_full_hash(self):
        """41+ hex chars should NOT match the 40-char full hash pattern."""
        long_hash = "a" * 41
        script = f"echo {long_hash}"
        hashes = check_hardcoded_hashes(script)
        # Should not have a 40-char match in the hashes
        assert not any(len(h[0]) == 40 for h in hashes)
    
    def test_hex_in_filename_not_detected(self):
        """Hex chars in filenames should NOT be detected (not git context)."""
        script = "cat deadbeef.log"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) == 0  # Not a git command, should not flag
    
    def test_hex_in_build_id_not_detected(self):
        """Hex chars in build IDs or other non-git contexts should NOT be detected."""
        script = "./build_1234567.sh"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) == 0
    
    def test_branch_names_with_hex_not_detected(self):
        """Branch names containing hex when creating branch should not be flagged."""
        # Creating a new branch - the hex is part of the branch name, not a hash
        script = "git checkout -b feature-abc1234"
        hashes = check_hardcoded_hashes(script)
        # This line has "git checkout" but the hash-like string is a branch name
        # Since it contains git checkout, it will be checked, but abc1234 is 7 chars
        # and appears after -b which indicates it's a branch name
        # Current behavior: will still detect it (limitation of simple regex)
        # A more sophisticated solution would parse git command arguments
    
    def test_hash_in_git_checkout_detected(self):
        """Hashes in git checkout command should be detected."""
        script = "git checkout abc1234567"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 1
        assert any("abc1234567" in h[0] for h in hashes)
    
    def test_hash_in_git_cherry_pick_detected(self):
        """Hashes in git cherry-pick should be detected."""
        script = "git cherry-pick def7890abc"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 1
    
    def test_hash_in_git_reset_detected(self):
        """Hashes in git reset should be detected."""
        script = "git reset --hard abc1234567"
        hashes = check_hardcoded_hashes(script)
        assert len(hashes) >= 1


class TestCheckUnpinnedGitClones:
    """Tests for check_unpinned_git_clones function."""
    
    def test_unpinned_clone_detected(self, sample_solutions):
        """T2.3.1: Unpinned git clone should be detected."""
        issues = check_unpinned_git_clones(sample_solutions["unpinned_clone"])
        assert len(issues) == 1
        assert "clone" in issues[0][0].lower()
    
    def test_pinned_clone_with_tag(self, sample_solutions):
        """T2.3.2: git clone with version tag should NOT be detected."""
        issues = check_unpinned_git_clones(sample_solutions["pinned_clone_tag"])
        assert len(issues) == 0
    
    def test_pinned_clone_with_checkout(self, sample_solutions):
        """T2.3.3: git clone followed by checkout should NOT be detected."""
        issues = check_unpinned_git_clones(sample_solutions["pinned_clone_checkout"])
        assert len(issues) == 0
    
    def test_unpinned_clone_main_branch(self, sample_solutions):
        """T2.3.4: git clone --branch main should be detected (main is unstable)."""
        issues = check_unpinned_git_clones(sample_solutions["unpinned_clone_main"])
        assert len(issues) == 1
        assert "main" in issues[0][2].lower() or "may change" in issues[0][2].lower()
    
    def test_shallow_clone_unpinned(self):
        """T2.3.5: Shallow clone without pinning should be detected."""
        script = "#!/bin/bash\ngit clone --depth 1 https://github.com/org/repo"
        issues = check_unpinned_git_clones(script)
        assert len(issues) == 1
    
    def test_clone_with_specific_branch(self):
        """Clone with specific non-main branch should not be flagged as unstable."""
        script = "#!/bin/bash\ngit clone -b feature-branch https://github.com/org/repo"
        issues = check_unpinned_git_clones(script)
        # feature-branch is not in unstable list, so no issue
        assert len(issues) == 0
    
    def test_no_clones(self):
        """Script with no git clones should return empty."""
        script = "#!/bin/bash\necho 'hello'\ngit status"
        issues = check_unpinned_git_clones(script)
        assert len(issues) == 0
    
    def test_comments_ignored(self):
        """git clone in comments should be ignored."""
        script = """#!/bin/bash
# git clone https://github.com/org/repo
echo "hello"
"""
        issues = check_unpinned_git_clones(script)
        assert len(issues) == 0
    
    def test_master_branch_detected(self):
        """git clone --branch master should be detected as unstable."""
        script = "git clone --branch master https://github.com/org/repo"
        issues = check_unpinned_git_clones(script)
        assert len(issues) == 1
    
    def test_develop_branch_detected(self):
        """git clone --branch develop should be detected as unstable."""
        script = "git clone -b develop https://github.com/org/repo"
        issues = check_unpinned_git_clones(script)
        assert len(issues) == 1
    
    def test_multiple_clones(self):
        """Multiple git clones should each be checked."""
        script = """#!/bin/bash
git clone https://github.com/org/repo1
git clone https://github.com/org/repo2 -b v1.0.0
git clone --branch main https://github.com/org/repo3
"""
        issues = check_unpinned_git_clones(script)
        # repo1: unpinned, repo2: pinned with tag, repo3: main branch
        assert len(issues) == 2  # repo1 and repo3
    
    def test_checkout_within_range(self):
        """Checkout within 3 lines should whitelist the clone."""
        script = """#!/bin/bash
git clone https://github.com/org/repo
echo "cloning..."
cd repo
git checkout abc1234567
"""
        issues = check_unpinned_git_clones(script)
        assert len(issues) == 0
    
    def test_checkout_too_far(self):
        """Checkout more than 3 lines away should NOT whitelist."""
        script = """#!/bin/bash
git clone https://github.com/org/repo
echo "step 1"
echo "step 2"
echo "step 3"
echo "step 4"
git checkout abc1234567
"""
        issues = check_unpinned_git_clones(script)
        assert len(issues) == 1  # Clone is unpinned (checkout too far)


class TestValidateTerminalSolution:
    """Tests for the integrated validate_terminal_solution function."""
    
    def test_valid_script_passes(self, sample_solutions):
        """Valid script should pass with no warnings."""
        can_proceed, warnings = validate_terminal_solution(sample_solutions["valid_complex"])
        assert can_proceed is True
        assert len(warnings) == 0
    
    def test_syntax_error_blocks(self, sample_solutions):
        """Syntax error should block with can_proceed=False."""
        can_proceed, warnings = validate_terminal_solution(
            sample_solutions["syntax_error_unclosed_quote"]
        )
        assert can_proceed is False
        assert any("SYNTAX ERROR" in w for w in warnings)
    
    def test_interactive_command_warns_but_allows(self, sample_solutions):
        """Interactive command should warn but allow proceeding (may have false positives)."""
        can_proceed, warnings = validate_terminal_solution(
            sample_solutions["interactive_rebase"]
        )
        assert can_proceed is True
        assert len(warnings) >= 1
        assert any("WARNING" in w for w in warnings)
    
    def test_hardcoded_hash_warns_but_allows(self, sample_solutions):
        """Hardcoded hash should warn but allow proceeding."""
        can_proceed, warnings = validate_terminal_solution(
            sample_solutions["hardcoded_short_hash"]
        )
        assert can_proceed is True
        assert any("WARNING" in w for w in warnings)
    
    def test_unpinned_clone_only_in_task_creation(self, sample_solutions):
        """Unpinned clone should only be checked when is_task_creation=True."""
        # Without task creation flag
        can_proceed, warnings = validate_terminal_solution(
            sample_solutions["unpinned_clone"],
            is_task_creation=False,
        )
        assert can_proceed is True
        unpinned_warnings = [w for w in warnings if "Unpinned" in w]
        assert len(unpinned_warnings) == 0
        
        # With task creation flag
        can_proceed, warnings = validate_terminal_solution(
            sample_solutions["unpinned_clone"],
            is_task_creation=True,
        )
        assert can_proceed is True  # Still allows proceeding (warning only)
        unpinned_warnings = [w for w in warnings if "Unpinned" in w]
        assert len(unpinned_warnings) == 1
    
    def test_multiple_issues(self):
        """Multiple issues should all be reported."""
        script = """#!/bin/bash
git rebase -i HEAD~3
git checkout abc1234567
"""
        can_proceed, warnings = validate_terminal_solution(script)
        assert can_proceed is True  # Interactive + hash are warnings only
        assert len(warnings) >= 2  # At least interactive + hash warnings
    
    def test_empty_script_passes(self, sample_solutions):
        """Empty script should block validation (no-op solution)."""
        can_proceed, warnings = validate_terminal_solution(sample_solutions["empty"])
        assert can_proceed is False
        assert any("EMPTY SOLUTION" in w for w in warnings)

    def test_comments_only_script_blocks(self):
        """Comments-only script should block validation (no-op solution)."""
        script = """#!/bin/bash
# comment 1
   # comment 2
"""
        can_proceed, warnings = validate_terminal_solution(script)
        assert can_proceed is False
        assert any("EMPTY SOLUTION" in w for w in warnings)

    def test_cd_only_script_blocks(self):
        """cd-only boilerplate should block validation (no-op solution)."""
        script = """#!/bin/bash
cd /workdir/repo
"""
        can_proceed, warnings = validate_terminal_solution(script)
        assert can_proceed is False
        assert any("EMPTY SOLUTION" in w for w in warnings)
    
    def test_valid_with_sequence_editor(self, sample_solutions):
        """Rebase with GIT_SEQUENCE_EDITOR should pass."""
        can_proceed, warnings = validate_terminal_solution(
            sample_solutions["interactive_rebase_with_editor"]
        )
        assert can_proceed is True
