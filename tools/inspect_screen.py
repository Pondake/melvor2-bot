"""Developer tool: capture the game, OCR it and print every text line with its box.

Usage: python tools/inspect_screen.py [out.png] [--region x,y,w,h] [--scale 1.5]
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from bot import ocr  # noqa: E402
from bot.window import GameWindow  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("out", nargs="?")
p.add_argument("--region")
p.add_argument("--scale", type=float, default=1.0)
a = p.parse_args()

win = GameWindow()
img = win.capture()
print("size", win.size)
if a.out:
    Image.fromarray(img).save(a.out)
region = tuple(int(v) for v in a.region.split(",")) if a.region else None
for line in ocr.read(img, region, a.scale):
    print(line)
