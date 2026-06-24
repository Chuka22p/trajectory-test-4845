#!/usr/bin/env python3
"""Execute into the running Docker container at the target workdir."""

import os
import subprocess
import sys

from utils.session_management import load_session_info


def check_container_running(container_id: str) -> tuple[bool, str]:
    """Check if a container exists and is running.
    
    Args:
        container_id: The Docker container ID to check.
        
    Returns:
        Tuple of (is_running, status).
        status is the container state (running, exited, paused, etc.)
        or "not_found" if container doesn't exist,
        or "docker_not_installed" if docker command fails.
    """
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container_id],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False, "not_found"
        status = result.stdout.strip()
        return status == "running", status
    except FileNotFoundError:
        return False, "docker_not_installed"


def main():
    """Main entry point for executing into the container."""
    session_info = load_session_info()
    
    if session_info is None:
        print("Error: No active session found. Run start.py first.")
        sys.exit(1)
    
    if not session_info.container_id:
        print("Error: No container ID in session. Container may not be running.")
        sys.exit(1)
    
    container_id = session_info.container_id
    target_workdir = session_info.benchmark_metadata.target_workdir
    
    # Verify container is actually running before attempting exec
    is_running, status = check_container_running(container_id)
    
    if not is_running:
        print(f"Error: Container {container_id[:12]} is not running.")
        print()
        
        if status == "not_found":
            print("The container no longer exists. It may have been manually removed.")
        elif status == "exited":
            print("The container has exited (stopped).")
        elif status == "paused":
            print("The container is paused.")
        elif status == "docker_not_installed":
            print("Docker is not installed or not in PATH.")
            sys.exit(1)
        else:
            print(f"Container status: {status}")
        
        print()
        print("To fix this, run:")
        print("  python3 start.py")
        print()
        print("This will reset your environment and start a fresh container.")
        sys.exit(1)
    
    # Build the docker exec command
    cmd = ["docker", "exec", "-it"]
    
    if target_workdir:
        cmd.extend(["-w", target_workdir])
    
    cmd.extend([container_id, "/bin/bash"])
    
    print(f"Connecting to container {container_id[:12]}...")
    if target_workdir:
        print(f"Working directory: {target_workdir}")
    
    # Replace current process with docker exec
    os.execvp("docker", cmd)


if __name__ == "__main__":
    main()

