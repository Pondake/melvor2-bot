"""Developer tool: send one input to the game, then save a screenshot.

Usage:
  python tools/act.py click X Y out.png
  python tools/act.py scroll X Y NOTCHES out.png
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from bot.window import GameWindow  # noqa: E402

win = GameWindow()
cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "click":
    win.click(int(args[0]), int(args[1]))
    out = args[2]
elif cmd == "scroll":
    win.scroll(int(args[0]), int(args[1]), int(args[2]))
    out = args[3]
else:
    raise SystemExit(f"unknown command {cmd}")
time.sleep(0.8)
Image.fromarray(win.capture()).save(out)
