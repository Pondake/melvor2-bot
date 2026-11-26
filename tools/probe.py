"""Developer tool: run the read-only parts of the bot against the live game (no action clicks)."""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from bot.game import GRIDS, Game  # noqa: E402
from bot.window import GameWindow  # noqa: E402

g = Game(GameWindow(), dry_run=True)
print("status:", g.status())
print("smelting:", g.read_smelting())
for skill in sys.argv[1:] or ["Mining"]:
    for page, card in g.scan_cards(GRIDS[skill]) if g.open_skill(skill) else []:
        print(f"{skill} card:", page, card.name, "active" if card.active else "")
