"""Where the bot keeps its files: next to the source, or next to MelvorBot.exe in a release build."""
import os
import pathlib
import sys

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = pathlib.Path(sys.executable).resolve().parent if FROZEN else pathlib.Path(__file__).resolve().parents[1]


def use_root() -> None:
    """Run from ROOT, so relative paths (logs/...) land there however the bot was started."""
    os.chdir(ROOT)
    (ROOT / "logs").mkdir(exist_ok=True)
