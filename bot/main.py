"""Melvor Idle 2 helper bot: keeps the character busy following a rotation and rules.

Run:  .venv\\Scripts\\python -m bot.main [--dry-run] [--config config.json]
GUI:  .venv\\Scripts\\pythonw -m bot.gui   (or start_gui.cmd)
Keys: Ctrl+Alt+P pause/resume, Ctrl+Alt+Q stop (global hotkeys).
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import logging
import pathlib
import random
import re
import threading
import time

from .game import GRIDS, SKILLS, Game, Status, Vitals, parse_amount, similar
from .window import GameWindow, GameWindowError
from . import desktop, steam
from .paths import RESOURCES, ROOT, use_root

CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG_PATH = RESOURCES / "config.default.json"  # shipped settings; config.json is the user's copy
STATE_PATH = ROOT / "logs" / "state.json"  # what the bot last read from the game; safe to delete
log = logging.getLogger("bot")

# Containers opened in the bank besides `open_items.names` (Hunting gives Small Survey Findings).
ALWAYS_OPEN = ["Small Survey Findings"]

# Skills whose page is the recipe frame (categories, recipes, Create); trained up to a goal level.
RECIPE_SKILLS = ["Fletching", "Runecrafting", "Crafting", "Herblore"]
SUPPORTED_SKILLS = [*GRIDS, "Fishing", "Cooking", "Firemaking", "Thieving", "Combat", *RECIPE_SKILLS]  # GRIDS: skills whose page is a grid of cards
# With a full bank a new kind of item is lost. These only add to stacks that exist already
# (Mining / Woodcutting on a card done before, Firemaking, Cooking a food already in stock) or
# rarely give items at all (Astrology).
SAFE_WHEN_FULL = {"Mining", "Woodcutting", "Firemaking", "Cooking", "Astrology"}
PAGE_SKILLS = {"Farming"}  # passive skills the status bar shows while their page is open
DEFAULT_ROTATION = [
    {"skill": "Mining", "target": "best Ore Vein", "minutes": [20, 40]},
    {"skill": "Woodcutting", "target": "best", "minutes": [20, 40]},
    {"skill": "Fishing", "minutes": [20, 40]},
    {"skill": "Cooking", "minutes": [10, 20]},
    {"skill": "Firemaking", "minutes": [10, 20]},
    {"skill": "Thieving", "minutes": [15, 30]},
    {"skill": "Astrology", "target": "least", "minutes": [15, 30]},
]


def load_config(path: pathlib.Path = CONFIG_PATH) -> dict:
    if not path.exists() and path == CONFIG_PATH and DEFAULT_CONFIG_PATH.exists():
        path.write_text(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not cfg.get("rotation"):
        cfg["rotation"] = [dict(a) for a in DEFAULT_ROTATION]
    return cfg


def minutes_range(act: dict) -> tuple[float, float]:
    """`minutes` is a number (fixed), [min, max] (random each time) or missing/0 (stay)."""
    m = act.get("minutes") or 0
    if isinstance(m, (list, tuple)):
        lo, hi = (float(m[0]), float(m[-1])) if m else (0.0, 0.0)
        return min(lo, hi), max(lo, hi)
    return float(m), float(m)


def save_config(cfg: dict, path: pathlib.Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# Bars from basic to advanced, to prefer the better bar when one ore makes two (Iron: Iron or Steel).
BAR_ORDER = ["Copper Bar", "Bronze Bar", "Iron Bar", "Silver Bar", "Steel Bar", "Gold Bar", "Mithril Bar",
             "Adamant Bar", "Adamantite Bar", "Rune Bar", "Runite Bar", "Dragonite Bar"]

class UserActive(Exception):
    """The user is playing the game right now: the bot stands back and tries again later."""


class Controls:
    """Flags shared by the bot loop, the global hotkeys and the GUI."""

    def __init__(self):
        self.paused = threading.Event()
        self.stop = threading.Event()
        self.skip = threading.Event()  # "move on to the next activity now" (button in the window)

    def toggle_pause(self) -> bool:
        if self.paused.is_set():
            self.paused.clear()
            log.info("resumed")
        else:
            self.paused.set()
            log.info("paused (Ctrl+Alt+P to resume)")
        return self.paused.is_set()


class Hotkeys(threading.Thread):
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
    WM_HOTKEY = 0x0312

    def __init__(self, controls: Controls):
        super().__init__(daemon=True)
        self.ctl = controls

    def run(self) -> None:
        user32 = ctypes.windll.user32
        mods = self.MOD_ALT | self.MOD_CONTROL | self.MOD_NOREPEAT
        ok_p = user32.RegisterHotKey(None, 1, mods, ord("P"))
        ok_q = user32.RegisterHotKey(None, 2, mods, ord("Q"))
        if not (ok_p and ok_q):
            log.warning("could not register Ctrl+Alt+P / Ctrl+Alt+Q (in use?)")
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY:
                if msg.wParam == 1:
                    self.ctl.toggle_pause()
                elif msg.wParam == 2:
                    log.info("stop requested (Ctrl+Alt+Q)")
                    self.ctl.stop.set()


class Bot:
    def __init__(self, game: Game, cfg: dict, controls: Controls):
        self.g = game
        self.ctl = controls
        self.cfg = cfg
        self.rotation = cfg["rotation"]
        self.idx = 0
        self.started_at = 0.0
        self.duration = 0.0           # seconds for the current activity, 0 = stay
        self.picked_start = False
        self.bait_needed = False      # bait ran out: next fishing run goes to the bait-box spot
        self.verify = False           # check on the next tick that the right card/spot is running
        self.level: int | None = None  # level of the current skill, to notice level-ups (new unlocks)
        self.known_spots: set[str] = set()
        self.skip_reason = ""         # why the current activity cannot run now (no food, nothing to cook)
        self.skips = 0                # activities skipped in a row
        self.saved_vitals: tuple = ()
        self.thief_survey: dict = {}   # {"t", "level", "spots": {location: [(npc, success, xp, seconds)]}}
        self.auto_eat_short: dict = {}  # last opponent whose max hit was above the auto-eat line
        self.too_hard: dict[str, dict] = {}  # monster -> {"t", "why"}: left because it ate the food too fast
        self.food_missing: dict[str, float] = {}  # food -> when the bank search found none (in memory)
        self.item_catalog: dict = {}   # items learned about: name -> {kind, text, t}
        self.hunt_failed: dict[str, float] = {}  # animal -> when it last would not start
        self.weapon: dict = {}  # {"type": "2H Sword" or None, "t"}: the normal weapon worn
        self.hunt_survey: dict = {}  # {"t", "level", "spots": {location: {animal: xp per catch}}}
        self.fire_look = 0.0  # last look at the log count while burning
        self.scarecrow_checked = 0.0
        self.scarecrow_worn = False
        self.fireflies_checked = 0.0
        self.jars_on: dict[str, float] = {}      # skill -> when its Fireflies in a Jar went on
        self.jars_locked: dict[str, float] = {}  # skill -> when its Toolbelt slot was found locked
        self.stance_map: dict[str, dict] = {}  # weapon type -> {stance: [skills it trains]}, from the tooltips
        self.recipes_made: dict[str, dict] = {}  # skill -> {recipe: last time it was started}
        self.vials_tried = 0.0  # last time vials were made for Herblore
        self.shop_failed: dict[str, float] = {}  # upgrade -> when a purchase did not go through
        self.gp_rate: dict[str, float] = {}  # activity ("Woodcutting best Tree") -> GP per minute it brought in
        self.price_queue: list[str] = []  # gained items whose sale price is not known yet
        self.learn_queue: list[str] = []  # new items waiting to be looked up
        self.hp_unread = 0             # Thieving looks in a row without a readable HP
        self.last_hp_frac: float | None = None
        self.food_zero = 0             # Thieving looks in a row that read 0 food
        self.last_emergency = 0.0
        self.levels: dict[str, int] = {}      # skill levels read from the sidebar
        self.bought: list[str] = []           # upgrades bought this run
        self.sold = 0                         # GP from selling this run
        self.cards: dict[str, list[str]] = {} # skill -> unlocked card names, from the last grid scan
        self.gold: int | None = None          # gold as last read in the Shop
        self.catalog: dict[str, dict] = {}    # upgrade -> prices seen so far (see learn_prices)
        self.equipped_heals: set[int] = set() # healing of the food in the Thieving / Combat slots: never sold
        self.shop: list[dict] = []            # upgrades read in the Shop, with what is still missing
        self.shop_read_at = 0.0
        self.ranch: dict[str, int] = {}       # enclosure stats in percent
        self.stances: dict[str, list[str]] = {}  # stance -> stats it trains (read once per run)
        self.known_targets: dict[str, list[str]] = {}  # skill -> cards started before (their items are in the bank)
        self.last_open_items = 0.0
        self.last_gear_check = 0.0
        self.gear: dict[str, list[str]] = {}  # skill -> Toolbelt items worn (as the bank shows them)
        self.quiet_until = 0.0        # after "everything skipped": do not retry every tick
        # Journal for the GUI: what the bot did, sold, bought and received. Texts are Dutch (UI).
        self.events: list[dict] = []          # {"t": epoch, "kind": ..., "text": ..., "gp": optional}
        self.drops: dict[str, list[int]] = {} # item -> [count at first sight, last count, gain kept]
        self.eats = 0                         # food eaten since the last journal line about loot
        self.drops_since = time.time()
        self.gp_samples: list[int] = []       # the status bar's GP per hour, read this turn
        self.gp_game_keys: set[str] = set()   # activities whose gp_rate already comes from the game
        self.announced = ""                   # last activity written to the journal
        self.slayer: list | None = None       # Slayer task as last read: [monster, done, needed]
        self.slayer_read_at = 0.0
        self.last_hp: int | None = None       # HP at the previous look during a fight
        self.worst_drop = 0                   # most HP lost between two looks in this fight
        self.level_pending: dict[str, int] = {}  # a level that jumped oddly: believed when read twice
        self.gold_seen: int | None = None     # gold at the last journal line about it
        self.plot_prices: dict[str, int] = {}  # Farming tab -> gold price of its next plot for sale
        self.saved_cycle: dict | None = None  # where the rotation was when the bot last ran
        self.recipe_raw: dict[str, list] = {}  # skill -> [(category, recipe, first ingredient in bank)]
        self.step_runs: dict[str, float] = {}  # low-priority step (every_hours) -> last start
        # What the bot knows is in the bank: item -> {"count", "price" (GP each), "t" (when seen)}.
        # Filled by the item notices, by every look at an item in the bank and by sales.
        self.inventory: dict[str, dict] = {}
        self.g.guard = self.user_guard
        self.food: dict[str, tuple[int | None, int]] = {}  # cooked food: name -> (pieces in bank, HP each)
        self.food_read_at = 0.0
        self.vitals: Vitals | None = None  # HP and equipped food, read on the Thieving page
        self.fish_table: dict[str, dict[str, int | None]] = {}  # spot -> {fish: pieces in bank}
        self.fish_read_at = 0.0
        self.cooking_level: int | None = None
        self.cooking_next: int | None = None  # Cooking level of the next recipe to unlock
        self.fire_logs: dict[str, int] = {}   # burnable logs in the bank, by recipe
        self.fire_next: int | None = None     # Firemaking level of the next log recipe
        self.game_build: str | None = None    # Steam build id of the installed game, last seen
        self.load_state()
        self.last_reselect = 0.0
        self.last_smelt_check = 0.0
        self.last_farm_check = 0.0
        self.smelting = False
        self.failures = 0
        self.last_bank_warn = 0.0
        self.last_stuck_warn = 0.0
        self.update_checked = 0.0
        self.update_warned = ""  # build id of a waiting update the journal already mentions
        self.game_front_at = 0.0  # last time the user had the game in front and was using it
        self.launched_at = 0.0    # last time the bot started the game through Steam
        self.capture_failed_since = 0.0  # first of an unbroken run of unreadable game windows
        self.game_missing = False  # the last look found no game window: it may still be loading
        self.g.real_ok = self.real_mouse_ok
        self.real_mouse_note = ""  # why the real mouse may or may not be used, for the log
        # Read by the GUI (plain attributes; written only by the bot thread).
        self.state = "starting"       # starting | running | paused | waiting | error | stopped
        self.problem = ""             # last error message, cleared after a good tick
        self.problem_code = ""        # GameWindowError.code when the game window is the problem
        self.errors = 0               # consecutive failed ticks
        self.status: Status | None = None
        self.target_name = ""         # card the bot started, e.g. "Coal"
        self.task = ""                # what the bot itself is busy with right now, for the window
        self.smelt_counts: dict[str, tuple[int, int]] = {}

    # What the bot learned about the game (food stock, fish per spot, HP) survives a restart, so
    # the GUI is not empty and the slow fish survey is not repeated every time.

    def load_state(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.food = {k: (v[0], v[1]) for k, v in data.get("food", {}).items()}
        self.food_read_at = data.get("food_read_at", 0.0)
        self.fish_table = data.get("fish_table", {})
        self.fish_read_at = data.get("fish_read_at", 0.0)
        self.cooking_level = data.get("cooking_level")
        self.cooking_next = data.get("cooking_next")
        self.fire_logs = data.get("fire_logs", {})
        self.fire_next = data.get("fire_next")
        self.known_targets = data.get("known_targets", {})
        self.shop = data.get("shop", [])
        self.cards = data.get("cards", {})
        self.gear = data.get("gear", {})
        self.events = data.get("events", [])
        self.slayer = data.get("slayer")
        self.gold_seen = data.get("gold_seen")
        self.plot_prices = data.get("plot_prices", {})
        self.saved_cycle = data.get("cycle")
        self.recipe_raw = data.get("recipe_raw", {})
        self.thief_survey = data.get("thief_survey", {})
        self.auto_eat_short = data.get("auto_eat_short", {})
        self.too_hard = data.get("too_hard", {})
        self.item_catalog = data.get("item_catalog", {})
        self.weapon = data.get("weapon", {})
        self.hunt_survey = data.get("hunt_survey", {})
        self.jars_on = data.get("jars_on", {})
        self.stance_map = data.get("stance_map", {})
        self.recipes_made = data.get("recipes_made", {})
        self.gp_rate = data.get("gp_rate", {})
        self.game_build = data.get("game_build")
        # HP bar ends, measured at full HP: kept across restarts (a fight rarely starts at full HP).
        self.g.bar_extent.update({k: tuple(v) for k, v in data.get("bar_extent", {}).items()})
        self.learn_queue = data.get("learn_queue", [])
        self.step_runs = data.get("step_runs", {})  # low-priority step -> last start time
        self.armour_checked = data.get("armour_checked")
        self.inventory = data.get("inventory", {})
        # A restart is not a reason to look at the Shop (and sell for it) again.
        self.shop_read_at = data.get("shop_read_at", 0.0)
        # The same activity is not announced again after a restart.
        self.announced = next((e["text"] for e in reversed(self.events) if e["kind"] == "activity"), "")
        self.catalog = data.get("catalog", {})
        self.levels = {k: v for k, v in data.get("levels", {}).items() if k in SKILLS}
        self.ranch = data.get("ranch", {})
        if data.get("vitals"):
            self.vitals = Vitals(*data["vitals"])

    def save_state(self) -> None:
        v = self.vitals
        data = {"food": self.food, "food_read_at": self.food_read_at, "fish_table": self.fish_table,
                "fish_read_at": self.fish_read_at, "cooking_level": self.cooking_level,
                "cooking_next": self.cooking_next, "fire_logs": self.fire_logs, "fire_next": self.fire_next, "known_targets": self.known_targets, "shop": self.shop, "ranch": self.ranch, "cards": self.cards, "gear": self.gear, "events": self.events[-400:], "slayer": self.slayer, "gold_seen": self.gold_seen, "plot_prices": self.plot_prices, "cycle": self.cycle_state(), "recipe_raw": self.recipe_raw, "thief_survey": getattr(self, "thief_survey", {}), "auto_eat_short": getattr(self, "auto_eat_short", {}), "too_hard": getattr(self, "too_hard", {}), "item_catalog": getattr(self, "item_catalog", {}), "weapon": getattr(self, "weapon", {}), "hunt_survey": getattr(self, "hunt_survey", {}), "jars_on": getattr(self, "jars_on", {}), "stance_map": getattr(self, "stance_map", {}), "recipes_made": getattr(self, "recipes_made", {}), "gp_rate": getattr(self, "gp_rate", {}), "game_build": getattr(self, "game_build", None), "bar_extent": dict(self.g.bar_extent),
                "learn_queue": getattr(self, "learn_queue", []), "step_runs": getattr(self, "step_runs", {}), "armour_checked": getattr(self, "armour_checked", None), "inventory": self.inventory, "shop_read_at": self.shop_read_at, "catalog": self.catalog, "levels": self.levels,
                "vitals": [v.hp, v.hp_max, v.food_heal, v.food_count] if v else None}
        try:
            STATE_PATH.parent.mkdir(exist_ok=True)
            STATE_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as e:
            log.debug("state not saved: %s", e)

    def event(self, kind: str, text: str, **extra) -> None:
        """Add a line to the journal the GUI shows (kept across restarts)."""
        if self.g.dry_run:
            text += " (dry run)"
        self.events.append({"t": time.time(), "kind": kind, "text": text, **extra})
        del self.events[:-400]
        self.save_state()

    def user_guard(self) -> None:
        """Stand back while the user plays: raise UserActive when they used the game moments ago.

        Called before every click. A fight is the exception: someone has to keep eating, so the bot
        carries on there whoever else is clicking."""
        if not self.cfg.get("yield_to_user", True):
            return
        if self.status and self.status.skill == "Combat" and self.current["skill"] == "Combat":
            return
        idle = self.g.win.user_idle_seconds()
        if idle is not None:  # the game is in front
            if idle < self.cfg.get("yield_seconds", 20):
                raise UserActive()
            return
        # The user just switched away from the game: a short pause, not the full wait.
        if time.time() - self.game_front_at < self.cfg.get("yield_after_leaving", 3):
            raise UserActive()

    def watch_front(self) -> None:
        """Background thread: note when the user last had the game in front (with recent input).

        Polled often, so leaving the game is seen within a fraction of a second, even while the
        bot itself sleeps between looks."""
        while not self.ctl.stop.is_set():
            try:
                idle = None if desktop.real_active else self.g.win.user_idle_seconds()
                if idle is not None and idle < self.cfg.get("yield_seconds", 20):
                    self.game_front_at = time.time()
            except Exception:
                pass
            self.ctl.stop.wait(0.25)

    def note_stock(self, name: str, count: int | None = None, price: float | None = None) -> None:
        """Remember how many pieces of an item are in the bank and what one sells for."""
        key = next((k for k in self.inventory if similar(k, name) >= 0.9), name)
        slot = self.inventory.setdefault(key, {"count": None, "price": None, "t": 0.0})
        if count is not None:
            slot["count"], slot["t"] = max(0, int(count)), time.time()
        if price:
            slot["price"] = price

    def known_count(self, name: str) -> int | None:
        """Pieces of `name` in the bank as last seen: the stock list, else the Firemaking page."""
        known = self.stock(name, 12 * 3600)
        if known:
            return known["count"]
        return next((n for k, n in self.fire_logs.items() if similar(k, name) >= 0.9), None)

    def stock(self, name: str, max_age: float) -> dict | None:
        """What is known about an item, if it was seen within `max_age` seconds."""
        key = next((k for k in self.inventory if similar(k, name) >= 0.9), None)
        slot = self.inventory.get(key) if key else None
        return slot if slot and slot.get("count") is not None and time.time() - slot["t"] <= max_age else None

    def track_drop(self) -> None:
        """Follow the item notice of the game to know what came in during this activity."""
        got = self.g.read_drop()
        if not got:
            return
        name, count = got
        plus = getattr(self.g, "last_drop_plus", None)
        if plus and count <= plus and not self.known_item(name):
            # The bank holds no more than just came in: the first of its kind. Learn what it is.
            if name not in self.learn_queue:
                log.info("new item: %s (+%d, %d in the bank)", name, plus, count)
                self.learn_queue.append(name)
        self.note_stock(name, count)
        key = next((k for k in self.drops if similar(k, name) >= 0.8), name)
        slot = self.drops.setdefault(key, [count, count, 0])
        step = count - slot[1]
        if step < 0 or step > 200:
            # Sold, used, or a misread count: keep what was gained so far and start again from here.
            slot[2] += max(0, slot[1] - slot[0])
            slot[0] = count
        slot[1] = count

    def known_item(self, name: str) -> bool:
        return any(similar(k, name) >= 0.9 for k in self.item_catalog)

    def learn_items(self) -> None:
        """Read what each newly seen item is, write it in the journal and let the matching routine
        pick it up (compost, bait, food, gear and seeds each have one)."""
        while self.learn_queue:
            name = self.learn_queue.pop(0)
            if self.known_item(name):
                continue
            self.task = f"looking up the new item {name}"
            info = self.g.item_info(name)
            if not info:
                log.info("new item %s: not found in the bank", name)
                continue
            name = info["name"]  # the full name when the notice was cut
            if self.known_item(name):
                continue
            self.item_catalog[name] = {"kind": info["kind"], "text": info["text"], "t": time.time()}
            use = {"compost": "the farm uses it as compost before planting",
                   "bait": "fishing uses it when it is the biggest bait stack",
                   "food": "it counts as food from the next food check",
                   "gear": "the next gear check looks at it",
                   "seed": "the crop choice can plant it",
                   "container": "it is opened with the nests from now on"}.get(info["kind"], "nothing uses it yet")
            self.event("learn", f"New item {name} ({info['kind']}): {info['text'] or 'no description read'} - {use}")
            if info["kind"] == "food":
                self.food_read_at = 0.0
            if info["kind"] == "gear":
                self.last_gear_check = 0.0
            self.save_state()

    def activity_key(self) -> str:
        act = self.current
        return f"{act['skill']} {act.get('target', '')}".strip()

    def money_factor(self) -> float:
        """While saving for something (a Tree plot), the best earners get longer turns and the
        poorest shorter ones; otherwise 1. Needs GP/min of at least three activities."""
        mcfg = self.cfg.get("money", {})
        if not mcfg.get("steer", True) or not getattr(self, "saving", False) or len(self.gp_rate) < 3:
            return 1.0
        best = max(self.gp_rate.values()) or 0
        mine = self.gp_rate.get(self.activity_key())
        if not best or mine is None:
            return 1.0
        if mine >= best * 0.8:
            return mcfg.get("boost", 1.5)
        if mine < best * 0.25:
            return mcfg.get("cut", 0.7)
        return 1.0

    def flush_drops(self, where: str) -> None:
        """Write what came in since the last flush as one journal line."""
        gains = sorted(((slot[2] + slot[1] - slot[0], name) for name, slot in self.drops.items()), reverse=True)
        parts = [f"+{n:,} {name}" for n, name in gains if n > 0]
        if self.eats:
            parts.append(f"ate {self.eats}x")
        minutes = max(1, round((time.time() - self.drops_since) / 60))
        key = self.activity_key()
        # The game's own figure (status bar, inputs already subtracted) beats the bot's estimate.
        game_hour = sorted(self.gp_samples)[len(self.gp_samples) // 2] if len(self.gp_samples) >= 3 else None
        if game_hour is not None:
            # The first game figure replaces the old estimate, which left out the inputs used up.
            old = self.gp_rate.get(key) if key in self.gp_game_keys else None
            self.gp_game_keys.add(key)
            rate = game_hour / 60
            self.gp_rate[key] = round(rate if old is None else 0.5 * old + 0.5 * rate, 1)
        if parts:
            # Fallback without the game's figure: pieces x the sale price the bot has seen (unknown
            # prices count 0), inputs not subtracted.
            worth = sum(n * ((self.stock(name, 30 * 86400) or {}).get("price") or 0) for n, name in gains if n > 0)
            for n, name in gains:  # items without a known price get looked up in the bank later
                if n > 0 and not (self.stock(name, 30 * 86400) or {}).get("price") and name not in self.price_queue:
                    self.price_queue.append(name)
            rate = worth / max(minutes, 1)
            if game_hour is None and minutes >= 3 and worth >= 0:
                old = self.gp_rate.get(key)
                self.gp_rate[key] = round(rate if old is None else 0.5 * old + 0.5 * rate, 1)
            if game_hour is not None:
                money = f" ({game_hour:+,} GP/hr by the game)"
            else:
                money = f" (~{worth:,.0f} GP, {rate:,.0f} GP/min)" if worth else ""
            self.event("loot", f"{where}, {minutes} min: " + ", ".join(parts) + money)
        self.drops, self.eats, self.drops_since, self.gp_samples = {}, 0, time.time(), []
        self.note_gold()

    def note_gold(self) -> None:
        """Gold from the top bar into the journal, with the change since the last line."""
        gold = self.g.read_gold()
        if gold is None or gold == self.gold_seen:
            return
        change = f" ({gold - self.gold_seen:+,} since the last look)" if self.gold_seen is not None else ""
        self.gold_seen = gold
        self.event("gold", f"{gold:,} GP{change}", gp=gold)

    def note_levels(self, levels: dict[str, int]) -> None:
        """Remember skill levels; a level gained is a line in the journal.

        Levels only go up, one or two at a time. A lower read or a big jump is a misread unless
        the same value comes back a second time."""
        for skill, level in levels.items():
            if skill not in SKILLS:
                continue
            old = self.levels.get(skill)
            if old is None:
                self.levels[skill] = level
            elif old < level <= old + 2 or (level > old and self.level_pending.get(skill) == level):
                self.levels[skill] = level
                self.level_pending.pop(skill, None)
                self.event("level", f"{skill} level {level}" + (f" (was {old})" if level > old + 1 else ""))
            elif level > old:
                self.level_pending[skill] = level

    def update_config(self, cfg: dict) -> None:
        """Apply a new config while running (from the GUI)."""
        changed = cfg["rotation"] != self.rotation
        self.cfg = cfg
        self.rotation = cfg["rotation"]
        if self.idx >= len(self.rotation):
            self.idx = 0
        if changed and self.picked_start:
            self.begin_activity()  # new durations take effect now
        self.last_smelt_check = 0.0  # re-check smelting with the new rules
        log.info("config updated")

    # --- activities -----------------------------------------------------------------

    @property
    def current(self) -> dict:
        return self.rotation[self.idx]

    def start_current(self) -> bool:
        act = self.current
        skill = act["skill"]
        log.info("start activity %d/%d: %s %s", self.idx + 1, len(self.rotation), skill, act.get("target", ""))
        self.target_name = ""
        if not act.get("enabled", True):
            self.skip_reason = "switched off"
            return False
        guard = self.bank_full()
        if guard and skill not in SAFE_WHEN_FULL:
            self.skip_reason = f"bank full: {skill} could bring new kinds of items"
            return False
        cond = act.get("only_while_below")  # ["Fletching", 5]: a helper step for another skill's goal
        if cond:
            level = self.g.skill_level(cond[0])
            if level is not None and level >= int(cond[1]):
                self.skip_reason = f"{cond[0]} is level {level}, no longer needed"
                self.last_reselect = time.time()
                return False
        every = act.get("every_hours")  # a low-priority step: at most once per this many hours
        key = f"{skill} {act.get('target', '')}".strip()
        # Only when the turn begins: a level-up re-check in the middle of the turn found its own start
        # "too recent" and cut the turn short.
        if (every and time.time() - self.step_runs.get(key, 0) < float(every) * 3600
                and self.step_runs.get(key, 0) < self.started_at - 5):
            left = (float(every) * 3600 - (time.time() - self.step_runs.get(key, 0))) / 3600
            self.skip_reason = f"low priority: again in {left:.1f} h"
            self.last_reselect = time.time()
            return False
        cond = act.get("only_while_stock_below")  # ["Normal Log", 300]: keep another skill supplied
        if cond:
            have = self.known_count(cond[0])
            if have is not None and have >= int(cond[1]):
                self.skip_reason = f"{have} {cond[0]} in the bank, enough (goal {cond[1]})"
                self.last_reselect = time.time()
                return False
        if skill == "Fishing":
            ok = self.start_fishing()
        elif skill == "Cooking":
            ok = self.start_cooking()
        elif skill == "Thieving":
            ok = self.start_thieving()
        elif skill == "Firemaking":
            ok = self.start_firemaking()
        elif skill == "Combat":
            ok = self.start_combat()
        elif skill == "Hunting":
            ok = self.start_hunting(act)
        elif skill == "Agility":
            st = self.status
            if st and st.skill == "Agility":
                ok = True
            else:
                name = self.g.start_agility(self.cfg.get("agility", {}).get("min_success", 100))
                ok = name is not None
                self.target_name = name or ""
                if not ok:
                    self.skip_reason = "no Agility course that never fails"
        elif skill in RECIPE_SKILLS:
            ok = self.start_recipe_skill(act)
        elif skill in GRIDS:
            target = act.get("target", "best")
            if skill == "Mining" and target.split()[:1] == ["smeltable"]:
                target = self.smeltable_ore() or "best Ore Vein"
            if guard and skill != "Astrology":
                # No upgrades now: only a card done before, so its items already have a place.
                words = target.split()
                kind = words[1].lower() if len(words) > 1 and words[0].lower() == "best" else ""
                known = [n for n in self.known_targets.get(skill, [])
                         if (kind in n.lower() if kind else True)
                         and (words[0].lower() in ("best", "least", "random") or similar(n, target) >= 0.8)]
                if not known:
                    self.skip_reason = f"bank full: no {skill} card done before"
                    return False
                target = known[-1]
            name = self.g.start_card(skill, target)
            self.target_name = name or ""
            ok = name is not None
            if self.g.last_cards.get(skill):
                self.cards[skill] = [n for n, _ in self.g.last_cards[skill]]
            if ok:
                done = self.known_targets.setdefault(skill, [])
                if name in done:
                    done.remove(name)
                done.append(name)  # most recent last
                self.save_state()
        else:
            log.error("skill %s is not supported yet", skill)
            ok = False
        self.last_reselect = time.time()
        return ok

    ORE_FOR_BAR = {"Steel Bar": "Iron Ore", "Bronze Bar": "Copper Ore", "Adamant Bar": "Adamantite Ore",
                   "Rune Bar": "Runite Ore"}

    def smeltable_ore(self) -> str | None:
        """The ore Mining should go for: one the smithy can smelt now, furthest below its smelting rule.

        The highest ore gives the most Mining xp, but Adamantite is useless while Smithing can only
        make Copper, Iron and Silver bars. When every useful ore is above its rule, None: then the
        highest ore is fine (the surplus can be sold).
        """
        if not self.smelt_counts or time.time() - getattr(self, "smelt_read_at", 0.0) > 1800:
            counts = self.g.read_smelting()
            if counts:
                self.smelt_counts, self.smelt_read_at = counts, time.time()
        rules = {r["bar"]: r["min_ore"] for r in self.cfg.get("smelting", {}).get("rules", [])}
        keep = self.cfg.get("sell", {}).get("keep_ores", 200)
        cards = self.cards.get("Mining") or self.known_targets.get("Mining", [])
        running = self.status.detail if self.status and self.status.skill == "Mining" else ""
        want = []
        for bar, (have, _) in self.smelt_counts.items():
            ore = self.ORE_FOR_BAR.get(bar, bar.replace(" Bar", " Ore"))
            tier = next((i for i, c in enumerate(cards) if similar(c, ore) >= 0.9), None)
            if cards and tier is None:
                continue  # that ore is not unlocked (or never seen) in Mining
            goal = rules.get(bar, keep)
            if have < goal:
                want.append((have / max(goal, 1), -(tier or 0), ore, have, goal))
        # Steel, Mithril and better bars take Coal besides their ore. Only the ore was counted, so the
        # smithy kept falling back to Iron Bars for want of Coal.
        coal_bars = [b for b in self.smelt_counts if b in ("Steel Bar", "Mithril Bar", "Adamant Bar", "Adamantite Bar",
                                                          "Rune Bar", "Runite Bar")]
        coal_tier = next((i for i, c in enumerate(cards) if similar(c, "Coal") >= 0.9), None)
        if coal_bars and (coal_tier is not None or not cards):
            coal = self.known_count("Coal") or 0
            goal = self.cfg.get("mining", {}).get("coal_goal", 400)
            if coal < goal:
                want.append((coal / max(goal, 1), -(coal_tier or 0), "Coal", coal, goal))
        # Runecrafting runs on Rune Essence, which only Mining brings in. Its stock is the first
        # ingredient of the rune recipes, as the last Runecrafting turn read it.
        if any(a["skill"] == "Runecrafting" and a.get("enabled", True) for a in self.rotation):
            runes = [raw for cat, name, raw in self.recipe_raw.get("Runecrafting", []) if name.endswith(" Rune")]
            known = self.stock("Rune Essence", 6 * 3600)
            have = max(runes) if runes else (known["count"] if known else None)
            goal = self.cfg.get("runecrafting", {}).get("essence_goal", 200)
            tier = next((i for i, c in enumerate(cards) if similar(c, "Rune Essence") >= 0.85), None)
            if have is not None and have < goal and (tier is not None or not cards):
                # Half weight: with both at 0 Iron Ore won the tie all night and Runecrafting stood still.
                want.append((have / max(goal, 1) * 0.5 - 0.01, -(tier or 0), "Rune Essence", have, goal))
        if not want:
            log.info("mining: every ore the smithy can use is stocked - highest ore instead")
            return None
        # Stay on the running ore while it is still short: no hopping between ores every check.
        keep_on = next((w for w in want if running and similar(running.split(" Ill")[0], w[2]) >= 0.85), None)
        pick = keep_on or min(want)
        log.info("mining: %s for %s (%d of %d)", pick[2], "Runecrafting" if pick[2] == "Rune Essence" else
                 ("better bars" if pick[2] == "Coal" else "smelting"),
                 pick[3], pick[4])
        return pick[2]

    def begin_activity(self) -> None:
        """Start the clock for the current activity with a fresh (random) duration."""
        lo, hi = minutes_range(self.current)
        self.duration = 0.0
        if hi > 0 and len(self.rotation) > 1:
            factor, level = self.level_factor(self.current["skill"])
            # The floor keeps a high skill from getting a token minute; a step set shorter than the
            # floor (a 5-10 min bush) keeps its own lower bound.
            floor = min(self.cfg.get("min_turn_minutes", 8), lo)
            money = self.money_factor()
            if money != 1.0:
                log.info("%s earns %s GP/min while saving -> turn x%.1f", self.activity_key(),
                         self.gp_rate.get(self.activity_key()), money)
            self.duration = max(random.uniform(lo, hi) * factor * money, floor) * 60
        self.started_at = time.time()
        self.failures = 0
        self.level = None
        if self.duration:
            log.info("%s for %.0f min%s", self.current["skill"], self.duration / 60,
                     f" (level {level}, x{factor:.2f})" if abs(factor - 1) > 0.01 else "")
        self.save_state()

    def skill_rank(self, skill: str) -> int | None:
        """Level that stands for a rotation step: Combat counts as its lowest melee stat."""
        if skill == "Combat":
            known = [self.levels[s] for s in ("Attack", "Strength", "Defence") if s in self.levels]
            return min(known) if known else None
        return self.levels.get(skill)

    def level_factor(self, skill: str) -> tuple[float, int | None]:
        """How much longer (or shorter) a turn of `skill` is than its configured minutes.

        Compared with the middle level of the rotation: a low skill gets more time, a high one a
        bit less, as the square root of the ratio and within 0.6 to 1.6 times."""
        level = self.skill_rank(skill)
        if not self.cfg.get("level_scaling", True) or level is None:
            return 1.0, level
        ranks = sorted(r for r in (self.skill_rank(a["skill"]) for a in self.rotation if a.get("enabled", True))
                       if r is not None)
        if len(ranks) < 3:
            return 1.0, level
        middle = ranks[len(ranks) // 2]
        return min(1.6, max(0.6, (middle / max(level, 1)) ** 0.5)), level

    def cycle_state(self) -> dict | None:
        """Where the rotation is, for the state file: a restart carries on from here."""
        if not self.picked_start:
            return self.saved_cycle  # nothing chosen yet in this run: keep what was saved
        act = self.current
        return {"idx": self.idx, "skill": act["skill"], "target": act.get("target", ""),
                "started_at": self.started_at, "duration": self.duration}

    def pick_start(self, st: Status) -> None:
        """First tick: carry on with what the game is doing if it is in the rotation, else start at the top."""
        self.picked_start = True
        skills = [a["skill"] for a in self.rotation]
        # A restart is not a new start: carry on at the step and with the time that was left.
        # Without this every restart gave the running activity a fresh full turn, and a day with
        # many restarts was a day of woodcutting only.
        c = self.saved_cycle
        if (c and c.get("idx", -1) < len(self.rotation) and self.rotation[c["idx"]]["skill"] == c.get("skill")
                and self.rotation[c["idx"]].get("target", "") == c.get("target", "")):
            self.idx, self.started_at, self.duration = c["idx"], c["started_at"], c["duration"]
            left = self.seconds_to_switch()
            log.info("resuming the rotation at step %d (%s), %s", self.idx + 1, c["skill"],
                     "its time is up" if left is not None and left <= 0 else
                     f"{left / 60:.0f} min left" if left is not None else "no time limit")
            self.verify = True
            self.failures = 0
            self.level = None
            return
        if self.cfg.get("start_with_current", True) and st.skill in skills:
            self.idx = skills.index(st.skill)
            log.info("game is already doing %s: continuing with it", st.skill)
            self.verify = True  # same skill is not enough: a better card may be unlocked
        else:
            self.idx = 0
        self.begin_activity()

    def start_fishing(self, recheck: bool = False) -> bool:
        """Pick a spot and fish there. `recheck` (already fishing): only move for a newly unlocked best spot."""
        fcfg = self.cfg.get("fishing", {})
        if not fcfg.get("auto_spot", True):
            return True if recheck else self.g.start_fishing()
        aware = fcfg.get("cooking_aware", True)
        if aware and not self.food:
            self.read_food()  # which recipes are unlocked = which fish can be cooked
        spots = self.g.survey_fishing()
        known = set(self.known_spots)
        self.known_spots |= {s.name for s in spots}  # accumulate: one survey can miss a location
        fish = cookable = None
        if aware and spots and self.food:
            def entry(name: str):
                return next((v for k, v in self.fish_table.items() if similar(k, name) >= 0.8), None)

            if (not self.fish_table or time.time() - self.fish_read_at > 3 * 3600
                    or any(entry(s.name) is None for s in spots)):
                self.fish_table = self.g.survey_fish(spots)  # slow (every spot is opened): rarely
                self.fish_read_at = time.time()
                self.save_state()
            fish = {s.name: entry(s.name) or {} for s in spots}
            cookable = set(self.food)
            waiting = self.waiting_fish()
            if waiting:
                log.info("cannot be cooked yet (Cooking %s, next recipe at %s): %s -> prefer spots with fish Cooking can use",
                         self.cooking_level or "?", self.cooking_next or "?",
                         ", ".join(f"{n} {c}" for n, c in sorted(waiting.items(), key=lambda kv: -kv[1])))
        if recheck:
            best = self.g.choose_spot(spots, dict(fcfg, variety=0), False, fish, cookable)
            current = self.status.detail if self.status else ""
            # Stay when already on the best spot, or when the best one is not new (the bot may have
            # chosen another spot on purpose, for variety or bait). With no earlier survey (bot just
            # started) "not the best" is enough to move.
            if not best or similar(best.name, current) >= 0.8 or (known and any(similar(best.name, k) >= 0.8 for k in known)):
                self.g._location_popup(False)
                return True
            log.info("better fishing spot: %s (+%d xp)", best.name, best.xp)
            spot = best
        else:
            spot = self.g.choose_spot(spots, fcfg, self.bait_needed, fish, cookable, self.food_heals_if_low())
        if not spot:
            log.warning("Fishing: no spots read; fishing at the current spot")
            return self.g.start_fishing()
        if self.bait_needed and similar(spot.name, fcfg.get("bait_spot", "")) >= 0.8:
            log.info("out of bait: fishing at %s for bait boxes (open them in the bank)", spot.name)
        ok, self.bait_needed = self.g.start_fishing_at(spot, fcfg)
        if self.bait_needed:
            log.warning("no bait left: next fishing run goes to %s", fcfg.get("bait_spot") or "the best spot")
        self.target_name = spot.name if ok else ""
        return ok

    # --- food: cooking keeps a stock, thieving uses it ---------------------------------

    def read_food(self) -> list:
        """Survey the Cooking page: updates the food stock the GUI shows and Thieving uses."""
        recipes = self.g.survey_cooking(self.cfg.get("cooking", {}).get("categories", ["Basic Food"]))
        for r in recipes:
            self.food[r.name] = (r.stock, r.heals)
            for spot_fish in self.fish_table.values():  # the recipe shows the raw fish left: keep the table current
                for fish in spot_fish:
                    if similar(fish, r.name) >= 0.85:
                        spot_fish[fish] = r.raw
        if recipes:
            self.food = {k: v for k, v in self.food.items() if re.fullmatch(r"[A-Z][a-z']+(?: [A-Za-z']+)*", k) and len(k.split()[0]) > 2}
            self.food_read_at = time.time()
            self.cooking_level = self.g.skill_level("Cooking") or self.cooking_level
            ahead = [n for n in self.g.cooking_locked if self.cooking_level is None or n > self.cooking_level]
            self.cooking_next = min(ahead) if ahead else None
            self.save_state()
        return recipes

    def waiting_fish(self) -> dict[str, int]:
        """Fish piled up in the bank that Cooking cannot cook yet (its level is too low)."""
        limit = self.cfg.get("fishing", {}).get("backlog_limit", 200)
        return Game.waiting_fish(self.fish_table, set(self.food), limit) if self.fish_table and self.food else {}

    def start_cooking(self) -> bool:
        """Cook the best-healing food there are ingredients for."""
        ccfg = self.cfg.get("cooking", {})
        ready = [r for r in self.read_food() if r.raw >= ccfg.get("min_raw", 10)]
        if self.bank_full():
            ready = [r for r in ready if (r.stock or 0) > 0]  # a food we have never made is a new item
        if not ready:
            self.skip_reason = "nothing to cook"
            return False
        best = max(ready, key=lambda r: r.heals)
        waiting = self.waiting_fish()
        if waiting:
            # Fish is piling up that needs a higher Cooking level: keep cooking until the
            # ingredients run out instead of leaving after the usual minutes.
            self.duration = 0.0
            log.info("Cooking level %s, next recipe at %s; waiting on it: %s -> cook until the ingredients run out",
                     self.cooking_level or "?", self.cooking_next or "?",
                     ", ".join(f"{n} {c}" for n, c in sorted(waiting.items(), key=lambda kv: -kv[1])))
        st = self.status
        if st and st.skill == "Cooking" and similar(st.detail, best.name) >= 0.8:
            log.info("Cooking: %s already running", best.name)
        elif not self.g.cook(best):
            return False
        self.target_name = best.name
        return True

    def start_firemaking(self) -> bool:
        """Burn the highest log recipe there are logs for; the list is in unlock order."""
        logs = self.g.survey_firemaking()
        self.fire_logs = {r.name: r.raw for r in logs}
        for r in logs:
            self.note_stock(r.name, r.raw)
        ahead = self.g.firemaking_locked
        self.fire_next = min(ahead) if ahead else None
        self.save_state()
        fcfg = self.cfg.get("firemaking", {})
        keep = fcfg.get("keep", 200)  # logs to leave of every kind: shop upgrades and Fletching need them
        ready = [r for r in logs if r.raw - keep >= fcfg.get("min_logs", 10)]
        if not ready:
            self.skip_reason = f"no logs to burn above the reserve of {keep}"
            return False
        best = ready[-1]
        st = self.status
        if st and st.skill == "Firemaking" and similar(st.detail, best.name) >= 0.8:
            log.info("Firemaking: %s already burning", best.name)
        elif not self.g.burn(best):
            return False
        self.target_name = best.name
        if keep and len(self.rotation) > 1:
            # The game burns until the logs are gone; leave in time to keep the reserve.
            budget = (best.raw - keep) * self.g.burn_interval
            self.duration = min(self.duration, budget) if self.duration else budget
            log.info("Firemaking: %d %s to burn, keep %d -> at most %.0f min", best.raw - keep, best.name, keep,
                     self.duration / 60)
        return True

    def tend_fire(self) -> bool:
        """While burning: look at the log count every couple of minutes and stop at the reserve.
        The time estimate alone burned 734 Normal Logs down to 37 (it burns faster than the page's
        interval says), which left Fletching without logs. True = left Firemaking."""
        st = self.status
        if not (st and st.skill == "Firemaking") or time.time() - self.fire_look < 120:
            return False
        self.fire_look = time.time()
        keep = self.cfg.get("firemaking", {}).get("keep", 200)
        logs = self.g.survey_firemaking()
        for r in logs:
            self.note_stock(r.name, r.raw)
        self.fire_logs = {r.name: r.raw for r in logs} or self.fire_logs
        burning = next((r for r in logs if similar(r.name, self.target_name or st.detail) >= 0.8), None)
        if burning and burning.raw <= keep:
            log.info("Firemaking: %d %s left, the reserve is %d -> stop", burning.raw, burning.name, keep)
            self.g.stop_burning()
            if len(self.rotation) > 1:
                self.next_activity(f"{burning.name} down to the reserve")
                return True
        return False

    @staticmethod
    def goal_level(act: dict) -> int | None:
        """Level at which an activity is done: `until_level`, or a target like "level 5"."""
        if act.get("until_level"):
            return int(act["until_level"])
        m = re.search(r"level\s*(\d+)", str(act.get("target", "")), re.I)
        return int(m.group(1)) if m else None

    def start_recipe_skill(self, act: dict) -> bool:
        """Make something on a recipe page, mostly to train the skill up to a goal level."""
        skill, goal = act["skill"], self.goal_level(act)
        if goal:
            level = self.g.skill_level(skill)
            if level is not None and level >= goal:
                self.skip_reason = f"{skill} is level {level}, goal {goal} reached"
                return False
        st = self.status
        if st and st.skill == skill:
            log.info("%s: already making %s", skill, st.detail or "something")
            self.target_name = st.detail
            return True
        recipes = self.g.survey_recipes(skill, act.get("categories"))
        if recipes:  # what each recipe's first ingredient stands at, for Mining (Rune Essence)
            self.recipe_raw[skill] = [(r.category, r.name, r.raw) for r in recipes]
            self.save_state()
        ready = [r for r in recipes if r.raw >= r.need]
        if skill == "Herblore" and not ready and recipes and time.time() - self.vials_tried > 3600:
            # Every potion starts with a Vial. Make some: sand (Shop) -> Molten Glass -> Pack of Vials.
            self.vials_tried = time.time()
            if self.supply_vials():
                recipes = self.g.survey_recipes(skill, act.get("categories"))
                ready = [r for r in recipes if r.raw >= r.need]
        # Categories run from basic to advanced: stay in the first one that has something to make
        # (cheap, few new item types in the bank), highest recipe first. Only the first ingredient
        # is read, so a recipe can still fail on a second one: check that the game really
        # started, else try the next.
        # Within the first category that has something to make: the recipe made longest ago first
        # (variation: Runecrafting made only Mind Runes), and among those the most xp per make.
        made = self.recipes_made.setdefault(skill, {})
        order = []
        for category in dict.fromkeys(r.category for r in recipes):
            order += sorted((r for r in ready if r.category == category),
                            key=lambda r: (made.get(r.name, 0), -(r.xp or 0)))
        wanted = str(act.get("target", ""))
        order.sort(key=lambda r: similar(r.name, wanted) < 0.8)  # a named recipe goes first
        if act.get("only") and wanted:  # a supply step: that recipe or nothing
            order = [r for r in order if similar(r.name, wanted) >= 0.8]
        # A recipe that did not start lacks an ingredient the bot cannot see (only the first is
        # read): leave it alone for two hours instead of trying it every round.
        failed = getattr(self, "failed_recipes", {})
        self.failed_recipes = failed
        order = [r for r in order if time.time() - failed.get(f"{skill}|{r.name}", 0) > 7200]
        for r in order:
            if not self.g.create_recipe(skill, r) or self.g.dry_run:
                continue
            time.sleep(2.5)
            if self.g.status().skill == skill:
                self.target_name = r.name
                failed.pop(f"{skill}|{r.name}", None)
                made[r.name] = time.time()
                self.save_state()
                return True
            failed[f"{skill}|{r.name}"] = time.time()
            log.info("%s: %s did not start (another ingredient missing?) - not again for 2 h", skill, r.name)
        self.skip_reason = f"nothing to make for {skill}"
        return False

    # --- combat -----------------------------------------------------------------------

    def choose_stance(self, wanted: str) -> str | None:
        """"auto": the stance that trains the lowest of Attack / Strength / Defence."""
        # Which stat a stance trains depends on the weapon, and the game no longer shows that in a
        # tooltip the bot can read. So "keep" (the default; "auto" in old configs) leaves the stance
        # the user set in the game; a named stance is clicked when the weapon has it.
        if not wanted or wanted.lower() in ("keep", "auto"):
            return None
        if wanted.lower() != "balance":
            return wanted
        stats = {s: self.levels.get(s) for s in ("Attack", "Strength", "Defence")}
        if any(v is None for v in stats.values()):
            return None
        lowest = min(stats, key=lambda s: stats[s])
        weapon = self.weapon.get("type") or "unknown"
        known = self.stance_map.get(weapon)
        if not known and self.real_mouse_ok():
            known = {k: v for k, v in self.g.stance_trains_real().items() if v}
            if known:
                self.stance_map[weapon] = known
                self.save_state()
        # Melee convention until the tooltips have been read: Stab/Accurate train Attack,
        # Slash/Crush/Powerful Strength, Block Defence.
        table = known or {"Stab": ["Attack"], "Accurate": ["Attack"], "Slash": ["Strength"], "Crush": ["Strength"],
                          "Powerful": ["Strength"], "Block": ["Defence"]}
        picks = [s for s, trains in table.items() if trains == [lowest]] or \
                [s for s, trains in table.items() if lowest in trains]
        on_page = [ln.text.strip() for ln in self.g.stance_buttons()]
        pick = next((s for s in picks if any(similar(t, s) >= 0.9 for t in on_page)), None)
        if pick:
            log.info("stance: %s is lowest (%s) -> %s", lowest, ", ".join(f"{k} {v}" for k, v in stats.items()), pick)
        return pick

    def note_task(self, task: tuple[str, int, int]) -> None:
        """Remember the Slayer task; a new monster is a line in the journal."""
        if not self.slayer or self.slayer[0] != task[0]:
            if self.slayer:
                self.event("slayer", f"Task done: {self.slayer[0]} ({self.slayer[2]} kills)")
            self.event("slayer", f"New task: {task[0]}, {task[1]} of {task[2]}")
        changed = list(task) != self.slayer
        self.slayer = list(task)
        if changed:
            self.save_state()

    def start_combat(self) -> bool:
        """Fight the Slayer task's monster (or the configured one), with enough food in the Combat slot."""
        ccfg = self.cfg.get("combat", {})
        area, monster = ccfg.get("area", "Corrupted Farmlands"), ccfg.get("monster", "Evil Plant")
        need = ccfg.get("min_food", 20)
        st = self.status
        fighting = bool(st and st.skill == "Combat")
        if not self.g.open_combat():
            return False
        v = self.vitals = self.g.combat_vitals()
        if v.food_heal:
            self.equipped_heals.add(v.food_heal)
        if (v.food_count or 0) < need:
            # Best food first; a failed bank visit ("page did not open") or a stack that sits in
            # another skill's slot moves on to the next food instead of skipping the fight.
            for heals, name in self.food_choices(need)[:6]:
                log.info("combat: %d food equipped, need %d -> equip %s (+%d HP)", v.food_count or 0, need, name, heals)
                self.g.equip_food(name, "Combat")
                self.note_food_missing()
                if not self.g.open_combat():
                    return False
                v = self.vitals = self.g.combat_vitals()
                if v.food_heal:
                    self.equipped_heals.add(v.food_heal)
                if (v.food_count or 0) >= need:
                    break
        if (v.food_count or 0) < need:
            self.skip_reason = f"Combat needs {need} food, {v.food_count or 0} equipped"
            return False
        v = self.eat_up(v, self.g.combat_eat, self.g.combat_vitals, "combat")
        if v.hp is not None and v.hp_max and v.hp >= v.hp_max:
            self.g.hp_markers(v.hp, v.hp_max)  # full HP before the fight: measure the bar for the markers
        # Slayer mode: the task names the monster, and "Jump To Enemy" finds it. Without a readable
        # task the configured area and monster are the fallback.
        task = self.g.slayer_task() if ccfg.get("mode", "slayer") == "slayer" else None
        if task:
            self.note_task(task)
            monster = task[0]
            log.info("combat: Slayer task %s %d/%d", *task)
        hard = self.too_hard_now(monster)
        if hard:
            fallback = ccfg.get("monster", "Evil Plant")
            if task and similar(fallback, monster) < 0.8 and not self.too_hard_now(fallback):
                log.info("combat: %s is too strong (%s) - fighting %s instead", monster, hard, fallback)
                task, monster = None, fallback
            else:
                self.skip_reason = f"{monster} is too strong for now ({hard})"
                return False
        self.last_hp, self.worst_drop = None, 0
        self.fight_log = {"monster": monster, "t": time.time(), "food": v.food_count or 0, "low": 0}
        if fighting and similar(st.detail, monster) >= 0.8:
            log.info("Combat: already fighting %s", monster)
        else:
            stance = self.choose_stance(ccfg.get("stance", "auto"))
            if stance and similar(self.g.current_stance() or "", stance) < 0.9:
                if self.g.set_stance(stance):
                    self.event("activity", f"Combat stance {stance} (trains the lowest melee skill)")
            if not (self.g.jump_to_enemy() if task else self.g.start_fight(area, monster)):
                return False
        self.target_name = monster
        return True

    def too_hard_now(self, monster: str) -> str | None:
        """Why `monster` is skipped for now (it ate the food too fast), or None."""
        hours = self.cfg.get("combat", {}).get("too_hard_hours", 24)
        for name, entry in self.too_hard.items():
            if similar(name, monster) >= 0.85 and time.time() - entry.get("t", 0) < hours * 3600:
                return entry.get("why", "ate the food too fast")
        return None

    def food_budget_spent(self, v) -> str | None:
        """Leave a fight before the food runs out: when only `food_reserve` pieces are left, or when
        the fight eats more than `max_food_per_min` (both the game's auto-eat and the bot's own).
        Rainbow Slime ate the whole stack in nine minutes and left 6 of 320 HP."""
        ccfg = self.cfg.get("combat", {})
        fl = getattr(self, "fight_log", None)
        if not fl or v.food_count is None:
            return None
        if v.food_count > fl["food"]:  # more food was equipped: count from here
            fl["food"], fl["t"] = v.food_count, time.time()
        used = fl["food"] - v.food_count
        minutes = (time.time() - fl["t"]) / 60
        rate = used / minutes if minutes >= 3 else 0.0
        if rate > ccfg.get("max_food_per_min", 2.0):
            return f"ate {used} food in {minutes:.0f} min"
        # Two looks in a row at or below the reserve: one misread count does not end a fight.
        fl["low"] = fl["low"] + 1 if v.food_count <= ccfg.get("food_reserve", 5) else 0
        if fl["low"] >= 2 and used > 0:
            return f"only {v.food_count} food left after eating {used} in {minutes:.0f} min"
        return None

    def note_auto_eat_short(self, max_hit: int, auto: int) -> None:
        """The opponent hits harder than the auto-eat line: one hit can go past it. From then on
        the Auto Eat upgrades in the Shop get priority (for a week after the last such fight)."""
        first = not self.auto_eat_short or time.time() - self.auto_eat_short.get("t", 0) > 7 * 86400
        who = self.target_name or self.current["skill"]
        if first or self.auto_eat_short.get("who") != who:
            self.event("warn", f"{who} hits up to {max_hit}, auto-eat only starts at {auto} HP: "
                               "Auto Eat upgrades get priority in the Shop")
        self.auto_eat_short = {"t": time.time(), "who": who, "max_hit": max_hit, "auto": auto}
        self.save_state()

    def auto_eat_wanted(self) -> bool:
        return bool(self.auto_eat_short) and time.time() - self.auto_eat_short.get("t", 0) <= 7 * 86400

    def backup_eat_limit(self, v, eat_below_pct: float) -> float:
        """HP under which the bot eats itself, next to the game's own auto-eat."""
        auto, max_hit = self.g.hp_markers(v.hp, v.hp_max)
        if auto is None and max_hit is None:
            return v.hp_max * eat_below_pct / 100  # no markers read: the old percentage
        limit = max(auto or 0, round((max_hit or 0) * 1.1))
        if auto and max_hit and max_hit >= auto:
            self.note_auto_eat_short(max_hit, auto)
        if (auto, max_hit) != getattr(self, "last_markers", None):
            self.last_markers = (auto, max_hit)
            log.info("HP bar: auto-eat at %s, max hit %s -> the bot eats below %d", auto, max_hit, limit)
        return limit

    def tend_fight(self) -> bool:
        """During a fight: eat when HP is low; run when the food is gone. True = left Combat."""
        ccfg = self.cfg.get("combat", {})
        if not self.g.open_combat():
            return False
        v = self.vitals = self.g.combat_vitals()
        if v.food_heal:
            self.equipped_heals.add(v.food_heal)
        if v.hp is None or not v.hp_max:
            log.debug("combat: HP not readable this tick")
            return False
        # The game eats by itself below its auto-eat line (white marker on the HP bar). The bot only
        # eats as a backup: below that line (auto-eat did not fire) or within reach of the monster's
        # max hit (orange marker), where one hit could end the fight. Eating up to 2.2 times the
        # worst drop between two looks ate 72 pieces in nine minutes.
        if self.last_hp is not None and self.last_hp > v.hp:
            self.worst_drop = max(self.worst_drop, self.last_hp - v.hp)
        limit = self.backup_eat_limit(v, ccfg.get("eat_below", 50))
        eaten = 0
        while v.hp is not None and v.hp < limit and (v.food_count or 0) > 0 and eaten < 2:
            log.info("combat: HP %d/%d (eat below %d) -> eat", v.hp, v.hp_max, limit)
            if not self.g.combat_eat() or self.g.dry_run:
                break
            eaten += 1
            self.eats += 1
            v = self.vitals = self.g.combat_vitals()
            if v.food_heal:
                self.equipped_heals.add(v.food_heal)
        self.last_hp = v.hp
        if (v.hp, v.food_count) != self.saved_vitals:
            self.saved_vitals = (v.hp, v.food_count)
            self.save_state()
        spent = self.food_budget_spent(v)
        if spent and not self.g.dry_run:
            monster = self.target_name or "the fight"
            log.warning("combat: %s - running from %s", spent, monster)
            self.too_hard[monster] = {"t": time.time(), "why": spent}
            self.event("warn", f"Ran from {monster}: {spent}. Skipped for {self.cfg.get('combat', {}).get('too_hard_hours', 24)} h")
            self.g.run_from_combat()
            if len(self.rotation) > 1:
                self.next_activity("the fight eats the food too fast")
            return True
        if v.food_count == 0 and len(self.rotation) > 1:
            self.next_activity("out of food")
            return True
        auto, max_hit = getattr(self, "last_markers", None) or (None, None)
        # Run when one more hit could end it. Under 25% is normal now (the game eats at its own
        # line), so the old "below a quarter" rule would run from every fight.
        danger = max_hit if max_hit else v.hp_max * 0.15
        if v.hp is not None and v.hp <= danger and not self.g.dry_run:
            # Still this low after eating: the food cannot keep up with this monster.
            log.warning("combat: HP %d/%d after eating - running", v.hp, v.hp_max)
            self.event("warn", f"Ran from {self.target_name or 'the fight'}: HP {v.hp} of {v.hp_max} after eating")
            if self.target_name:
                self.too_hard[self.target_name] = {"t": time.time(), "why": f"HP {v.hp} of {v.hp_max} after eating"}
            self.g.run_from_combat()
            if len(self.rotation) > 1:
                self.next_activity("fight too dangerous")
            return True
        if ccfg.get("mode", "slayer") == "slayer" and time.time() - self.slayer_read_at > 45:
            self.slayer_read_at = time.time()  # not every look: reading costs time the fight does not wait for
            task = self.g.slayer_task()
            st = self.status
            if task:
                self.note_task(task)
                first = task[0].split()[0].lower()
                if st and st.skill == "Combat" and st.detail and first not in st.detail.lower() \
                        and similar(st.detail, task[0]) < 0.6 and not self.too_hard_now(task[0]):
                    log.info("combat: fighting %s but the Slayer task is %s -> jump", st.detail, task[0])
                    if self.g.jump_to_enemy():
                        self.target_name = task[0]
        return False

    def food_heals_if_low(self) -> dict[str, int] | None:
        """Food -> HP while good food runs low (fewer than `food_goal` pieces healing `good_heal` or
        more), so Fishing goes for Lobster and Salmon instead of the spot with the most xp."""
        fcfg = self.cfg.get("fishing", {})
        good = fcfg.get("good_heal", 150)
        stock = sum((s or 0) for s, h in self.food.values() if (h or 0) >= good)
        if not self.food or stock >= fcfg.get("food_goal", 300):
            return None
        log.info("good food low (%d pieces healing %d+): fishing prefers fish that heals more", stock, good)
        return {name: h for name, (_, h) in self.food.items() if h}

    def food_choices(self, need: int) -> list[tuple[int, str]]:
        """Foods to equip, best healing first: at least `need` cooked, and not missing from the bank
        in the last hour. The Cooking page counts food in the slots too, and a food that ran out
        once stayed in the list: Salmon and Pike were tried while Sardine (334) and Beef (629) waited."""
        if not self.food or time.time() - self.food_read_at > 600:
            self.read_food()
        now = time.time()
        return sorted(((heals, name) for name, (stock, heals) in self.food.items()
                       if (stock or 0) >= need and now - self.food_missing.get(name, 0) > 3600), reverse=True)

    def note_food_missing(self) -> None:
        name = getattr(self.g, "bank_missing", None)
        if name:
            self.food_missing[name] = time.time()

    def eat_up(self, v, eat, read, what: str):
        """Eat before starting: after a fight that ended at 6 of 320 HP the first hit could end the next
        one. Eats up to `eat_up_to` of max HP (the game's auto-eat does the rest)."""
        limit = v.hp_max * self.cfg.get("eat_up_to", 0.6) if v.hp_max else None
        eaten = 0
        while v.hp is not None and limit and v.hp < limit and (v.food_count or 0) > 0 and eaten < 10:
            if not eat() or self.g.dry_run:
                break
            eaten += 1
            self.eats += 1
            time.sleep(0.5)
            v = self.vitals = read()
        if eaten:
            log.info("%s: ate %d before starting, HP %s/%s", what, eaten, v.hp, v.hp_max)
        return v

    def start_thieving(self) -> bool:
        """Thieving costs HP: only with enough food equipped; equip the best cooked food if needed."""
        tcfg = self.cfg.get("thieving", {})
        need = tcfg.get("min_food", 20)
        if not self.g.open_skill("Thieving"):
            return False
        v = self.vitals = self.g.read_vitals()
        if v.food_heal:
            self.equipped_heals.add(v.food_heal)
        for _ in range(2):  # a low count is read again before a trip to the bank
            if (v.food_count or 0) >= need:
                break
            time.sleep(0.6)
            again = self.g.read_vitals()
            if (again.food_count or 0) > (v.food_count or 0):
                v = self.vitals = again
        if (v.food_count or 0) < need:
            # The Cooking page counts food in the slots too: all Trout sat in the Combat slot and the
            # bank had none ("Trout not found"). Try the next food then.
            for heals, name in self.food_choices(need)[:6]:
                log.info("thieving: %d food equipped, need %d -> equip %s (+%d HP)", v.food_count or 0, need, name, heals)
                self.g.equip_food(name, "Thieving")
                self.note_food_missing()
                if not self.g.open_skill("Thieving"):
                    return False
                v = self.vitals = self.g.read_vitals()
                if v.food_heal:
                    self.equipped_heals.add(v.food_heal)
                if (v.food_count or 0) >= need or getattr(self.g, "food_already_equipped", False):
                    break
        if (v.food_count or 0) < need and getattr(self.g, "food_already_equipped", False):
            log.info("thieving: the header reads %s food but the bank says it is equipped - going on",
                     v.food_count)
            v = Vitals(v.hp, v.hp_max, v.food_heal, need)
        if (v.food_count or 0) < need:
            self.skip_reason = f"Thieving needs {need} food, {v.food_count or 0} equipped"
            return False
        v = self.eat_up(v, self.g.eat, self.g.read_vitals, "thieving")
        if v.hp is not None and v.hp_max and v.hp >= v.hp_max:
            self.g.hp_markers(v.hp, v.hp_max)  # full HP: measure the bar for the auto-eat markers
        seed = tcfg.get("herb_seeds", {"location": "Farmers Market", "npc": "Herbalist", "min_success": 45})
        if seed and "Herbs" in self.g.seedless_tabs:
            # Herb seeds are sold nowhere; the Herbalist drops them. Riskier than the usual 90%: the
            # backup eating (auto-eat line, 1.1x the max hit) and the HP watch handle the failures.
            if self.g.go_thieving_location(seed["location"]):
                name = self.g.start_thieving_npc(seed["npc"], seed.get("min_success", 45))
                if name:
                    self.target_name = name
                    if self.announced != f"Thieving: {name}":
                        self.event("activity", f"Thieving the {name} for herb seeds (the herb plots are empty)")
                    return True
        self.best_thieving_location(tcfg.get("min_success", 90))
        name = self.g.start_thieving(tcfg.get("min_success", 90))
        self.target_name = name or ""
        if name is None:
            # Not a failure that three tries can fix: move on in the rotation at once.
            self.skip_reason = f"no Thieving target with a success chance of {tcfg.get('min_success', 90)}% or more"
        return name is not None

    def best_thieving_location(self, min_success: float) -> None:
        """Go to the location whose best safe NPC gives the most xp per second.

        Every location is looked at again every few hours and after a Thieving level-up; in
        between the last survey decides. Stuck in the first location was the old behaviour."""
        level = self.levels.get("Thieving")
        survey = self.thief_survey
        if (not survey.get("spots") or time.time() - survey.get("t", 0) > 3 * 3600
                or survey.get("level") != level):
            spots = {}
            names = [n for n, _ in self.g.thieving_locations()]
            old_spots = survey.get("spots", {})
            for name in names:
                if self.g.go_thieving_location(name):
                    found = self.g.thieving_targets()
                    if not found:  # read again once: the page can still be drawing
                        time.sleep(0.8)
                        found = self.g.thieving_targets()
                    spots[name] = ([(t.name, t.success, t.xp, t.seconds) for t in found]
                                   or old_spots.get(name, []))
            if spots:
                self.thief_survey = survey = {"t": time.time(), "level": level, "spots": spots}
                self.save_state()
                log.info("thieving: %s", "; ".join(
                    f"{loc}: " + ", ".join(f"{n} {s or 0:.0f}% +{x or '?'}" for n, s, x, _ in npcs)
                    for loc, npcs in spots.items()))
        best = None
        for loc, npcs in survey.get("spots", {}).items():
            for n, s, x, secs in npcs:
                if s is not None and s >= min_success:
                    rate = (x or 0) * s / 100 / (secs or 3.0)
                    if not best or rate > best[0]:
                        best = (rate, loc, n)
        if best:
            log.info("thieving: best safe target %s in %s (%.2f xp/s)", best[2], best[1], best[0])
            self.g.go_thieving_location(best[1])

    def start_hunting(self, act: dict) -> bool:
        """Hunt the best animal that works (or `target`), best first; out of traps: make some in
        Crafting ("Basic <trap type>") and try again. An animal that does not start (a tool
        missing) is passed over for a few hours."""
        hcfg = self.cfg.get("hunting", {})
        st = self.status
        target = act.get("target", "best") or "best"
        if not self.g.open_skill("Hunting"):
            return False
        now = time.time()
        level = self.levels.get("Hunting")
        survey = self.hunt_survey
        if (not survey.get("spots") or now - survey.get("t", 0) > 3 * 3600 or survey.get("level") != level):
            # Every unlocked location, every animal with its xp: the best is not always in the first.
            spots = {}
            for loc, _ in self.g.hunting_locations():
                if self.g.go_hunting_location(loc):
                    spots[loc] = self.g.hunting_animal_xp() or survey.get("spots", {}).get(loc, {})
            if spots:
                self.hunt_survey = survey = {"t": now, "level": level, "spots": spots}
                self.save_state()
                log.info("hunting: %s", "; ".join(f"{loc}: " + ", ".join(f"{a} {x:.0f}" for a, x in animals.items())
                                                   for loc, animals in spots.items()))
        pairs = sorted(((x, loc, a) for loc, animals in survey.get("spots", {}).items() for a, x in animals.items()),
                       reverse=True)
        if target != "best":
            pairs = [p for p in pairs if similar(p[2], target) >= 0.8]
        order = [(loc, a) for _, loc, a in pairs if now - self.hunt_failed.get(a, 0) > 3 * 3600]
        if st and st.skill == "Hunting" and order and similar(st.detail.split(" I")[0], order[0][1]) >= 0.7:
            self.target_name = order[0][1]
            return True  # already on the best animal
        for loc, name in order:
            if not self.g.go_hunting_location(loc):
                continue
            got = self.g.start_hunting(name)
            if got is None and self.g.no_traps:
                trap = f"Basic {self.g.trap_type}" if self.g.trap_type else hcfg.get("trap", "Basic Snare")
                made = self.g.make_count("Crafting", "Hunting Traps", trap, hcfg.get("traps_goal", 40),
                                         hcfg.get("trap_seconds", 2.0))
                for item, n in getattr(self.g, "last_inputs", {}).items():
                    self.note_stock(item, n)  # String, Stick ...: the supply steps go by these
                if made:
                    self.event("activity", f"Made {made} {trap} for Hunting")
                    got = self.g.start_hunting(name)
            if got:
                self.target_name = got
                return True
            self.hunt_failed[name] = now
        self.skip_reason = "no animal could be hunted (traps or tools missing)"
        return False

    def tend_thief(self) -> bool:
        """While thieving: eat when HP is low, leave when the food is gone. True = left Thieving."""
        tcfg = self.cfg.get("thieving", {})
        if not self.g.open_skill("Thieving"):
            return False
        v = self.vitals = self.g.read_vitals()
        for _ in range(2):  # one unreadable look is common; read again before giving up
            if v.hp is not None and v.hp_max:
                break
            time.sleep(0.5)
            v = self.vitals = self.g.read_vitals()
        if v.food_heal:
            self.equipped_heals.add(v.food_heal)
        if v.hp is None or not v.hp_max:
            self.hp_unread += 1
            low_before = self.last_hp_frac is not None and self.last_hp_frac < tcfg.get("eat_below", 60) / 100
            if self.hp_unread >= 3 or low_before:
                # HP 22/180 once stayed unread for minutes while Thieving went on: eat blind rather
                # than let the character die. Food is cheap.
                log.warning("thieving: HP unreadable %d times -> eat to be safe", self.hp_unread)
                self.g.eat()
            return False
        self.hp_unread = 0
        self.last_hp_frac = v.hp / v.hp_max
        eaten = 0
        limit = self.backup_eat_limit(v, tcfg.get("eat_below", 60))
        while v.hp is not None and v.hp < limit and (v.food_count or 0) > 0 and eaten < 2:
            log.info("thieving: HP %d/%d -> eat", v.hp, v.hp_max)
            if not self.g.eat() or self.g.dry_run:
                break
            eaten += 1
            self.eats += 1
            v = self.vitals = self.g.read_vitals()
            if v.food_heal:
                self.equipped_heals.add(v.food_heal)
        if (v.hp, v.food_count) != self.saved_vitals:
            self.saved_vitals = (v.hp, v.food_count)
            self.save_state()
        self.food_zero = self.food_zero + 1 if v.food_count == 0 else 0
        if self.food_zero >= 3 and len(self.rotation) > 1:
            self.food_zero = 0
            self.next_activity("out of food")
            return True
        return False

    def emergency_eat(self) -> None:
        """While the user plays: eat during Thieving when HP is low, without leaving their page.

        Only when the Thieving page is already open (the header with HP and Eat is there)."""
        if not (self.status and self.status.skill == "Thieving" and self.current["skill"] == "Thieving"):
            return
        if time.time() - self.last_emergency < 3:
            return
        self.last_emergency = time.time()
        try:
            v = self.g.read_vitals_here()
        except Exception:
            return
        if v and v.hp is not None and v.hp_max and v.hp < v.hp_max * self.cfg.get("thieving", {}).get("eat_below", 60) / 100:
            log.info("thieving: HP %d/%d while you play -> eat", v.hp, v.hp_max)
            self.g.eat(guarded=False)

    def next_activity(self, reason: str) -> None:
        if self.current["skill"] == "Combat" and self.status and self.status.skill == "Combat":
            self.g.run_from_combat()  # never leave a fight running unwatched
        self.flush_drops(self.current["skill"])
        self.idx = (self.idx + 1) % len(self.rotation)
        log.info("switch (%s) -> %s", reason, self.current["skill"])
        self.begin_activity()
        self.try_start()

    def bank_full(self) -> bool:
        """Bank (nearly) full: a new kind of item would be lost, so only stack-safe work is allowed."""
        st = self.status
        if not st or st.bank_used is None or not st.bank_max:
            return False
        return st.bank_max - st.bank_used <= self.cfg.get("bank", {}).get("block_free", 2)

    def try_start(self) -> None:
        self.skip_reason = ""
        if self.start_current():
            self.failures = self.skips = 0
            if self.current.get("every_hours"):
                act = self.current
                self.step_runs[f"{act['skill']} {act.get('target', '')}".strip()] = time.time()
                self.save_state()
            doing = self.current["skill"] + (f": {self.target_name}" if self.target_name else "")
            if doing != self.announced:
                self.announced = doing
                self.event("activity", doing)
            return
        if self.skip_reason and len(self.rotation) > 1:
            # Not a failure: this activity has nothing to do right now. Move on at once.
            if not re.search(r"switched off|no longer needed|goal \d+ reached", self.skip_reason):
                self.event("skip", f"{self.current['skill']} ({self.skip_reason})")
            self.skips += 1
            if self.skips < len(self.rotation):
                self.next_activity(self.skip_reason)
            else:
                log.warning("every activity was skipped (last reason: %s); trying again in a minute", self.skip_reason)
                self.event("warn", f"Every activity was skipped (last reason: {self.skip_reason})")
                self.skips = 0
                self.quiet_until = time.time() + 60
            return
        self.failures += 1
        if self.failures >= 3 and len(self.rotation) > 1:
            self.next_activity("could not start 3x")

    def seconds_to_switch(self) -> float | None:
        if not self.duration or not self.started_at:
            return None
        return max(0.0, self.started_at + self.duration - time.time())

    def seconds_to_smelt_check(self) -> float | None:
        if not self.cfg.get("smelting", {}).get("rules"):
            return None
        every = self.cfg.get("smelting", {}).get("check_minutes", 10) * 60
        return max(0.0, self.last_smelt_check + every - time.time())

    def upgrade_missing(self, entry: dict) -> list[tuple[str, str]]:
        """What an upgrade still needs, as (kind, text): kind is level, gold, material or price."""
        missing = []
        if entry.get("skill") and entry.get("level"):
            level = self.levels.get(entry["skill"])
            if level is None:
                missing.append(("level", f"{entry['skill']} {entry['level']} (own level not read)"))
            elif level < entry["level"]:
                missing.append(("level", f"{entry['skill']} {level}/{entry['level']}"))
        task = entry.get("task")
        if task and task[1] < task[2]:
            missing.append(("level", f"{task[0]} {task[1]}/{task[2]}"))
        costs = list(dict.fromkeys(tuple(c) for c in entry.get("costs", [])))
        # Costs are icons plus "have / need". Gold is in the thousands and its "have" is the gold in
        # the bank, the same on every card; Slayer Coins are in the thousands too but have another
        # "have" (selling items never brings those in).
        plot = bool(entry.get("plot"))

        def is_gold(have: int, need: int) -> bool:
            if plot:
                return True
            if need < 5000:
                return False
            return self.gold is None or abs(have - self.gold) <= max(self.gold * 0.05, 200)

        gold = [c for c in costs if is_gold(*c)]
        keep = self.keep_gold()
        for have, need in costs:
            g = is_gold(have, need)
            if have < need + (keep if g else 0):
                kind = "gold" if g else ("coins" if need >= 5000 else "material")
                text = f"{kind} {have:,}/{need:,}"
                if g and keep:
                    text += f" + {keep:,} reserve"
                missing.append(("gold" if g else "material", text))
        if not costs:  # every upgrade costs something: without a price the card was not read completely
            missing.append(("price", "price not read"))
        return missing

    def upgrade_status(self, entry: dict) -> tuple[str, bool]:
        """What an upgrade still needs as text for the GUI, and whether it can be bought now."""
        missing = self.upgrade_missing(entry)
        return ("; ".join(text for _, text in missing) or "can be bought"), not missing

    # Normal sets from basic to best. The Defence level of each tier is a guess beyond Iron; the bank
    # panel's own requirement is read before anything is put on.
    ARMOUR_TIERS = [("Stone", 1), ("Copper", 10), ("Iron", 20), ("Steel", 30), ("Mithril", 40),
                    ("Adamant", 50), ("Rune", 60), ("Dragon", 70)]
    ARMOUR_PIECES = ["Helmet", "Platebody", "Platelegs", "Gloves", "Boots"]

    def check_armour(self) -> None:
        """Wear the best normal armour set the Defence level allows; smith a missing piece; sell older sets.

        Only normal sets (Stone, Copper, Iron ...). Armour with bonuses from elsewhere is never sold.
        Pieces of a better set that cannot be worn yet stay in the bank.
        """
        acfg = self.cfg.get("armour", {})
        defence = self.levels.get("Defence")
        if defence is None:
            return
        self.task = "checking the armour set"
        changed = []
        pieces = [(piece, "Defence", defence) for piece in self.ARMOUR_PIECES]
        weapon = self.weapon_type()
        attack = self.levels.get("Attack")
        if weapon and attack is not None and acfg.get("weapon", True):
            pieces.append((weapon, "Attack", attack))  # the weapon follows Attack the same way
            if "2H" not in weapon:
                pieces.append(("Shield", "Defence", defence))  # a one-handed weapon leaves the off hand for a shield
        for piece, stat, level in pieces:
            best = [t for t, lvl in self.ARMOUR_TIERS if lvl <= level and (stat == "Defence" or t != "Stone")]
            worn_tier = None
            for tier in reversed(best):  # best wearable first
                name = f"{tier} {piece}"
                if not self.g._open_bank():
                    return
                info = self.g.gear_panel(name)
                self.g._clear_bank_search()
                if info and info["need"] and info["need"][1] > level:
                    continue  # the guess was too low: this tier needs more
                if info and info["worn"]:
                    worn_tier = tier
                    break
                if info and info["stack"] and info["can_equip"]:
                    if self.g.equip_gear(name):
                        worn_tier = tier
                        changed.append(f"put on {name}")
                        break
                if acfg.get("smith", True) and self.g.smith_one(f"{tier} Gear", name):
                    if self.g.equip_gear(name):
                        worn_tier = tier
                        changed.append(f"smithed and put on {name}")
                        break
            if worn_tier and acfg.get("sell_old", True):
                for tier, _ in self.ARMOUR_TIERS:
                    if tier == worn_tier:
                        break  # only sets below the worn one; better ones are kept for later
                    known = self.stock(f"{tier} {piece}", 86400)
                    if known and known["count"] == 0:
                        continue
                    gp = self.g.sell_item(f"{tier} {piece}", lambda have, unit: have)
                    self.note_stock(f"{tier} {piece}", self.g.last_stack or 0)
                    if gp:
                        self.event("sold", f"{self.g.last_sale[0]} {tier} {piece} for {gp:,} GP (old armour)", gp=gp)
        if acfg.get("sell_old", True):
            self.sell_old_gear()
        if not self.g.dry_run:  # a dry run sold and smithed nothing: it is not a check
            self.armour_checked = {"Defence": defence, "Attack": attack, "t": time.time()}
            self.save_state()
        if changed:
            self.event("gear", "Armour: " + ", ".join(changed))

    WEAPON_TYPES = ["2H Sword", "Scimitar", "Sword", "Mallet", "Dagger"]  # Smithing's melee weapons

    def weapon_type(self) -> str | None:
        """Type of the normal weapon worn ("2H Sword"), found once in the bank and remembered.
        A weapon from elsewhere (no normal tier worn) gives None: then the weapon is left alone."""
        # Check the remembered weapon is still the one worn: the user switched from a Copper 2H Sword
        # to an Iron Scimitar with a shield, and a week-old memory would have swapped it back.
        if self.weapon.get("type") and self.weapon.get("tier"):
            if not self.g._open_bank():
                return None
            info = self.g.gear_panel(f"{self.weapon['tier']} {self.weapon['type']}")
            self.g._clear_bank_search()
            if info and info["worn"]:
                return self.weapon["type"]
        attack = self.levels.get("Attack") or 1
        tiers = [t for t, lvl in self.ARMOUR_TIERS if lvl <= attack + 10 and t != "Stone"]
        for tier in reversed(tiers):
            for wtype in self.WEAPON_TYPES:
                if not self.g._open_bank():
                    return None
                info = self.g.gear_panel(f"{tier} {wtype}")
                self.g._clear_bank_search()
                if info and info["worn"]:
                    self.weapon = {"type": wtype, "tier": tier, "t": time.time()}
                    log.info("weapon: wearing %s %s", tier, wtype)
                    self.save_state()
                    return wtype
        self.weapon = {"type": None, "t": time.time()}
        self.save_state()
        return None

    def check_fireflies(self) -> None:
        """Keep a Fireflies in a Jar on the Toolbelt of each listed skill (a Utility slot; the jars are
        used up). A missing one is made: Molten Glass from Bucket of Sand (Smithing), a Glass Jar
        (Crafting, 2 glass), then Firefly + jar upgraded in the bank, and put on."""
        fcfg = self.cfg.get("fireflies", {})
        missing = []
        for skill in fcfg.get("skills", ["Farming", "Ranching", "Firemaking", "Combat"]):
            name = f"Fireflies in a Jar ({skill})"
            if not self.g._open_bank():
                return
            info = self.g.gear_panel(name)
            self.g._clear_bank_search()
            if info and info["worn"]:
                continue
            if info and info["stack"]:
                if info["can_equip"] and self.g.equip_gear(name):
                    self.jars_on[skill] = time.time()
                    self.event("gear", f"Put {name} on the Toolbelt")
                elif not info["can_equip"]:
                    self.jars_locked[skill] = time.time()  # e.g. Combat: the Toolbelt slot is still a Shop purchase
                continue
            # Not in the bank: on the Toolbelt (no placeholder, so the bank shows nothing), or used up.
            # A jar put on in the last day counts as worn; after that a new one is made.
            if time.time() - self.jars_on.get(skill, 0) < fcfg.get("assume_worn_hours", 24) * 3600:
                continue
            if time.time() - self.jars_locked.get(skill, 0) < 24 * 3600:
                continue  # its slot is locked: a jar would only lie in the bank
            missing.append(skill)
        if not missing:
            return
        self.task = "making Fireflies in a Jar"
        n = len(missing)
        self.g.make_count("Smithing", "Smelting", "Molten Glass", 2 * n, 2.5)
        jars = self.g.make_count("Crafting", "Materials & Gems", "Glass Jar", n, 3.0)
        made = []
        for skill in missing[:max(jars, 0) or n]:
            name = f"Fireflies in a Jar ({skill})"
            if self.g.upgrade_item("Firefly", name):
                made.append(skill)
                if self.g.equip_gear(name):
                    self.jars_on[skill] = time.time()
                    made[-1] += " (put on)"
        if made and not self.g.dry_run:
            self.event("gear", "Fireflies in a Jar made for " + ", ".join(made))

    def supply_vials(self) -> bool:
        """Make Vials for Herblore: buy Bucket of Sand when short (cheap), smelt Molten Glass, craft
        Pack of Vials. Returns True when packs were made."""
        hcfg = self.cfg.get("herblore", {})
        packs = hcfg.get("vial_packs", 10)
        glass = 2 * packs
        sand = self.known_count("Bucket of Sand")
        if sand is None or sand < glass:
            card = self.g.shop_item("Items & Materials", "Bucket of Sand")
            if card and card["enabled"]:
                price = max((need for _, need in card["costs"]), default=0)
                if (self.gold or 0) - price >= self.keep_gold():
                    self.g.hold(card["buy"], "buy Bucket of Sand", 4.0, action=True)
                    if not self.g.dry_run:
                        self.event("bought", f"Bucket of Sand x100 for {price:,} GP (for vials)", gp=price)
        self.task = "making vials for Herblore"
        self.g.make_count("Smithing", "Smelting", "Molten Glass", glass, 2.5)
        made = self.g.make_count("Crafting", "Materials & Gems", "Pack of Vials", packs, 3.0)
        if made and not self.g.dry_run:
            self.event("activity", f"Made {made} Pack of Vials for Herblore")
        return bool(made)

    def check_scarecrow(self) -> None:
        """Keep a scarecrow on the Farming Toolbelt against pests: equip one from the bank, or buy a
        stack of 100 in the Shop (Farming > Farming Items & Materials) when gold and logs allow."""
        name = self.cfg.get("farming", {}).get("scarecrow", "Normal Scarecrow")
        self.task = "checking the scarecrow"
        if not self.g._open_bank():
            return
        info = self.g.gear_panel(name)
        self.g._clear_bank_search()
        if info and info["worn"]:
            self.scarecrow_worn = True
            return
        self.scarecrow_worn = False
        if not (info and info["stack"]):
            card = self.g.shop_item("Farming", name)
            if not card:
                return
            gold_need = max((need for have, need in card["costs"] if need >= 1000), default=0)
            short = [(have, need) for have, need in card["costs"] if have < need]
            if short or not card["enabled"]:
                log.info("scarecrow: not affordable yet (%s)", ", ".join(f"{h}/{n}" for h, n in card["costs"]) or "costs unread")
                return
            if (self.gold or 0) - gold_need < self.keep_gold():
                log.info("scarecrow: would go below the gold to keep")
                return
            self.g.hold(card["buy"], f"buy {name}", 4.0, action=True)
            if self.g.dry_run:
                return
            self.event("bought", f"{name} x100 for {gold_need:,} GP", gp=gold_need)
            if not self.g._open_bank():
                return
            info = self.g.gear_panel(name)
            self.g._clear_bank_search()
        if info and info["stack"] and info["can_equip"] and self.g.equip_gear(name):
            self.event("gear", f"Put {name} on the Farming Toolbelt")

    def check_gear(self) -> None:
        """Skill gear (Toolbelt items with a bonus) lying in the bank goes into a free slot of its skill.

        A taken slot is left alone: which of two items is better is the user's call, so that case
        is only logged."""
        worn: dict[str, list[str]] = {}
        for e in self.g.toolbelt_items(equip=self.cfg.get("gear", {}).get("equip", True)):
            if e.get("stack") is not None:
                self.note_stock(e["name"], e["stack"])
            skill = e["skill"] or "?"
            for s in e["slots"]:
                if s["item"] and s["item"] not in worn.setdefault(skill, []):
                    worn[skill].append(s["item"])
            if e.get("topped_up"):
                log.info("gear: topped up %s for %s with %d", e["name"], skill, e["topped_up"])
                self.event("gear", f"{e['name']} topped up for {skill} (+{e['topped_up']})")
            elif e.get("equipped"):
                log.info("gear: equipped %s for %s", e["name"], skill)
                self.event("gear", f"{e['name']} equipped for {skill}" + (f" ({', '.join(e['bonuses'])})" if e["bonuses"] else ""))
                worn.setdefault(skill, []).append(e["name"])
            elif e["stack"] >= 1 and not any(s["state"] in ("free", "taken") and "potion" in e["name"].lower()
                                             for s in e["slots"]) and not any(s["state"] == "free" for s in e["slots"]):
                log.info("gear: %s is in the bank but %s has no free slot (worn: %s)", e["name"], skill,
                         ", ".join(worn.get(skill, [])) or "nothing read")
        self.gear = worn
        self.save_state()

    def keep_gold(self) -> int:
        """Gold that stays in the purse: nothing is bought that would leave less."""
        return max(0, int(self.cfg.get("shop", {}).get("keep_gold", 0)))

    def learn_prices(self, gold: int) -> None:
        """Prices never change, single reads do ("15,000" came back as "15"): remember every read
        per upgrade and use the value seen most often. Only the amounts owned come from this read."""
        for e in self.shop:
            if e.get("plot"):  # read on the Farming page, not from a shop card: only the gold owned changes
                e["costs"] = [(gold, e["costs"][0][1])]
                continue
            key = "|".join(str(e.get(k)) for k in ("category", "name", "skill", "level"))
            known = self.catalog.setdefault(key, {"gold": {}, "mats": {}})
            now = set()
            for have, need in e["costs"]:
                if need >= 5000 or (have and abs(have - gold) <= max(gold * 0.05, 100)):  # the gold cost
                    if need >= 1000:
                        known["gold"][str(need)] = known["gold"].get(str(need), 0) + 1
                else:
                    slot = known["mats"].setdefault(str(need), {"count": 0, "have": have})
                    slot["count"] += 1
                    slot["have"] = have
                    now.add(str(need))
            costs = []
            if known["gold"]:
                costs.append((gold, int(max(known["gold"], key=known["gold"].get))))
            costs += [(slot["have"], int(need)) for need, slot in known["mats"].items()
                      if slot["count"] >= 2 or need in now]
            e["costs"] = costs

    def shop_rank(self, e: dict) -> int:
        """Buying order: bank space, then Auto Eat while a monster out-hits the auto-eat, then the rest."""
        if e["category"] == "Bank Slots":
            return 0
        if e["name"].startswith("Auto Eat") and self.auto_eat_wanted():
            return 1
        if e.get("plot") in self.cfg.get("shop", {}).get("priority_plots", ["Trees"]):
            return 1  # a Tree plot: a big Farming xp boost, saved for
        return 2

    def read_shop(self, buy: bool = True, sell: bool = True) -> None:
        """Look at the Shop: the GUI shows which upgrades are in reach, and the bot buys those allowed.

        At most one round of selling per look (`sell`): after a sale and the purchase it paid for, the
        next upgrade waits for the next look instead of emptying the stock in one go."""
        scfg = self.cfg.get("shop", {})
        categories = list(scfg.get("categories", ["Bank Slots", "Firemaking", "Mining", "Woodcutting", "Fishing"]))
        if self.auto_eat_wanted() and "Combat Upgrades" not in categories:
            categories.append("Combat Upgrades")
        self.shop = self.g.survey_shop(categories)
        if "Combat Upgrades" not in scfg.get("categories", []):
            # Looked at only for Auto Eat: the other combat upgrades are not on the list.
            self.shop = [e for e in self.shop if e["category"] != "Combat Upgrades" or e["name"].startswith("Auto Eat")]
        for tab, price in self.plot_prices.items():
            # The next plot of a Farming tab is bought on the Farming page, but it is an upgrade like
            # the rest: shown in the list, saved for and bought when the gold is there. "plot" also
            # marks its one cost as gold, whatever the amount (a herb plot is 4,000).
            self.shop.append({"category": "Farming", "name": f"{tab.rstrip('s')} plot", "skill": None, "level": None,
                              "costs": [(self.gold or 0, price)], "plot": tab})
        if getattr(self, "land_price", None) and self.cfg.get("ranching", {}).get("buy_land", True):
            self.shop.append({"category": "Ranching", "name": "Ranching land", "skill": None, "level": None,
                              "costs": [(self.gold or 0, self.land_price)], "plot": "Ranching land"})
        for skill in {e["skill"] for e in self.shop if e.get("skill")}:
            level = self.g.skill_level(skill)
            if level is not None:
                self.levels[skill] = level
        # Every card shows the same gold, but single reads lose a digit ("3.44K" -> "44K"). Take the
        # value most cards agree on, helped by the (rounded) gold in the top bar, and use it everywhere.
        reads = [c[0] for e in self.shop for c in e["costs"] if c[1] >= 5000]
        top = self.g.read_gold()
        if reads:
            # The top bar is rounded but never loses a digit: a card read close to it is the exact value.
            close = [v for v in reads if top and abs(v - top) <= max(top * 0.05, 100)]
            gold = (max(set(close), key=close.count) if close else top if top is not None
                    else max(set(reads), key=reads.count))
            self.gold = gold
            self.learn_prices(gold)
        self.shop_read_at = time.time()
        ready = [e for e in self.shop if self.upgrade_status(e)[1]]
        # Short of gold only? Then sell some surplus for it: bank space first, then the cheapest.
        if buy and sell and self.cfg.get("sell", {}).get("enabled", True) and not ready and not self.g.dry_run:
            short = []
            for e in self.shop:
                kinds = [kind for kind, _ in self.upgrade_missing(e)]
                gold = [c for c in dict.fromkeys(tuple(c) for c in e["costs"])
                        if e.get("plot") or (c[1] >= 5000 and (self.gold is None or abs(c[0] - self.gold) <= max(self.gold * 0.05, 200)))]
                if gold and kinds == ["gold"]:  # gold is the only thing missing
                    short.append((self.shop_rank(e), gold[0][1] + self.keep_gold() - gold[0][0], e))
            if short:
                _, gap, entry = min(short, key=lambda s: (s[0], s[1]))
                if gap <= self.cfg.get("sell", {}).get("max_gap", 20000) and self.sell_for(gap, entry["name"]) >= gap:
                    self.read_shop(buy=True, sell=False)  # the gold is there now: buy it
                    return
        if ready:
            log.info("shop: in reach now: %s", ", ".join(e["name"] for e in ready))
        self.save_state()
        # Buy what is in reach: bank space first (a full bank loses items), then the cheapest upgrade.
        wanted = [e for e in ready if (scfg.get("buy_bank", True) if e["category"] == "Bank Slots"
                                       else scfg.get("buy_upgrades", True))
                  and time.time() - self.shop_failed.get(e["name"], 0) > 4 * 3600]
        # Saving: while a priority item (rank 1) is not affordable yet, buy nothing below it except
        # bank space, or the gold never piles up to 100K for a Tree plot.
        saving = [e for e in self.shop if self.shop_rank(e) == 1 and e not in ready]
        self.saving = bool(saving)
        if saving:
            wanted = [e for e in wanted if self.shop_rank(e) <= 1]
            if now_log := (time.time() - getattr(self, "saving_logged", 0) > 3600):
                self.saving_logged = time.time()
                log.info("shop: saving for %s", ", ".join(e["name"] for e in saving))
        if wanted and buy:
            wanted.sort(key=lambda e: (self.shop_rank(e), max((c[1] for c in e["costs"]), default=0)))
            pick = wanted[0]
            log.info("shop: buying %s (%s)", pick["name"], pick["category"])
            if pick.get("plot") == "Ranching land":
                bought = self.g.buy_ranch_land()
                if bought:
                    self.land_price = None  # the next visit reads the next one
            else:
                bought = self.g.buy_plot(pick["plot"]) if pick.get("plot") else self.g.buy_upgrade(pick)
            if pick.get("plot") and bought and not self.g.dry_run:
                self.plot_prices.pop(pick["plot"], None)  # the next look at the farm tells what is for sale then
            if not bought and not self.g.dry_run:
                self.shop_failed[pick["name"]] = time.time()  # leave it a few hours instead of every look
            if bought and not self.g.dry_run:
                self.bought.append(pick["name"])
                gold_costs = [c[1] for c in pick["costs"] if self.gold and abs(c[0] - self.gold) <= max(self.gold * 0.05, 200)]
                price = max(gold_costs, default=0)
                other = [c[1] for c in pick["costs"] if c[1] not in gold_costs]
                spent = f"{price:,} GP" if price else (f"{max(other):,} coins" if other else "its materials")
                self.event("bought", f"{pick['name']} ({pick['category']}) for {spent}", gp=price)
                # See what the gold allows next, a few in a row at most, without selling for it.
                self.read_shop(buy=len(self.bought) % 4 != 0, sell=False)

    # --- selling: only what the bot itself gathers and makes, and never the reserve ----------------

    def sell_candidates(self) -> list[tuple[str, int]]:
        """(item, pieces to keep) in the order to sell them: listed duplicates, cheap food, then
        logs, ores, bars.

        Loot (nests, secondary drops, rares) is never in here on its own: the bot only sells what it
        gathers and makes. The one exception is `sell.duplicates`, names the user listed because a
        second piece has no use; of those exactly one stays."""
        scfg = self.cfg.get("sell", {})
        out: list[tuple[str, int]] = [(name, 1) for name in scfg.get("duplicates", [])]
        # Food: the best ones stay to be eaten, and so does whatever is equipped right now.
        foods = sorted(((heals, name, stock) for name, (stock, heals) in self.food.items() if stock), reverse=True)
        best = {name for _, name, _ in foods[:scfg.get("keep_best_foods", 2)]}
        for heals, name, stock in sorted(foods):
            if name not in best and heals not in self.equipped_heals:
                out.append((name, scfg.get("keep_food", 100)))
        # Logs and ores of every card the grids showed as unlocked: what the bot cuts and mines.
        for tree in self.cards.get("Woodcutting", []) or self.known_targets.get("Woodcutting", []):
            if tree.endswith(" Tree"):
                out.append((tree[:-5] + " Log", scfg.get("keep_logs", 200)))
        for ore in self.cards.get("Mining", []) or self.known_targets.get("Mining", []):
            if ore.endswith(" Ore") or ore == "Coal":
                out.append((ore, scfg.get("keep_ores", 200)))
        for bar in self.smelt_counts:
            out.append((bar, scfg.get("keep_bars", 100)))
        # What a supply step gathers stays up to its goal: Normal Logs were cut to 800 (Fletching,
        # a scarecrow stack, Ranching repairs) and then sold back down to 250.
        goals = {"Coal": self.cfg.get("mining", {}).get("coal_goal", 400)}  # Coal feeds Steel and better bars
        for act in self.rotation:
            cond = act.get("only_while_stock_below")
            if cond:
                goals[cond[0]] = max(goals.get(cond[0], 0), int(cond[1]))
        out = [(name, max(keep, next((g for k, g in goals.items() if similar(k, name) >= 0.9), 0))) for name, keep in out]
        return list(dict.fromkeys(out))

    def sell_spare(self) -> int:
        """Sell what has no use any more, without waiting for a gold shortage: `sell.always` (Junk
        from Fishing) and cooked food below the best few (the best ones and the equipped ones stay)."""
        scfg = self.cfg.get("sell", {})
        got = 0
        names = list(scfg.get("always", []))
        if scfg.get("old_food", True) and self.food:
            foods = sorted(((heals, name, stock) for name, (stock, heals) in self.food.items() if stock), reverse=True)
            best = {name for _, name, _ in foods[:scfg.get("keep_best_foods", 2)]}
            names += [name for heals, name, stock in foods if name not in best and heals not in self.equipped_heals]
        for name in names:
            known = self.stock(name, 86400)
            if known and known["count"] == 0:
                continue
            gp = self.g.sell_item(name, lambda have, unit: have)
            self.note_stock(name, self.g.last_stack or 0)
            if gp:
                self.event("sold", f"{self.g.last_sale[0]:,} {name} for {gp:,} GP (no use any more)", gp=gp)
                got += gp
        self.sold += got
        return got

    def sell_old_gear(self) -> None:
        """Normal gear of every kind (weapons, shields, armour) from a tier below the best one both
        Attack and Defence allow: Stone now, Copper once Iron can be worn everywhere."""
        level = min(self.levels.get("Attack") or 0, self.levels.get("Defence") or 0)
        allowed = [t for t, lvl in self.ARMOUR_TIERS if lvl <= level]
        if len(allowed) < 2:
            return
        kinds = set(self.ARMOUR_PIECES + self.WEAPON_TYPES + ["Shield", "Defender"])
        # What each wearable tier holds, by kind. A piece only goes when a better piece of the same
        # kind is there to replace it: being allowed to wear Steel sold the Iron Boots while no Steel
        # Boots existed.
        owned: dict[str, set[str]] = {}
        for tier in allowed:
            if not self.g._open_bank():
                return
            for title, panel in self.g._bank_matches(tier):
                words = title.split()
                while words and len(words[-1]) <= 2 and words[-1] != "2H":  # icon scraps: "Stone Mallet e"
                    words.pop()
                name = " ".join(words)
                if not (name.startswith(tier + " ") and name[len(tier) + 1:] in kinds):
                    continue
                # A sold piece stays as a placeholder with 0: it only counts when some are in the
                # bank ("Sell 3 for ...") or the piece is worn ("Will Replace: <itself>").
                stack = next((parse_amount(m.group(1)) for ln in panel
                              for m in [re.search(r"sell\s+([\d,.]+)\s+for", ln.text, re.I)] if m), 0) or 0
                if stack > 0 or self.g.worn_in_panel(panel, name):
                    owned.setdefault(tier, set()).add(name[len(tier) + 1:])
            self.g._clear_bank_search()
        worn = self.weapon if isinstance(getattr(self, "weapon", None), dict) else {}
        if worn.get("tier") and worn.get("type"):
            owned.setdefault(worn["tier"], set()).add(worn["type"])
        for i, tier in enumerate(allowed[:-1]):
            better = set().union(*(owned.get(t, set()) for t in allowed[i + 1:]))
            found = [f"{tier} {kind}" for kind in sorted(owned.get(tier, set()) & better)]
            for name in found:
                gp = self.g.sell_item(name, lambda have, unit: have)
                if gp:
                    self.event("sold", f"{self.g.last_sale[0]} {name} for {gp:,} GP (old gear)", gp=gp)

    def sell_duplicates(self) -> int:
        """Sell every spare piece of the items the user listed as "one is enough"; one always stays.

        This runs on its own, not only when gold is short: the list is the user's instruction."""
        got = 0
        for name in self.cfg.get("sell", {}).get("duplicates", []):
            known = self.stock(name, 86400)
            if known and known["count"] == 0:
                continue  # none in the bank; an item notice sets the count again when one drops
            # One piece is enough: the worn one counts, so then the whole bank stack is spare.
            gp = self.g.sell_item(name, lambda have, unit: have - (0 if getattr(self.g, "last_worn", False) else 1))
            self.note_stock(name, self.g.last_stack or 0)
            if gp:
                pieces = getattr(self.g, "last_sale", (0, name, gp))[0]
                self.event("sold", f"{pieces:,} {name} for {gp:,} GP (duplicates)", gp=gp)
                got += gp
        if got:
            self.sold += got
        return got

    def sell_for(self, gap: int, what: str) -> int:
        """Sell surplus until about `gap` GP has come in. Returns the GP received."""
        scfg = self.cfg.get("sell", {})
        share = scfg.get("share", 0.5)
        dupes = set(scfg.get("duplicates", []))
        got = 0
        log.info("sell: %d GP short for %s", gap, what)
        self.task = f"selling surplus for {what}"
        if not self.equipped_heals and self.status and self.status.skill not in ("Thieving", "Combat"):
            # Know what is in the food slots before any food is sold.
            for page, read in ((lambda: self.g.open_skill("Thieving"), self.g.read_vitals),
                               (self.g.open_combat, self.g.combat_vitals)):
                if page():
                    heal = read().food_heal
                    if heal:
                        self.equipped_heals.add(heal)
        if not self.food:
            self.read_food()
        # First look what the surplus is worth. Selling a little without reaching the goal only
        # shrinks the stock, so nothing is sold unless the whole gap can be covered.
        plan, value = [], 0.0
        for name, keep in self.sell_candidates():
            if value >= gap:
                break
            # Counts of what the bot gathers come in with every item notice, and a price never
            # changes: a fresh entry in the stock list saves a trip to the bank per item.
            known = self.stock(name, 3600)
            if known and (known["price"] or known["count"] == 0):
                worth = (known["count"], known["price"] or 0.0)
            else:
                worth = self.g.appraise_item(name)
                self.note_stock(name, worth[0] if worth else 0, worth[1] if worth else None)
            if not worth or not worth[0]:
                continue
            have, unit = worth
            # Part of the surplus, never the reserve; of a listed duplicate everything but one.
            spare = have - keep if name in dupes else int((have - keep) * share)
            if spare > 0 and unit > 0:
                plan.append((name, keep, spare, unit))
                value += spare * unit
        log.info("sell: surplus worth %d GP: %s", value,
                 ", ".join(f"{n} {s} x {u:.0f}" for n, _, s, u in plan) or "nothing to spare")
        if value < gap:
            log.info("sell: not enough for %s yet - selling nothing", what)
            return 0
        for name, keep, spare, unit in plan:
            if got >= gap:
                break
            need = gap - got

            def amount(have: int, price: float, keep=keep, need=need, name=name) -> int:
                left = have - keep if name in dupes else int((have - keep) * share)
                return min(left, int(-(-need // max(price, 0.01)))) if left > 0 and price > 0 else 0

            gp = self.g.sell_item(name, amount)
            if self.g.last_stack is not None:
                self.note_stock(name, self.g.last_stack)
            if gp:
                pieces = getattr(self.g, "last_sale", (0, name, gp))[0]
                self.event("sold", f"{pieces:,} {name} for {gp:,} GP", gp=gp)
            got += gp
        if got:
            self.sold += got
            self.food_read_at = 0.0  # stocks changed: read them again before the next choice
        log.info("sell: %d GP received%s", got, "" if got >= gap else f", {gap - got} GP still short")
        return got

    def seconds_to_farm_check(self) -> float | None:
        """Time to the next look at the farm when no "!" shows up before it."""
        fcfg = self.cfg.get("farming", {})
        if not fcfg.get("enabled", True):
            return None
        return max(0.0, self.last_farm_check + fcfg.get("check_minutes", 60) * 60 - time.time())
    # --- rules ----------------------------------------------------------------------

    def check_smelting(self) -> bool:
        rules = self.cfg.get("smelting", {}).get("rules", [])
        if not rules:
            return False
        counts = self.g.read_smelting()
        if counts:
            self.smelt_counts = counts
        log.info("smelting counts: %s", ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in counts.items()) or "none read")
        for rule in rules:
            have = counts.get(rule["bar"])
            if not have or have[0] < rule["min_ore"]:
                continue
            # The same ore can make a better bar: Iron Ore makes Steel (with Coal) once Smithing
            # allows it. Try the highest unlocked bar first and fall back when it does not start
            # (its second ingredient, Coal, is not read).
            ore = self.ORE_FOR_BAR.get(rule["bar"], rule["bar"].replace(" Bar", " Ore"))
            rank = BAR_ORDER.index(rule["bar"]) if rule["bar"] in BAR_ORDER else len(BAR_ORDER)
            better = [bar for bar in counts if bar != rule["bar"] and bar in BAR_ORDER
                      and self.ORE_FOR_BAR.get(bar, bar.replace(" Bar", " Ore")) == ore
                      and BAR_ORDER.index(bar) > rank]
            for bar in sorted(better, key=BAR_ORDER.index, reverse=True) + [rule["bar"]]:
                log.info("rule: %s ore %d >= %d -> smelt %s", rule["bar"], have[0], rule["min_ore"], bar)
                if not self.g.smelt(bar):
                    continue
                if not self.g.dry_run:
                    time.sleep(2.5)
                    if self.g.status().skill != "Smithing":
                        log.info("smelting: %s did not start (another ingredient missing?)", bar)
                        continue
                self.smelting = True
                self.event("smelt", f"Smelting {bar} ({have[0]:,} {ore})")
                return True
        return False

    def real_mouse_ok(self) -> bool:
        """May the bot use the real mouse now? Only while the user is away and no media is playing."""
        rcfg = self.cfg.get("real_mouse", {})
        if not rcfg.get("enabled", True):
            return False
        if time.time() - getattr(self.g, "real_failed_at", 0) < 600:
            return False  # Windows refused the cursor a moment ago (locked screen): not for a while
        try:
            pid = desktop.window_pid(self.g.win.hwnd)
        except Exception:  # no window yet
            return False
        ok, why = desktop.may_use_real_mouse(pid, rcfg.get("idle_minutes", 10) * 60)
        key = "ok" if ok else why.split(" ")[0]
        if key != self.real_mouse_note:  # log changes only, not every look
            log.info("real mouse %s: %s", "allowed" if ok else "not now", why)
            self.real_mouse_note = key
        return ok

    PATCH_NOTES = pathlib.Path("logs") / "patchnotes"

    def check_game_update(self) -> None:
        """Steam updates the game in the background but only applies it on a restart, and the game
        says nothing. Note a waiting update in the journal, and keep the patch notes of every new
        build in logs/patchnotes/ so changes in the game's screens can be looked up."""
        info = steam.build_info()
        if not info:
            log.info("update check: Steam app manifest not found")
            return
        if info["pending"] and self.update_warned != info["target"]:
            self.update_warned = info["target"]
            self.event("warn", f"A game update is waiting (build {info['target']}). Steam installs it when "
                               "the game restarts: close and start the game when it suits you.")
        build = info["build"]
        notes_file = self.PATCH_NOTES / f"build-{build}.txt"
        if build == self.game_build and notes_file.exists():
            return
        old, self.game_build = self.game_build, build
        notes = steam.latest_update_notes()
        if notes:
            self.PATCH_NOTES.mkdir(parents=True, exist_ok=True)
            notes_file.write_text(f"{notes['title']}\n{notes['url']}\n\n{notes['text']}\n", encoding="utf-8")
        if old and old != build:
            title = notes["title"] if notes else f"build {build}"
            self.event("game", f"The game was updated: {title}. Patch notes in {notes_file.as_posix()}")
        self.save_state()

    def tick(self) -> None:
        now = time.time()
        ucfg = self.cfg.get("update_check", {})
        if ucfg.get("enabled", True) and now - self.update_checked >= ucfg.get("every_minutes", 60) * 60:
            self.update_checked = now
            try:
                self.check_game_update()
            except Exception as e:  # noqa: BLE001 - never let the check stop the bot
                log.warning("update check failed: %s", e)
        popup = self.g.dismiss_popup()
        if popup and popup != "Item Opened":
            self.event("game", f"Closed a pop-up: {popup[:120]}")
        st = self.g.status()
        if st.skill in PAGE_SKILLS and st.skill not in [a["skill"] for a in self.rotation]:
            # The status bar shows Farming while its page is open; look from the activity's own page.
            self.g.open_skill(self.current["skill"])
            st = self.g.status()
        self.status = st
        log.debug("status %s", st)
        if st.gp_hour is not None and st.skill == self.current["skill"]:
            self.gp_samples.append(st.gp_hour)
        if not self.picked_start:
            self.pick_start(st)
        if self.ctl.skip.is_set():
            self.ctl.skip.clear()
            if len(self.rotation) > 1:
                self.event("activity", f"Skipped the rest of {self.current['skill']} (your button)")
                self.next_activity("skipped by the user")
                return

        if st.bank_used is not None and st.bank_max and st.bank_used >= st.bank_max - self.cfg.get("bank_warn_free", 2):
            if now - self.last_bank_warn > 600:
                log.warning("bank almost full: %d/%d slots - new item types will not fit", st.bank_used, st.bank_max)
                self.event("warn", f"Bank almost full: {st.bank_used} of {st.bank_max} slots")
                self.last_bank_warn = now

        if self.g.stuck_count >= 6 and now - self.last_stuck_warn > 1800:
            self.event("warn", "The game does not react: no page opens. Something covers it; picture in logs/stuck.png")
            self.last_stuck_warn = now
        if now < self.quiet_until:
            return
        if not self.smelting and st.skill == "Smithing" and "Smithing" not in [a["skill"] for a in self.rotation]:
            # Smelting that runs when the bot starts (its own from before a restart, or the user's)
            # is left to finish, like the bot's own; then the rotation carries on.
            log.info("smelting %s is running: letting it finish", st.detail or "bars")
            self.smelting = True
        if self.smelting:
            if st.skill == "Smithing":
                return
            log.info("smelting finished")
            self.smelting = False
            self.try_start()
            return

        if st.skill and st.level:
            self.note_levels({st.skill: st.level})

        # The activity comes first, the errands (smelting, farm, bank, shop) after. In the other
        # order a restart spent one or two minutes on errands while the turn was already over,
        # and the window showed the next step while the game still did the last one.
        self.task = ""
        if self.duration and now - self.started_at >= self.duration:
            self.task = "switching to the next activity"
            self.next_activity(f"{self.duration / 60:.0f} min done")
            return

        skill = self.current["skill"]
        if len(self.rotation) > 1 and (not self.current.get("enabled", True)
                                       or (self.bank_full() and skill not in SAFE_WHEN_FULL)):
            self.next_activity("switched off" if not self.current.get("enabled", True)
                               else f"bank full: {skill} could bring new kinds of items")
            return
        if st.skill != skill:
            # One bad read of the status bar is not the end of an activity: look once more. Without
            # this a running tree was "started" again three times in a minute. In a fight the next
            # monster takes a few seconds to spawn and the status bar is empty meanwhile: wait it out
            # (the bot "restarted" a running Slayer fight every minute with a warning each time).
            for _ in range(4 if skill == "Combat" else 1):
                time.sleep(2.5 if skill == "Combat" else 1.0)
                st = self.status = self.g.status()
                if st.skill == skill:
                    break
        if st.skill != skill:
            log.info("not doing %s (now: %s) -> start", skill, st.skill or "nothing")
            if skill == "Combat" and self.last_hp is not None:
                # The bot did not stop this fight: the monster won, or the game ended it.
                self.event("warn", f"The fight with {self.target_name or 'the monster'} ended on its own (last HP read: {self.last_hp})")
                self.last_hp = None
            self.verify = False
            self.task = f"starting {skill}"
            self.try_start()
            return

        # A fight is never left alone: a trip to the farm, the bank or the smithy takes longer than
        # a strong monster needs. Those checks wait until the fight is over.
        fighting = self.current["skill"] == "Combat" and st.skill == "Combat"
        smelt_every = self.cfg.get("smelting", {}).get("check_minutes", 10) * 60
        if not fighting and now - self.last_smelt_check >= smelt_every:
            self.last_smelt_check = now
            self.task = "checking the ores for smelting"
            if self.check_smelting():
                return

        fcfg = self.cfg.get("farming", {})
        # The farm is tended when the sidebar shows the red "!" at Farming (something is ready),
        # and otherwise only now and then, for watering and the plot prices. The other errands
        # (bank, gear, ranch, shop) have their own timer and no longer ride on the farm check.
        farm_due = False
        if not fighting and fcfg.get("enabled", True):
            since = now - self.last_farm_check
            if since >= fcfg.get("check_minutes", 60) * 60 or now >= getattr(self, "farm_ready_at", float("inf")):
                farm_due = True
                self.farm_ready_at = float("inf")
            elif (since >= 60 and now - getattr(self, "badge_look", 0.0) >= 30
                  and now >= getattr(self, "badge_muted_until", 0.0)):
                self.badge_look = now  # one look costs about half a second: twice a minute is enough
                lit = self.g.badges()
                if lit.get("Farming"):
                    log.info("farming: the sidebar shows something is ready")
                    farm_due = True
                if lit.get("Ranching") and not fighting:
                    log.info("ranching: the sidebar shows the animals want something")
                    self.ranch_due = True
        fcfg = self.cfg.get("fishing", {})
        if (fcfg.get("use_bait") and self.current["skill"] == "Fishing" and st.skill == "Fishing"
                and now - getattr(self, "last_bait_check", 0.0) >= fcfg.get("bait_minutes", 15) * 60
                and self.real_mouse_ok()):
            # While the user is away: move to the biggest bait stack now and then, so every bait
            # gets used and an empty stack never stays selected.
            self.last_bait_check = now
            self.task = "choosing the bait"
            if self.g.open_skill("Fishing") and not self.g._choose_bait():
                self.event("warn", "Fishing: every bait stack is empty")
        errands_due = now - getattr(self, "last_errands", 0.0) >= self.cfg.get("errand_minutes", 10) * 60
        if not fighting and (farm_due or errands_due or getattr(self, "ranch_due", False)):
            self.last_errands = now
            if farm_due:
                self.last_farm_check = now
                self.task = "tending the farm"
                farm = self.g.tend_farm()
                self.plot_prices = dict(getattr(self.g, "plot_prices", {}))
                done = [f"{farm[k]} {k}" for k in ("harvested", "planted", "watered") if farm.get(k)]
                if farm.get("switched"):
                    done.append("now growing " + ", ".join(farm["switched"]))
                if done and not self.g.dry_run:
                    self.event("farm", ", ".join(done))
                ready_in = getattr(self.g, "next_ready", None)
                if ready_in is not None:
                    # Come back when the first plot is done (its timer), not on the next hourly round:
                    # potatoes stood ready while the badge was ignored after a visit seconds too early.
                    self.farm_ready_at = now + ready_in + 15
                    log.info("farming: next plot ready in %d min", ready_in // 60 + 1)
                if not done:
                    # The badge looked lit every minute once without work: ignore it until the next timer.
                    self.badge_muted_until = min(now + 10 * 60, getattr(self, "farm_ready_at", now + 600))
                    log.info("farming: nothing to do - the sidebar badge is ignored until the next plot is ready")
            self.note_levels(self.g.skill_levels())
            ocfg = self.cfg.get("open_items", {})
            if (ocfg.get("names") and self.current["skill"] != "Thieving" and not self.bank_full()
                    and now - self.last_open_items >= ocfg.get("every_minutes", 60) * 60):
                self.last_open_items = now
                self.task = "opening items in the bank"
                # The configured list, the containers every release knows, and containers the bot learned
                # from an item notice (item_catalog kind "container"): new ones open without a config edit.
                learned = [n for n, v in self.item_catalog.items() if v.get("kind") == "container"]
                names = list(dict.fromkeys([*ocfg["names"], *ALWAYS_OPEN, *learned]))
                for name in names:  # Bird Nest -> seeds, Bait Box -> bait
                    opened = self.g.open_items(name)
                    if opened and not self.g.dry_run:
                        got = ", ".join(f"+{n} {item}" for n, item in getattr(self.g, "last_opened", []))
                        self.event("open", f"Opened {opened} {name}" + (f": {got}" if got else ""))
                if self.cfg.get("sell", {}).get("enabled", True):
                    self.sell_duplicates()
                    self.sell_spare()
            if self.learn_queue and self.current["skill"] not in ("Thieving", "Combat"):
                self.learn_items()
            if self.price_queue and self.current["skill"] not in ("Thieving", "Combat"):
                # Sale prices for GP per minute: a few per round, read from the bank's Sell block.
                self.task = "looking up sale prices"
                for name in self.price_queue[:3]:
                    worth = self.g.appraise_item(name)
                    if worth:
                        self.note_stock(name, worth[0], worth[1])
                        log.info("price: %s %.0f GP each", name, worth[1])
                self.price_queue = self.price_queue[3:]
            acfg = self.cfg.get("armour", {})
            last = getattr(self, "armour_checked", None) or {}
            def tiers(stats: dict) -> tuple:  # tier reached per stat: a new one (10, 20 ...) triggers a check
                return tuple(sum(1 for _, lvl in self.ARMOUR_TIERS if lvl <= (stats.get(s) or 0))
                             for s in ("Defence", "Attack"))
            tier, old_tier = tiers(self.levels), tiers(last)
            # A check takes about three minutes of bank work: only when Defence reaches a new tier
            # (10, 20, 30 ...) or every few hours, not on every level.
            if (acfg.get("enabled", True) and self.current["skill"] not in ("Thieving", "Combat")
                    and (tier != old_tier or now - last.get("t", 0) >= acfg.get("every_hours", 6) * 3600)):
                self.check_armour()
            fcfg2 = self.cfg.get("farming", {})
            logs = self.known_count("Normal Log") or 0
            # Every 3 h, and as soon as the logs for a stack are there while none is worn: Fletching
            # and Firemaking use the logs up again within the hour.
            if (fcfg2.get("use_scarecrow", True) and self.current["skill"] not in ("Thieving", "Combat")
                    and (now - self.scarecrow_checked >= 3 * 3600
                         or (not self.scarecrow_worn and logs >= 500 and now - self.scarecrow_checked >= 600))):
                self.scarecrow_checked = now
                self.check_scarecrow()
            ffcfg = self.cfg.get("fireflies", {})
            if (ffcfg.get("enabled", True) and self.current["skill"] not in ("Thieving", "Combat")
                    and now - self.fireflies_checked >= ffcfg.get("every_hours", 4) * 3600):
                self.fireflies_checked = now
                self.check_fireflies()
            gcfg = self.cfg.get("gear", {})
            if (gcfg.get("enabled", True) and self.current["skill"] not in ("Thieving", "Combat")
                    and now - self.last_gear_check >= gcfg.get("every_minutes", 120) * 60):
                self.last_gear_check = now
                self.task = "checking skill gear in the bank"
                self.check_gear()
            if self.current["skill"] not in ("Thieving", "Combat"):  # those need the bot on their own page
                rcfg = self.cfg.get("ranching", {})
                if rcfg.get("enabled", True):
                    self.task = "tending the animals"
                    did = self.g.tend_ranch()
                    self.ranch_due = False
                    land = self.g.ranch_land()
                    self.land_price = land[0] if land else None
                    if did and not self.g.dry_run:
                        self.event("farm", "Ranching: " + ", ".join(did))
                    self.ranch = self.g.ranch_stats() or self.ranch
                    low = [f"{k} {v}%" for k, v in self.ranch.items() if k in ("hunger", "comfort") and v < rcfg.get("warn_below", 60)]
                    if low:
                        log.warning("ranching: %s - feed or tend the animals by hand", ", ".join(low))
                        if now - getattr(self, "ranch_warned", 0.0) > 3600:
                            self.ranch_warned = now
                            self.event("warn", f"Ranching: {', '.join(low)}. Feed or tend the animals by hand")
                if now - self.shop_read_at >= self.cfg.get("shop", {}).get("every_minutes", 120) * 60:
                    self.task = "looking at the Shop"
                    self.read_shop()
            self.g.open_skill(self.current["skill"])  # leave the Farming page (see PAGE_SKILLS)
            return

        skill = self.current["skill"]

        if skill == "Thieving" and self.tend_thief():
            return
        if skill == "Firemaking" and self.tend_fire():
            return
        if skill == "Combat" and self.tend_fight():
            return

        # Right skill, but is it the right card? Check at the start, on a level-up (new unlocks)
        # and every reselect_minutes as a safety net.
        leveled = bool(st.level and self.level and st.level > self.level)
        if st.level:
            self.level = st.level
        self.track_drop()
        if now - self.drops_since > 1800:  # an activity that stays for hours still reports now and then
            self.flush_drops(skill)
        reselect = self.cfg.get("reselect_minutes", 10) * 60
        goal = self.goal_level(self.current)
        if goal and skill in RECIPE_SKILLS and st.level and st.level >= goal and len(self.rotation) > 1:
            self.next_activity(f"{skill} reached level {goal}")
            return
        if skill in ("Cooking", "Firemaking", "Combat", *RECIPE_SKILLS):
            self.verify = False  # leave a running batch alone; the next one is picked when it ends
        elif self.verify or leveled or (skill != "Fishing" and now - self.last_reselect >= reselect):
            why = "start" if self.verify else f"level {st.level}" if leveled else "periodic"
            self.verify = False
            log.info("check %s target (%s)", skill, why)
            if skill == "Fishing":
                self.start_fishing(recheck=True)
                self.last_reselect = now
            else:
                self.try_start()

    def ensure_game(self) -> None:
        """No game window: start the game through Steam (Steam too, when it is closed). The game then
        opens on Character Selection, where dismiss_popup loads the one character."""
        gcfg = self.cfg.get("game_launch", {})
        if not gcfg.get("enabled", True):
            return
        now = time.time()
        if now - self.launched_at < gcfg.get("retry_minutes", 5) * 60:
            return  # Steam and the game take a minute or two to come up
        if steam.pids():
            return  # the game runs but has no window yet (loading)
        self.launched_at = now
        log.warning("game not running - starting it through Steam")
        self.event("warn", "The game was not running: started it through Steam")
        steam.launch_game()

    def restart_hung_game(self) -> None:
        """A game window that cannot be read for a long time is broken (after Windows ran out of
        memory it stayed unreadable for five hours): close it, kill it if it does not go, and let
        ensure_game start it again."""
        gcfg = self.cfg.get("game_launch", {})
        limit = gcfg.get("restart_hung_minutes", 20)
        now = time.time()
        if not self.capture_failed_since:
            self.capture_failed_since = now
        if not gcfg.get("enabled", True) or not limit or now - self.capture_failed_since < limit * 60:
            return
        minutes = round((now - self.capture_failed_since) / 60)
        log.warning("game window unreadable for %d min - restarting the game", minutes)
        self.event("warn", f"The game window could not be read for {minutes} min: restarted the game")
        self.g.win.close()
        for _ in range(12):
            if self.ctl.stop.wait(5) or not steam.pids():
                break
        steam.kill_game()
        self.capture_failed_since = 0.0
        self.launched_at = 0.0  # the next "not found" starts it at once

    def run(self, max_ticks: int = 0) -> None:
        done = 0
        threading.Thread(target=self.watch_front, daemon=True, name="watch-front").start()
        try:
            while not self.ctl.stop.is_set() and not (max_ticks and done >= max_ticks):
                if self.ctl.paused.is_set():
                    self.state = "paused"
                    self.ctl.stop.wait(0.5)
                    continue
                done += 1
                if self.state in ("starting", "paused"):
                    self.state = "running"
                try:
                    self.user_guard()
                    if self.state == "yielding":
                        log.info("you left the game alone - carrying on")
                        self.state = "running"  # the window shows it at once, not after the tick
                    if self.game_missing:
                        self.g.win.size  # raises GameWindowError while the window is still not there
                        # A window that just appeared is still loading: the first tick read an empty
                        # start screen as "nothing running" and switched the rotation before Load.
                        # So look every few seconds until Character Selection (press Load) or the game.
                        log.info("game window appeared - waiting for the start screen")
                        self.game_missing = False
                        deadline, seen = time.time() + 180, ""
                        while time.time() < deadline and not self.ctl.stop.is_set():
                            seen = self.g.start_screen()
                            if seen in ("game", "choose"):
                                break
                            self.ctl.stop.wait(2 if seen == "loading" else 4)
                        log.info("start screen: %s", seen)
                    self.tick()
                    self.task = ""
                    self.state, self.problem, self.problem_code, self.errors = "running", "", "", 0
                    self.capture_failed_since = 0.0
                except UserActive:
                    # The user is clicking in the game: drop what was being done and look again when
                    # they have left it alone for a while. Nothing is half-done that matters: every
                    # step starts from a fresh read of the screen.
                    if self.state != "yielding":
                        log.info("you are using the game - standing back")
                    self.state = "yielding"
                    self.emergency_eat()
                    self.ctl.stop.wait(0.3)
                    continue
                except GameWindowError as e:
                    self.state, self.problem, self.problem_code = "waiting", str(e), e.code
                    fighting = bool(self.status and self.status.skill == "Combat")
                    if e.code == "minimized" and (fighting or self.cfg.get("restore_window", True)):
                        # A minimized window cannot be read: the rotation stands still, and in a fight
                        # nobody eats. Put it back, behind the other windows and without the focus.
                        log.warning("game window minimized - restoring it behind the other windows")
                        if time.time() - getattr(self, "restored_at", 0.0) > 600:
                            self.event("warn", "The game window was minimized: put it back behind your other windows")
                        self.restored_at = time.time()
                        try:
                            self.g.win.restore()
                        except Exception:
                            log.exception("could not restore the game window")
                        self.ctl.stop.wait(1.5)
                        continue
                    log.warning("%s - retry in 10 s", e)
                    try:
                        if e.code == "not_found":
                            self.game_missing = True
                            self.ensure_game()
                        elif e.code == "capture_failed":
                            self.restart_hung_game()
                    except Exception:
                        log.exception("could not (re)start the game")
                    self.ctl.stop.wait(10)
                    continue
                except Exception as e:
                    self.errors += 1
                    self.problem, self.problem_code = f"{type(e).__name__}: {e}", ""
                    self.state = "error" if self.errors >= 3 else "running"
                    log.exception("tick failed (%d in a row)", self.errors)
                in_fight = self.current["skill"] == "Combat" and self.status and self.status.skill == "Combat"
                # During a fight look again almost at once: HP can drop a third between two slow looks.
                self.ctl.stop.wait(random.uniform(0.6, 1.2) if in_fight else
                                   self.cfg.get("tick_seconds", 5) * random.uniform(0.8, 1.3))
        finally:
            self.flush_drops(self.current["skill"])
            if self.status and self.status.skill in ("Thieving", "Combat"):
                log.warning("bot stopped while %s runs: nobody watches the HP or eats now", self.status.skill)
            self.state = "stopped"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--dry-run", action="store_true", help="read and navigate, but never start/stop actions")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--ticks", type=int, default=0, help="stop after N ticks (testing)")
    a = p.parse_args()

    use_root()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "bot.log", encoding="utf-8")],
    )
    cfg = load_config(pathlib.Path(a.config))
    ctl = Controls()
    Hotkeys(ctl).start()
    log.info("bot started%s - Ctrl+Alt+P pause, Ctrl+Alt+Q stop", " (DRY-RUN)" if a.dry_run else "")
    try:
        Bot(Game(GameWindow(), dry_run=a.dry_run), cfg, ctl).run(a.ticks)
    except KeyboardInterrupt:
        pass
    log.info("bot stopped")


if __name__ == "__main__":
    main()
