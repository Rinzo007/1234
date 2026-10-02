"""Публичный фасад команды пережатия кэша."""

import sys

from .cache_compact_runtime import *

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main())