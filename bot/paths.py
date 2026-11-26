"""Where the bot keeps its files.

From source everything stays in the checkout. The release build (MelvorBot.exe) runs from
%LOCALAPPDATA%\\Programs\\MelvorBot and keeps config, logs and state in %LOCALAPPDATA%\\MelvorBot,
so installing or updating the exe never touches them.
"""
import os
import pathlib
import sys

FROZEN = bool(getattr(sys, "frozen", False))
_LOCAL = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / "AppData" / "Local")
INSTALL_DIR = _LOCAL / "Programs" / "MelvorBot"
INSTALLED_EXE = INSTALL_DIR / "MelvorBot.exe"
# ROOT: the data folder (config.json, logs/). RESOURCES: files shipped with the program.
ROOT = _LOCAL / "MelvorBot" if FROZEN else pathlib.Path(__file__).resolve().parents[1]
RESOURCES = pathlib.Path(getattr(sys, "_MEIPASS", ROOT)) if FROZEN else ROOT


def use_root() -> None:
    """Run from ROOT, so relative paths (logs/...) land there however the bot was started."""
    ROOT.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    (ROOT / "logs").mkdir(exist_ok=True)
