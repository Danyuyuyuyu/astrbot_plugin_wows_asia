import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# AstrBot keeps its data under $ASTRBOT_ROOT/data; never touch a real installation from tests
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="wows-astrbot-"))
sys.path.insert(0, str(ROOT))  # core tests: import wows
sys.path.insert(0, str(ROOT.parent))  # entry point tests: import <plugin dir>.main like AstrBot does
