"""Convenience entry point: uv run python main.py [export|probe|import]."""

import sys

from chatgpt_exporter import main

if __name__ == "__main__":
    sys.exit(main())
