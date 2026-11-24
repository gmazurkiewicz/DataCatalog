#!/usr/bin/env python3
"""Run without installing anything: python run.py [options]

Same as `uv run datacatalog` / `python -m datacatalog`. See `python run.py --help`.
"""

from datacatalog.cli import main

if __name__ == "__main__":
    main()
