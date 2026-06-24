from dotenv import load_dotenv
from os import getenv
load_dotenv()

IS_LOCAL = getenv("BASELINE_ENDPOINT_ENV") == "local"
BASE_URL = "https://mercor-rl--coil-proxy-reverse-proxy.modal.run" if not IS_LOCAL else "http://localhost:9000"
BASELINE_LOCAL_API_KEY = getenv("BASELINE_LOCAL_API_KEY")

def get_api_headers() -> dict:
    """Return headers for API requests, including X-API-Key when running locally."""
    if IS_LOCAL and BASELINE_LOCAL_API_KEY:
        return {"X-API-Key": BASELINE_LOCAL_API_KEY}
    return {}

SESSION_INFO_PATH = ".session_info.json"
TASK_DIR = "task/"
WORKSPACE_DIR = "task/workspace/"
TERMINAL_SOLUTION_FILE = "your_solution.sh"
CUSTOM_START_COMMAND_SCRIPT = ".task_scripts/custom_start_command.sh"
SUBMISSION_GATE_SCRIPT = ".task_scripts/submission_gate_script.sh"
UPDATE_TASK_GATE_SCRIPT = ".task_scripts/update_task_gate_script.sh"
