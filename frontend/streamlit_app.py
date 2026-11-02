"""Compatibility entry point for the modular Streamlit dashboard."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frontend.app import main


if __name__ == "__main__":
    main()
