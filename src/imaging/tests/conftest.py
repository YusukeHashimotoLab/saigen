"""Test setup for the imaging module (no hardware, no Bluetooth).

The imaging modules import each other by flat name (`import paths`), so the
module directory goes on sys.path. IMAGING_DATA_DIR is pointed at a fresh
temporary directory *before* any of them is imported, so no test ever
writes into the real data root.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["IMAGING_DATA_DIR"] = tempfile.mkdtemp(prefix="imaging_test_data_")
os.environ.pop("NEEWER_DEVICE_ID", None)
os.environ.pop("IMAGING_SERVER_URL", None)

IMAGING_DIR = Path(__file__).resolve().parents[1]
if str(IMAGING_DIR) not in sys.path:
    sys.path.insert(0, str(IMAGING_DIR))
