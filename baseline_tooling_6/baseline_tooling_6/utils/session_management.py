from dataclasses import dataclass, field, asdict
from typing import List, Optional
import json

from config import SESSION_INFO_PATH


@dataclass
class BenchmarkMetadata:
    """Metadata for benchmark configuration."""
    files_to_delete: List[str] = field(default_factory=list)
    type: str = "code"
    target_workdir: str = ""
    benchmark_id: str = ""
    dockerfile_path_override: Optional[str] = None


@dataclass
class SessionInfo:
    """Session information model."""
    baseline_attempt_id: str
    container_id: str
    benchmark_metadata: BenchmarkMetadata = field(default_factory=BenchmarkMetadata)
    last_update_commit: str = ""


def load_session_info() -> Optional[SessionInfo]:
    """Load session info from the JSON file.
    
    Returns:
        SessionInfo if file exists and is valid, None otherwise.
    """
    try:
        with open(SESSION_INFO_PATH, 'r') as f:
            data = json.load(f)
        
        # Parse nested BenchmarkMetadata
        benchmark_metadata = BenchmarkMetadata(**data.get('benchmark_metadata', {}))
        
        return SessionInfo(
            baseline_attempt_id=data['baseline_attempt_id'],
            container_id=data['container_id'],
            benchmark_metadata=benchmark_metadata,
            last_update_commit=data.get('last_update_commit', ''),
        )
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def save_session_info(session_info: SessionInfo) -> None:
    """Save session info to the JSON file.
    
    Args:
        session_info: The SessionInfo object to save.
    """
    with open(SESSION_INFO_PATH, 'w') as f:
        json.dump(asdict(session_info), f, indent=2)
