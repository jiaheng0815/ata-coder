"""pytest configuration — shared fixtures and path setup for the test suite."""
from pathlib import Path
import sys

# Ensure the project root is on sys.path so tests can import from
# top-level packages (ata_coder, examples) whether or not the package
# is installed in editable mode.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
