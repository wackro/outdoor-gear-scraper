import sys
from pathlib import Path

# Tests import `src.*` directly; the repo has no packaging metadata to rely on.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
