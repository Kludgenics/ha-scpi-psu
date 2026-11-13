import sys
from pathlib import Path

repo_root = Path(__file__).parent.parent.resolve()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

pytest_plugins = ["pytest_homeassistant_custom_component"]
