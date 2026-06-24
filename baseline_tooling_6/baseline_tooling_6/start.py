#!/usr/bin/env python3
"""Start script for baseline tooling - initializes a new baseline attempt session."""

import os
import subprocess

import requests

from config import BASE_URL, TASK_DIR, WORKSPACE_DIR, TERMINAL_SOLUTION_FILE, CUSTOM_START_COMMAND_SCRIPT, get_api_headers
from utils.session_management import (
    SessionInfo,
    BenchmarkMetadata,
    save_session_info,
    load_session_info,
)
from utils.env_utils import (
    create_gitignore,
    ensure_git_config,
    init_git_repo,
    download_and_extract_zip,
    delete_files,
    force_rmtree,
    build_docker_image,
    extract_dir_from_image,
    run_docker_container,
    stop_and_remove_container,
)


def run_container_diagnostics(container_id: str, target_workdir: str) -> dict:
    """Run diagnostics on container and return status dict.
    
    Args:
        container_id: The Docker container ID.
        target_workdir: The working directory inside the container.
        
    Returns:
        Dictionary with diagnostic results:
        - git: Whether git is available
        - workdir: Whether the working directory exists
        - safe_dir: Whether safe.directory is configured
    """
    diagnostics = {"git": False, "workdir": False, "safe_dir": False}
    
    if not container_id:
        return diagnostics
    
    # Check git is available
    result = subprocess.run(
        ["docker", "exec", container_id, "git", "--version"],
        capture_output=True,
        text=True,
    )
    diagnostics["git"] = result.returncode == 0
    
    # Check workdir exists
    if target_workdir:
        result = subprocess.run(
            ["docker", "exec", container_id, "test", "-d", target_workdir],
            capture_output=True,
        )
        diagnostics["workdir"] = result.returncode == 0
    
        # Check safe.directory is configured
        result = subprocess.run(
            ["docker", "exec", container_id, "git", "config", "--global", 
             "--get-all", "safe.directory"],
            capture_output=True,
            text=True,
        )
        diagnostics["safe_dir"] = target_workdir in result.stdout
    
    return diagnostics


def print_diagnostics(diagnostics: dict, target_workdir: str) -> None:
    """Print container diagnostics in a user-friendly format.
    
    Args:
        diagnostics: Dictionary from run_container_diagnostics.
        target_workdir: The target working directory.
    """
    print("\nContainer Diagnostics:")
    
    status_icon = lambda ok: "✓" if ok else "✗"
    
    print(f"  {status_icon(diagnostics['git'])} Git available")
    if target_workdir:
        print(f"  {status_icon(diagnostics['workdir'])} Working directory exists ({target_workdir})")
        print(f"  {status_icon(diagnostics['safe_dir'])} Git safe.directory configured")
    
    # Provide hints for failures
    if not diagnostics["git"]:
        print("\n  ⚠ Git is not available in the container.")
        print("    Check that the Dockerfile installs git.")
    
    if target_workdir and not diagnostics["workdir"]:
        print(f"\n  ⚠ Working directory {target_workdir} does not exist.")
        print("    Check the target_workdir in your task configuration.")
    
    if target_workdir and not diagnostics["safe_dir"]:
        print("\n  ⚠ Git safe.directory not configured.")
        print("    This may cause 'dubious ownership' errors.")
        print(f"    Run: git config --global --add safe.directory {target_workdir}")


def confirm_action(message: str) -> bool:
    """Ask user for confirmation.
    
    Args:
        message: The confirmation message to display.
        
    Returns:
        True if user confirms, False otherwise.
    """
    response = input(f"{message} (yes/no): ").strip().lower()
    return response in ("yes", "y")


def handle_existing_session() -> tuple[bool, str | None]:
    """Handle the case where a session already exists.
    
    Returns:
        Tuple of (should_proceed, baseline_attempt_id).
        If should_proceed is False, the script should exit.
        If baseline_attempt_id is None, a new one should be requested.
    """
    existing_session = load_session_info()
    
    if existing_session is None:
        # No existing session, proceed with new baseline attempt
        return True, None
    
    print("\nExisting session found:")
    print(f"  Baseline Attempt ID: {existing_session.baseline_attempt_id}")
    print(f"  Container ID: {existing_session.container_id}")
    print()
    
    # Ask what they want to do
    print("What would you like to do?")
    print("  1. Reset current task (keep same baseline attempt)")
    print("  2. Start a new baseline attempt")
    print("  3. Cancel")
    
    choice = input("\nEnter choice (1/2/3): ").strip()
    
    if choice == "1":
        # Reset current task
        if not confirm_action("Are you sure? This will delete all your progress"):
            print("Operation cancelled.")
            return False, None
        # Stop and remove the existing container
        if existing_session.container_id:
            stop_and_remove_container(existing_session.container_id)
        return True, existing_session.baseline_attempt_id
    
    elif choice == "2":
        # New baseline attempt
        if not confirm_action("Are you sure? This will delete all your progress"):
            print("Operation cancelled.")
            return False, None
        # Stop and remove the existing container
        if existing_session.container_id:
            stop_and_remove_container(existing_session.container_id)
        return True, None
    
    else:
        print("Operation cancelled.")
        return False, None


def call_start_api(baseline_attempt_id: str) -> dict | None:
    """Call the /internal/code-data/v1/baseline/start endpoint.
    
    Args:
        baseline_attempt_id: The baseline attempt ID to use.
        
    Returns:
        API response data or None on error.
    """
    url = f"{BASE_URL.rstrip('/')}/internal/code-data/v1/baseline/start"
    
    try:
        headers = {"Content-Type": "application/json"}
        headers.update(get_api_headers())
        response = requests.post(
            url,
            json={"baseline_attempt_id": baseline_attempt_id},
            headers=headers,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error calling API: {e}")
        return None


def main():
    """Main entry point for starting a baseline attempt."""
    # Check for existing session
    should_proceed, baseline_attempt_id = handle_existing_session()
    
    if not should_proceed:
        return
    
    # If no baseline_attempt_id from existing session, prompt for one
    if baseline_attempt_id is None:
        baseline_attempt_id = input("Enter baseline_attempt_id: ").strip()
        
        if not baseline_attempt_id:
            print("Error: baseline_attempt_id cannot be empty.")
            return
    
    # Call the API
    print(f"\nCalling start API for baseline attempt: {baseline_attempt_id}")
    data = call_start_api(baseline_attempt_id)
    
    if data is None:
        return
    
    task_zip_url = data.get("task_zip_url")
    benchmark_metadata_raw = data.get("benchmark_metadata", {})
    
    if not task_zip_url:
        print("Error: No task_zip_url in API response.")
        return
    
    # Download and extract the task zip
    if not download_and_extract_zip(task_zip_url, TASK_DIR):
        return
    
    # Create BenchmarkMetadata from response
    benchmark_metadata = BenchmarkMetadata(
        files_to_delete=benchmark_metadata_raw.get("files_to_delete", []),
        type=benchmark_metadata_raw.get("type", "code"),
        target_workdir=benchmark_metadata_raw.get("target_workdir", ""),
        benchmark_id=benchmark_metadata_raw.get("benchmark_id", ""),
        dockerfile_path_override=benchmark_metadata_raw.get("dockerfile_path_override"),
    )
    
    # Track whether sensitive files have been deleted (for finally block safety net)
    files_deleted = False
    
    # Wrap remaining setup in try/finally to ensure sensitive files are always deleted
    # even if Docker build or other operations fail
    try:
        # Create .gitignore in task directory (varies by benchmark type)
        print("\nSetting up git environment...")
        gitignore_patterns = ["workspace/"]
        if benchmark_metadata.type == "terminal":
            gitignore_patterns.append(TERMINAL_SOLUTION_FILE)
            # Create skeleton your_solution.sh for terminal benchmarks
            solution_path = os.path.join(TASK_DIR, TERMINAL_SOLUTION_FILE)
            with open(solution_path, "w") as f:
                f.write("#!/bin/bash\n\n# Write your solution here\n")
            print(f"  Created skeleton {solution_path}")
        create_gitignore(TASK_DIR, gitignore_patterns)
        
        # Build Docker image
        print()
        image_name = "baseline-task"
        if not build_docker_image(TASK_DIR, image_name, benchmark_metadata.dockerfile_path_override):
            print("Error: Failed to build Docker image.")
            return

        # Delete sensitive files IMMEDIATELY after build, BEFORE git init
        # This ensures golden solution is never committed to git history
        if benchmark_metadata.files_to_delete:
            print("\nDeleting sensitive files...")
            delete_files(TASK_DIR, benchmark_metadata.files_to_delete)
            files_deleted = True

        # NOW it's safe to init git - sensitive files are already gone
        ensure_git_config()
        init_git_repo(TASK_DIR, "Initial commit")

        # Capture the initial commit hash to seed last_update_commit
        initial_commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=TASK_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
        initial_commit_hash = initial_commit_result.stdout.strip()
        
        # Extract target_workdir from image to local WORKSPACE_DIR
        container_id = ""
        if benchmark_metadata.target_workdir:
            print(f"\nExtracting {benchmark_metadata.target_workdir} from image to {WORKSPACE_DIR}...")
            if not extract_dir_from_image(image_name, benchmark_metadata.target_workdir, WORKSPACE_DIR):
                print("Error: Failed to extract directory from image.")
                return
           
            # Run container with bind mount from WORKSPACE_DIR to target_workdir
            print()
            bind_mounts = [(WORKSPACE_DIR, benchmark_metadata.target_workdir)]
            container_id = run_docker_container(image_name, "baseline-container", bind_mounts)
            
            # Configure git safe.directory in container for all benchmark types
            # This prevents "dubious ownership" errors when running git commands
            if container_id:
                print("\nConfiguring git safe.directory in container...")
                subprocess.run(
                    ["docker", "exec", container_id, "git", "config", "--global", "--add", 
                     "safe.directory", benchmark_metadata.target_workdir],
                    capture_output=True,
                )
                print(f"  Added {benchmark_metadata.target_workdir} to git safe.directory")
                
                # Run and display container diagnostics
                diagnostics = run_container_diagnostics(
                    container_id, 
                    benchmark_metadata.target_workdir
                )
                print_diagnostics(diagnostics, benchmark_metadata.target_workdir)
        else:
            # No target_workdir specified, just run without bind mount
            print()
            container_id = run_docker_container(image_name, "baseline-container")
        
        if container_id is None:
            print("Warning: Failed to start Docker container. Session will be saved without container ID.")
            container_id = ""
        
        # Create and save SessionInfo
        session_info = SessionInfo(
            baseline_attempt_id=baseline_attempt_id,
            container_id=container_id,
            benchmark_metadata=benchmark_metadata,
            last_update_commit=initial_commit_hash,
        )
        save_session_info(session_info)
        
        print("\n" + "=" * 50)
        print("Session initialized successfully!")
        print("=" * 50)
        print(f"  Baseline Attempt ID: {baseline_attempt_id}")
        print(f"  Container ID: {container_id[:12] if container_id else 'N/A'}")
        print(f"  Benchmark Type: {benchmark_metadata.type}")
        print(f"  Target Workdir: {benchmark_metadata.target_workdir}")
        if benchmark_metadata.files_to_delete:
            print(f"  Files deleted: {benchmark_metadata.files_to_delete}")
        
        # Run custom start command script if it exists
        if os.path.isfile(CUSTOM_START_COMMAND_SCRIPT):
            print(f"\nRunning custom start command script: {CUSTOM_START_COMMAND_SCRIPT}")
            subprocess.run(
                [CUSTOM_START_COMMAND_SCRIPT, "--container-id", container_id],
                check=False,
            )
        
        if benchmark_metadata.target_workdir:
            # Initialize git repo in the workspace directory only for "code" benchmarks
            if benchmark_metadata.type == "code":
                print("\nInitializing git repo in workspace...")
                
                # Remove existing .git directory if it exists
                git_dir = os.path.join(WORKSPACE_DIR, ".git")
                if os.path.exists(git_dir):
                    force_rmtree(git_dir)
                
                init_git_repo(WORKSPACE_DIR, "Initial commit")
    
    finally:
        # Safety net: delete sensitive files if we failed before the normal deletion point
        # This ensures cleanup even if Docker build fails or other early errors occur
        if benchmark_metadata.files_to_delete and not files_deleted:
            print("\nCleaning up sensitive files (error recovery)...")
            delete_files(TASK_DIR, benchmark_metadata.files_to_delete)


if __name__ == "__main__":
    main()
