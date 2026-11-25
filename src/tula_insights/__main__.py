"""Lets the package run as ``python -m tula_insights``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
