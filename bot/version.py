"""The program's version: set by tools/build.py from the release tag, "dev" when run from source."""
try:
    from ._build import VERSION  # written by tools/build.py, not in git
except ImportError:
    VERSION = "dev"
