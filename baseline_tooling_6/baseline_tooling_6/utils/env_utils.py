"""Environment utilities for git initialization, file operations, and Docker setup."""

import inspect
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from typing import Optional

import requests


DEFAULT_GIT_USER_NAME = "Baseline User"
DEFAULT_GIT_USER_EMAIL = "baseline@example.com"


# =============================================================================
# File Operations
# =============================================================================

def _handle_rmtree_error(func, path, exc):
    """Error handler for shutil.rmtree to handle read-only files.
    
    Git creates some files (e.g., in .git/objects/) with read-only permissions.
    This handler makes them writable before retrying the deletion.
    
    Args:
        func: The function that raised the exception (e.g., os.unlink).
        path: The path that caused the error.
        exc: The exception that was raised.
    """
    exc_value = exc[1] if isinstance(exc, tuple) else exc
    # If the error is due to an access error (read-only file),
    # add write permission and try again
    if isinstance(exc_value, PermissionError) and not os.access(path, os.W_OK):
        try:
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
            func(path)
        except PermissionError:
            # Can't even chmod - file is owned by another user (e.g., root from Docker)
            # Re-raise to let force_rmtree handle it with Docker fallback
            raise exc_value
    else:
        raise exc_value


def force_rmtree(path: str) -> None:
    """Remove a directory tree, handling read-only files and root-owned files.
    
    First attempts normal removal with permission error handling.
    If that fails (e.g., files owned by root from Docker), falls back to
    using Docker to remove the directory with root privileges.
    
    Args:
        path: Directory path to remove.
    """
    abs_path = os.path.abspath(path)
    
    try:
        if "onexc" in inspect.signature(shutil.rmtree).parameters:
            shutil.rmtree(abs_path, onexc=_handle_rmtree_error)
        else:
            shutil.rmtree(abs_path, onerror=_handle_rmtree_error)
    except PermissionError:
        # Files are likely owned by root (created by Docker container)
        # Use Docker to remove them with root privileges
        print(f"  Permission denied, using Docker to remove {path}...")
        try:
            # Can't remove the mount point itself from inside the container,
            # so we remove the contents: /to_delete/* and /to_delete/.*
            # The shell glob handles both regular files and hidden files (like .git)
            subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{abs_path}:/to_delete",
                    "alpine:latest",
                    "sh", "-c", "rm -rf /to_delete/* /to_delete/.[!.]* /to_delete/..?* 2>/dev/null; true"
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            # Now the directory is empty and we can remove it normally
            if os.path.exists(abs_path):
                os.rmdir(abs_path)
        except subprocess.CalledProcessError as e:
            raise PermissionError(
                f"Failed to remove {path} even with Docker: {e.stderr}"
            ) from e
        except FileNotFoundError:
            raise PermissionError(
                f"Failed to remove {path}: Docker is not available for fallback removal"
            )


def download_and_extract_zip(task_zip_url: str, target_dir: str) -> bool:
    """Download and extract a zip file to a target directory.
    
    Removes existing target directory if present, then downloads and extracts
    the zip contents. If the zip contains a single top-level directory, its
    contents are extracted directly to target_dir.
    
    Args:
        task_zip_url: URL to download the zip from (e.g., presigned S3 URL).
        target_dir: Directory to extract contents to.
        
    Returns:
        True on success, False on error.
    """
    # Remove existing directory and recreate
    if os.path.exists(target_dir):
        print(f"Removing existing {target_dir}...")
        force_rmtree(target_dir)
    
    os.makedirs(target_dir, exist_ok=True)
    print(f"Created {target_dir}")
    
    # Download zip to temp file
    print("Downloading task zip...")
    try:
        response = requests.get(task_zip_url, stream=True)
        response.raise_for_status()
        
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
            for chunk in response.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
            tmp_zip_path = tmp_file.name
        
        print("Download complete.")
        
    except requests.exceptions.RequestException as e:
        print(f"Error downloading zip: {e}")
        return False
    
    # Extract zip
    print("Extracting zip...")
    try:
        with tempfile.TemporaryDirectory() as tmp_extract_dir:
            with zipfile.ZipFile(tmp_zip_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_extract_dir)
            
            # Find the top-level directory in the extracted content
            extracted_items = os.listdir(tmp_extract_dir)
            
            # If there's a single top-level directory, extract its contents
            if len(extracted_items) == 1:
                top_level = os.path.join(tmp_extract_dir, extracted_items[0])
                if os.path.isdir(top_level):
                    # Move contents of the top-level dir to target
                    for item in os.listdir(top_level):
                        src = os.path.join(top_level, item)
                        dst = os.path.join(target_dir, item)
                        shutil.move(src, dst)
                else:
                    # Single file at top level, move it
                    shutil.move(top_level, os.path.join(target_dir, extracted_items[0]))
            else:
                # Multiple items at top level, move all to target
                for item in extracted_items:
                    src = os.path.join(tmp_extract_dir, item)
                    dst = os.path.join(target_dir, item)
                    shutil.move(src, dst)
        
        print("Extraction complete.")

        # Restore execute permissions on shell scripts
        # Python's zipfile.extractall() does not preserve Unix file permissions
        # Skip symlinks to prevent modifying permissions on arbitrary files
        for root, dirs, files in os.walk(target_dir):
            for filename in files:
                if filename.endswith('.sh'):
                    filepath = os.path.join(root, filename)
                    if os.path.islink(filepath):
                        continue
                    try:
                        current_mode = os.stat(filepath).st_mode
                        os.chmod(filepath, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    except OSError as e:
                        print(f"Warning: Could not chmod {filepath}: {e}")
        print("Restored execute permissions on shell scripts.")

    except zipfile.BadZipFile as e:
        print(f"Error extracting zip: {e}")
        return False
    finally:
        # Clean up temp zip file
        if os.path.exists(tmp_zip_path):
            os.unlink(tmp_zip_path)
    
    return True


def delete_files(base_dir: str, files_to_delete: list[str]) -> None:
    """Delete files or directories relative to a base directory.
    
    Args:
        base_dir: Base directory that paths are relative to.
        files_to_delete: List of paths relative to base_dir to delete.
    """
    for file_path in files_to_delete:
        full_path = os.path.join(base_dir, file_path)
        
        if os.path.isfile(full_path):
            os.unlink(full_path)
            print(f"  Deleted file: {file_path}")
        elif os.path.isdir(full_path):
            force_rmtree(full_path)
            print(f"  Deleted directory: {file_path}")
        else:
            print(f"  Not found (skipping): {file_path}")


# =============================================================================
# Git Operations
# =============================================================================

def create_gitignore(directory: str, ignore_patterns: list[str]) -> None:
    """Create a .gitignore file in the specified directory.
    
    Args:
        directory: The directory to create .gitignore in.
        ignore_patterns: List of patterns to ignore.
    """
    gitignore_path = os.path.join(directory, ".gitignore")
    with open(gitignore_path, "w") as f:
        f.write("\n".join(ignore_patterns) + "\n")
    print(f"  Created .gitignore in {directory}")


def ensure_git_config() -> None:
    """Ensure git user.name and user.email are configured globally.
    
    Sets default values if not already configured.
    """
    # Check and set user.name
    try:
        result = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            subprocess.run(
                ["git", "config", "--global", "user.name", DEFAULT_GIT_USER_NAME],
                check=True,
            )
            print(f"  Set git user.name to '{DEFAULT_GIT_USER_NAME}'")
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "config", "--global", "user.name", DEFAULT_GIT_USER_NAME],
            check=True,
        )
        print(f"  Set git user.name to '{DEFAULT_GIT_USER_NAME}'")
    
    # Check and set user.email
    try:
        result = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            subprocess.run(
                ["git", "config", "--global", "user.email", DEFAULT_GIT_USER_EMAIL],
                check=True,
            )
            print(f"  Set git user.email to '{DEFAULT_GIT_USER_EMAIL}'")
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "config", "--global", "user.email", DEFAULT_GIT_USER_EMAIL],
            check=True,
        )
        print(f"  Set git user.email to '{DEFAULT_GIT_USER_EMAIL}'")


def init_git_repo(directory: str, initial_commit_message: str = "Initial commit") -> bool:
    """Initialize a git repository with an initial commit.
    
    Args:
        directory: The directory to initialize as a git repo.
        initial_commit_message: Message for the initial commit.
        
    Returns:
        True on success, False on error.
    """
    try:
        # git init
        subprocess.run(
            ["git", "init"],
            cwd=directory,
            capture_output=True,
            check=True,
        )
        print(f"  Initialized git repo in {directory}")
        
        # git add .
        subprocess.run(
            ["git", "add", "."],
            cwd=directory,
            capture_output=True,
            check=True,
        )
        print("  Staged all files")
        
        # git commit (disable GPG signing - these are internal commits, not user commits)
        subprocess.run(
            ["git", "-c", "commit.gpgSign=false", "commit", "-m", initial_commit_message],
            cwd=directory,
            capture_output=True,
            check=True,
        )
        print(f"  Created initial commit: '{initial_commit_message}'")
        
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"  Error initializing git repo: {e.stderr if e.stderr else e}")
        return False
    except FileNotFoundError:
        print("  Error: git is not installed or not in PATH.")
        return False


def init_git_repo_in_container(
    container_id: str,
    target_workdir: str,
    initial_commit_message: str = "Initial commit",
) -> bool:
    """Initialize a git repository inside a Docker container.
    
    This function:
    1. Removes existing .git directory if present
    2. Configures git to ignore dubious ownership
    3. Sets git user.name and user.email
    4. Initializes git repo and creates initial commit
    
    Args:
        container_id: The Docker container ID.
        target_workdir: The directory inside the container to initialize.
        initial_commit_message: Message for the initial commit.
        
    Returns:
        True on success, False on error.
    """
    if not container_id:
        print("  No container ID provided, skipping container git init.")
        return False
    
    def docker_exec(command: str) -> Optional[str]:
        """Execute a command in the container and return output."""
        try:
            result = subprocess.run(
                ["docker", "exec", container_id, "sh", "-c", command],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            # Don't fail on non-critical errors
            return None
    
    try:
        # Remove existing .git directory if present
        docker_exec(f"rm -rf {target_workdir}/.git")
        print(f"  Removed existing .git in container at {target_workdir}")
        
        # Configure git to ignore dubious ownership for this directory
        docker_exec(f"git config --global --add safe.directory {target_workdir}")
        print(f"  Added {target_workdir} to git safe.directory")
        
        # Set git user.name and user.email in container
        docker_exec(f"git config --global user.name '{DEFAULT_GIT_USER_NAME}'")
        docker_exec(f"git config --global user.email '{DEFAULT_GIT_USER_EMAIL}'")
        print("  Configured git user in container")
        
        # git init
        subprocess.run(
            ["docker", "exec", "-w", target_workdir, container_id, "git", "init"],
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"  Initialized git repo in container at {target_workdir}")
        
        # git add .
        subprocess.run(
            ["docker", "exec", "-w", target_workdir, container_id, "git", "add", "."],
            capture_output=True,
            text=True,
            check=True,
        )
        print("  Staged all files in container")
        
        # git commit (disable GPG signing - these are internal commits, not user commits)
        subprocess.run(
            ["docker", "exec", "-w", target_workdir, container_id,
             "git", "-c", "commit.gpgSign=false", "commit", "-m", initial_commit_message],
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"  Created initial commit in container: '{initial_commit_message}'")
        
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"  Error initializing git repo in container: {e.stderr if e.stderr else e}")
        return False
    except FileNotFoundError:
        print("  Error: docker is not installed or not in PATH.")
        return False


# =============================================================================
# Docker Operations
# =============================================================================

def stop_and_remove_container(container_id: str) -> bool:
    """Stop and remove a Docker container.
    
    Args:
        container_id: The container ID or name to stop and remove.
        
    Returns:
        True on success, False on error.
    """
    if not container_id:
        return True
    
    print(f"Stopping and removing container {container_id[:12]}...")
    try:
        # Use docker rm -f to force remove (stops if running, then removes)
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            text=True,
            check=True,
        )
        print("  Container removed successfully.")
        return True
    except subprocess.CalledProcessError as e:
        # Container might not exist, which is fine
        if "No such container" in e.stderr:
            print("  Container not found (already removed).")
            return True
        print(f"  Error removing container: {e.stderr}")
        return False
    except FileNotFoundError:
        print("  Error: Docker is not installed or not in PATH.")
        return False


def build_docker_image(
    build_context_dir: str,
    image_name: str = "baseline-task",
    dockerfile_path_override: Optional[str] = None,
) -> bool:
    """Build a Docker image from a Dockerfile.

    Args:
        build_context_dir: Directory containing the Dockerfile.
        image_name: Name to tag the built image with.
        dockerfile_path_override: Relative path to the Dockerfile within build_context_dir,
            if not at the top level (e.g. 'environment/Dockerfile').

    Returns:
        True on success, False on error.
    """
    if dockerfile_path_override:
        dockerfile_path = os.path.join(build_context_dir, dockerfile_path_override)
    else:
        dockerfile_path = os.path.join(build_context_dir, "Dockerfile")

    if not os.path.exists(dockerfile_path):
        print(f"Error: No Dockerfile found at {dockerfile_path}")
        return False

    # Use the Dockerfile's parent directory as build context when overridden
    if dockerfile_path_override:
        build_context = os.path.dirname(dockerfile_path)
    else:
        build_context = build_context_dir

    print("Building Docker image...")
    try:
        cmd = ["docker", "build", "--platform", "linux/amd64", "-t", image_name, build_context]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        print("Docker image built successfully.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error building Docker image: {e.stderr}")
        return False
    except FileNotFoundError:
        print("Error: Docker is not installed or not in PATH.")
        return False


def extract_dir_from_image(
    image_name: str,
    container_path: str,
    host_path: str,
) -> bool:
    """Extract a directory from a Docker image to the host filesystem.
    
    Creates a temporary container from the image (without starting it),
    copies the specified directory to the host, then removes the container.
    
    Args:
        image_name: Name of the Docker image.
        container_path: Path inside the container to extract.
        host_path: Path on the host to extract to.
        
    Returns:
        True on success, False on error.
    """
    temp_container_name = "temp-extract-container"
    
    # Clean up any existing temp container
    subprocess.run(
        ["docker", "rm", "-f", temp_container_name],
        capture_output=True,
    )
    
    # Remove existing host directory and recreate
    if os.path.exists(host_path):
        print(f"Removing existing {host_path}...")
        force_rmtree(host_path)
    os.makedirs(host_path, exist_ok=True)
    
    try:
        # Create (but don't start) a temporary container
        print(f"Creating temporary container from image {image_name}...")
        subprocess.run(
            ["docker", "create", "--name", temp_container_name, image_name],
            capture_output=True,
            text=True,
            check=True,
        )
        
        # Copy the directory from the container to the host
        # docker cp copies contents when source ends with /., otherwise copies the dir itself
        print(f"Extracting {container_path} to {host_path}...")
        subprocess.run(
            ["docker", "cp", f"{temp_container_name}:{container_path}/.", host_path],
            capture_output=True,
            text=True,
            check=True,
        )
        print("Extraction complete.")
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"Error extracting directory from image: {e.stderr}")
        return False
    except FileNotFoundError:
        print("Error: Docker is not installed or not in PATH.")
        return False
    finally:
        # Always clean up the temporary container
        subprocess.run(
            ["docker", "rm", "-f", temp_container_name],
            capture_output=True,
        )


def run_docker_container(
    image_name: str,
    container_name: str = "baseline-container",
    bind_mounts: list[tuple[str, str]] | None = None,
) -> str | None:
    """Run a Docker container in detached mode with optional bind mounts.
    
    Args:
        image_name: Name of the Docker image to run.
        container_name: Name for the container.
        bind_mounts: Optional list of (host_path, container_path) tuples for bind mounts.
        
    Returns:
        Container ID on success, None on error.
    """
    # Build the docker run command
    cmd = ["docker", "run", "-d", "--name", container_name]
    
    # Add bind mounts
    if bind_mounts:
        for host_path, container_path in bind_mounts:
            # Convert to absolute path if relative
            abs_host_path = os.path.abspath(host_path)
            cmd.extend(["-v", f"{abs_host_path}:{container_path}"])
    
    cmd.append(image_name)
    
    # Keep container running indefinitely
    cmd.extend(["sleep", "infinity"])
    
    print("Starting Docker container...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        container_id = result.stdout.strip()
        print(f"Container started with ID: {container_id[:12]}")
        return container_id
    except subprocess.CalledProcessError as e:
        # If container already exists, try to remove it and retry
        if "already in use" in e.stderr:
            print("Removing existing container...")
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                container_id = result.stdout.strip()
                print(f"Container started with ID: {container_id[:12]}")
                return container_id
            except subprocess.CalledProcessError as e2:
                print(f"Error starting Docker container: {e2.stderr}")
                return None
        print(f"Error starting Docker container: {e.stderr}")
        return None


def build_and_run_docker(
    build_context_dir: str,
    image_name: str = "baseline-task",
    container_name: str = "baseline-container",
) -> str | None:
    """Build a Docker image and start a container in detached mode.
    
    Args:
        build_context_dir: Directory containing the Dockerfile.
        image_name: Name to tag the built image with.
        container_name: Name for the container.
        
    Returns:
        Container ID on success, None on error.
    """
    dockerfile_path = os.path.join(build_context_dir, "Dockerfile")
    
    if not os.path.exists(dockerfile_path):
        print(f"Error: No Dockerfile found at {dockerfile_path}")
        return None
    
    # Build the Docker image
    print("Building Docker image...")
    try:
        subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "-t", image_name, build_context_dir],
            capture_output=True,
            text=True,
            check=True,
        )
        print("Docker image built successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error building Docker image: {e.stderr}")
        return None
    except FileNotFoundError:
        print("Error: Docker is not installed or not in PATH.")
        return None
    
    # Run the container in detached mode
    print("Starting Docker container...")
    try:
        result = subprocess.run(
            ["docker", "run", "-d", "--name", container_name, image_name],
            capture_output=True,
            text=True,
            check=True,
        )
        container_id = result.stdout.strip()
        print(f"Container started with ID: {container_id[:12]}")
        return container_id
    except subprocess.CalledProcessError as e:
        # If container already exists, try to remove it and retry
        if "already in use" in e.stderr:
            print("Removing existing container...")
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            try:
                result = subprocess.run(
                    ["docker", "run", "-d", "--name", container_name, image_name],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                container_id = result.stdout.strip()
                print(f"Container started with ID: {container_id[:12]}")
                return container_id
            except subprocess.CalledProcessError as e2:
                print(f"Error starting Docker container: {e2.stderr}")
                return None
        print(f"Error starting Docker container: {e.stderr}")
        return None
