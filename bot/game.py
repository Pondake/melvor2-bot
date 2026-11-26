"""High-level game actions built on screenshots + OCR + background clicks.

All positions are found by reading text on screen, so layout changes and window size
matter less than with fixed coordinates. Regions are fractions of the client size.
"""
import difflib
import itertools
import logging
import pathlib
import random
import re
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import ocr
from .ocr import Line
from .window import GameWindow, ScaledWindow

log = logging.getLogger("bot")

SKILLS = [
    "Combat",  # what the status bar shows during a fight
    "Attack", "Strength", "Defence", "Hitpoints", "Ranged", "Magic", "Prayer", "Slayer",
    "Farming", "Ranching", "Woodcutting", "Firemaking", "Fishing", "Cooking", "Mining",
    "Smithing", "Fletching", "Crafting", "Runecrafting", "Hunting", "Beastcrafting",
    "Astrology", "Herblore", "Thieving", "Agility",
]

PAGE_SETTLE = 0.9  # seconds to wait after a click that changes the page
# Sidebar skill rows, top to bottom per group; their pitch gives the UI scale (41 px at scale 1.0).
SIDEBAR_PITCH = 41.0
SIDEBAR_GROUPS = (
    ("Attack", "Strength", "Defence", "Hitpoints", "Ranged", "Magic", "Prayer", "Slayer"),
    ("Farming", "Ranching"),
    ("Woodcutting", "Firemaking", "Fishing", "Cooking", "Mining", "Smithing", "Fletching", "Crafting",
     "Runecrafting", "Hunting", "Beastcrafting", "Astrology", "Herblore", "Thieving", "Agility"),
)
# The game scales its UI with the window width (more height only shows more content).
# Pixel offsets below were measured at this client width and are scaled from it.
REF_WIDTH = 2426
TUNED_SCALE = 0.866  # the UI scale every offset, text height and OCR setting was tuned and tested on
# Filter popup above card grids; its labels can sit where a card title is expected.
GRID_SCROLL = 3  # wheel notches per page while scanning a grid; small enough that rows overlap
GRID_UI_TEXTS = ("Filter by type", "Other options", "Show hidden")


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9/ ]", "", text.lower().replace("|", "l")).strip()


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def find(lines: list[Line], text: str, min_ratio: float = 0.8, where=None) -> Line | None:
    """Best line matching `text` (fuzzy, OCR tolerant)."""
    best, best_r = None, min_ratio
    for line in lines:
        if where and not where(line):
            continue
        r = similar(line.text, text)
        if r >= best_r:
            best, best_r = line, r
    return best


def find_vs(lines: list[Line], label: str, rival: str, min_ratio: float = 0.8, where=None) -> Line | None:
    """Like find, but the line must look more like `label` than like `rival`.

    "Start Fishing" vs "Stop Fishing" scores exactly 0.8, so plain find() confuses them.
    """
    return find(lines, label, min_ratio, where=lambda ln: (where is None or where(ln))
                and similar(ln.text, label) > similar(ln.text, rival))


def find_all(lines: list[Line], text: str, min_ratio: float = 0.8) -> list[Line]:
    return [ln for ln in lines if similar(ln.text, text) >= min_ratio]


def is_button(text: str, label: str) -> bool:
    """Fuzzy button match; short labels ("Cut") also match with a stray icon glyph in front."""
    if similar(text, label) >= 0.8:
        return True
    t, lab = norm(text), norm(label)
    return len(lab) <= 4 and t.split()[-1:] == [lab] and len(t) <= len(lab) + 4 and "full" not in t


@dataclass(frozen=True)
class GridSpec:
    """Layout of a skill page that is a grid of cards. Offsets in px at REF_WIDTH."""
    start: str                      # card button text when idle
    stop: str | None                # card button text when running (None: button never changes)
    title_dx: tuple[int, int]       # title.cx - button.cx
    title_dy: tuple[int, int]       # button.cy - title.cy
    title_min_h: int = 18
    panel_start: str | None = None  # side panel button that starts the selected card
    panel_stop: str | None = None   # side panel button while running
    stat: str | None = None         # label of a number on the card (e.g. "Current Knowledge")
    after: str | None = None        # button to press in the detail view the card opens (e.g. "Study")
    back: str | None = None         # button in that detail view that returns to the cards


GRIDS = {
    # Mining: title centred above the "Start Mining" button, which turns into "Stop" while running.
    "Mining": GridSpec("Start Mining", "Stop", (-140, 140), (250, 400), 13),
    # Woodcutting: "Cut" under the tree picture, title up and to the right; the running tree
    # shows in the side panel with "Stop Cutting".
    "Woodcutting": GridSpec("Cut", None, (60, 300), (100, 200), 12, "Start Cutting", "Stop Cutting"),
    # Astrology: constellation cards, name centred above "Select". Select opens the constellation,
    # where "Study" starts it; the running one shows in the status bar.
    "Astrology": GridSpec("Select", None, (-140, 140), (150, 300), 12, stat="Current Knowledge",
                          after="Study", back="Change Constellation"),
}


@dataclass
class FishSpot:
    location: str
    name: str
    xp: int            # xp per catch: higher = bigger fish (more xp, heals more when cooked)
    rares_done: bool   # all special items of this location already found


@dataclass
class Recipe:
    name: str
    category: str
    line: Line
    raw: int                    # ingredient pieces in the bank ("have" of have/need)
    heals: int | None = None    # HP per piece; None = not food
    stock: int | None = None    # cooked pieces in the bank
    need: int = 1               # ingredient pieces one make takes ("need" of have/need)
    below: bool = False         # card only shows after scrolling the recipe list down
    xp: int | None = None       # skill xp per make ("+3" top right of the card)


@dataclass
class Vitals:
    hp: int | None
    hp_max: int | None
    food_heal: int | None       # HP per piece of the equipped food
    food_count: int | None      # pieces of it left


@dataclass
class ThiefTarget:
    name: str
    success: float | None       # success chance in percent
    button: Line
    active: bool
    xp: int | None = None       # skill xp per success ("+13" under the portrait)
    seconds: float | None = None  # time per attempt ("3.00s")

    @property
    def rate(self) -> float:
        """Expected xp per second; unknown values count as poor."""
        return (self.xp or 0) * (self.success or 0) / 100 / (self.seconds or 3.0)


def is_action_button(text: str, label: str) -> bool:
    """A press-and-hold button: "Hold to Sell Items", or just "Sell Items" when the game's setting
    Disable Press and Hold is on (then a click does it). The short form must match almost exactly:
    "Sell Item" is the block header and "Watered" a plot label."""
    text = text.strip()
    if similar(text, f"Hold to {label}") >= 0.8:
        return True
    return similar(text, label) >= 0.97 and len(text) <= len(label) + 1


def parse_amount(text: str) -> int | None:
    """Amount as the game prints it: "1,234" is 1234 and "1.32K" is 1320."""
    m = re.fullmatch(r"\s*([\d,.]+)\s*([KkMm]?)\s*", text)
    if not m:
        return None
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    digits = m.group(1)
    try:
        return int(float(digits.replace(",", "")) * mult) if mult > 1 else int(re.sub(r"[,.]", "", digits))
    except ValueError:
        return None


def sentence_read(ink: np.ndarray) -> str | None:
    """Raw text of some glyphs, read inside a rendered sentence.

    `ink` is how strongly each pixel belongs to the glyphs: a 0..1 array, or a boolean mask. OCR
    skips a short lone number and reads "/ 45" as "1 45"; set between two words ("Count 45 left")
    the same glyphs come back right. Soft edges matter: a hard black-and-white mask of 9 px digits
    turned "40,000" into "4-0n", the same digits with their anti-aliasing read right every time.
    """
    alpha = ink.astype(float)
    ys, xs = np.where(alpha > 0.35)
    if not len(ys):
        return None
    alpha = alpha[max(ys.min() - 1, 0):ys.max() + 2, max(xs.min() - 1, 0):xs.max() + 2]
    paper, pen = np.array((205.0, 190.0, 160.0)), np.array((60.0, 50.0, 40.0))  # dark on light
    glyph = Image.fromarray((paper * (1 - alpha[..., None]) + pen * alpha[..., None]).astype(np.uint8))
    size = 44
    glyph = glyph.resize((max(1, glyph.width * size // glyph.height), size), Image.LANCZOS)
    font = ImageFont.truetype("segoeuib.ttf", int(size * 1.25))
    paper_rgb, pen_rgb = tuple(int(v) for v in paper), tuple(int(v) for v in pen)
    canvas = Image.new("RGB", (glyph.width + 420, size + 40), paper_rgb)
    draw = ImageDraw.Draw(canvas)
    draw.text((15, 8), "Count", fill=pen_rgb, font=font)
    gx = 20 + int(draw.textlength("Count ", font=font))
    canvas.paste(glyph, (gx, 8 + font.getbbox("0")[1]))
    draw.text((gx + glyph.width + 12, 8), "left", fill=pen_rgb, font=font)
    text = " ".join(ln.text for ln in ocr.read(np.array(canvas), None, 2))
    m = re.search(r"Count\s*(.+?)\s*left", text)
    return m.group(1) if m else None


def sentence_amount(ink: np.ndarray) -> int | None:
    """Amount ("45", "15,000", "4.13K") of some glyphs, read with `sentence_read`."""
    raw = sentence_read(ink)
    if raw is None:
        return None
    raw = re.sub(r"^[^\dOoQDIl|SsB]+", "", raw)  # an icon edge in front reads as ":" or a letter
    return parse_amount(raw.translate(str.maketrans("OoQDIl|SsB", "0000111558")).replace(" ", ""))


def cost_chips(strip: np.ndarray, min_w: int, min_h: int, dark: int = 40) -> list[tuple[int, int, int, int]]:
    """Boxes (x0, y0, x1, y1) of the near-black cost chips of a shop card, left to right."""
    black = strip.max(axis=2) < dark

    def runs(flags, least: int) -> list[tuple[int, int]]:
        out, start = [], None
        for i, on in enumerate(list(flags) + [False]):
            if on and start is None:
                start = i
            elif not on and start is not None:
                if i - start >= least:
                    out.append((start, i))
                start = None
        return out

    boxes = []
    for y0, y1 in runs(black.sum(axis=1) > min_w, min_h):
        # A chip column is dark just inside both edges; text and icon sit in the middle rows.
        edge = black[y0 + 2:y0 + 4].all(axis=0) & black[y1 - 4:y1 - 2].all(axis=0)
        boxes += [(x0, y0, x1, y1) for x0, x1 in runs(edge, min_w)]
    return sorted(boxes)


def have_need(box: np.ndarray) -> tuple[int | None, int | None]:
    """(have, need) of a cost text: a coloured "have", then a white "/ need" ("2.76K / 40,000").

    The two numbers differ in size and colour, so plain OCR splits them and loses the slash. Here
    each is cut out by its colour, with its soft edges, and read on its own. The first white glyph
    after the coloured part is the slash.
    """
    lo, hi = box.min(axis=2).astype(float), box.max(axis=2).astype(float)
    cols = np.where(((hi > 120) & (hi - lo > 70)).any(axis=0))[0]
    if not len(cols):
        return None, None
    coloured = np.clip((hi - 70) / 150, 0, 1) * (hi - lo > 45)
    white = (np.clip((lo - 70) / 150, 0, 1) * (hi - lo < 60))[:, cols[-1] + 1:]
    have = sentence_amount(coloured[:, cols[0]:cols[-1] + 1])
    used = np.where((white > 0.5).any(axis=0))[0]
    gaps = [b for a, b in zip(used, used[1:]) if b - a > 1]
    return have, (sentence_amount(white[:, gaps[0]:]) if gaps else None)


def white_need(box: np.ndarray) -> int | None:
    """The white "/ need" of a cost text; anything coloured in front (a coin, the amount owned) is skipped."""
    return have_need(box)[1]


def read_cost_chip(chip: np.ndarray) -> tuple[int | None, int | None]:
    """(have, need) of one cost chip of a shop card: icon, a coloured "have", then a white "/ need"."""
    return have_need(chip[:, int(chip.shape[0] * 0.95):])  # the icon is about as wide as the chip is high


@dataclass
class Status:
    skill: str | None  # skill of the running action, None when idle/unknown
    detail: str
    bank_used: int | None
    bank_max: int | None
    level: int | None = None  # level of that skill, from the status bar
    gp_hour: int | None = None  # the game's own GP per hour for the action, inputs already subtracted


@dataclass
class Card:
    name: str
    button: Line | None  # "Start ..." / "Stop ..." button
    active: bool         # this card's action is running
    locked: bool
    kind: str = ""       # type label above the title, e.g. "Tree" / "Bush" / "Ore Vein"
    stat: int | None = None  # GridSpec.stat value read from the card


class Game:
    def __init__(self, win: GameWindow, dry_run: bool = False):
        self.win = win if isinstance(win, ScaledWindow) else ScaledWindow(win)
        self.dry_run = dry_run
        self.stuck_count = 0  # skill pages that would not open in a row (something covers the game)
        self.real_ok = None  # callable: may the real mouse be used right now? (set by the bot)
        self.seedless_tabs: set[str] = set()  # Farming tabs whose plots lacked seeds on the last visit
        self.bar_extent: dict[str, tuple] = {}  # page -> (left, right, frame width, row) of the own HP bar
        self.img: np.ndarray | None = None
        self._scale = 0.0
        self._scale_size: tuple = ()
        self._scale_at = 0.0
        self._measuring = False
        self.cooking_locked: set[int] = set()  # levels of the locked recipes seen by the last survey
        self.firemaking_locked: set[int] = set()
        self.burn_interval = 4.0  # seconds per log, read from the Firemaking page
        self.last_cards: dict[str, list[tuple[str, str]]] = {}  # skill -> (card name, type) of the last scan
        self.guard = None        # called before every click; may raise to make the bot stand back
        self.last_stack: int | None = None  # pieces of the item sell_item last looked at (None: not there)

    # --- basics ------------------------------------------------------------------

    def snap(self) -> np.ndarray:
        self.img = self.win.capture()
        return self.img

    @property
    def ui(self) -> float:
        """UI scale of the frames the bot works on, relative to the reference size.

        The game's real scale is measured from the screen (the pitch of the skill rows in the
        sidebar): it follows neither the window width nor anything else the bot can ask for, and
        the user can change it. The window wrapper then resizes every frame to TUNED_SCALE, so
        this is that constant as soon as one measurement has worked.
        """
        size = tuple(self.win.raw.size)
        if self._scale and self._scale_size == size and time.time() - self._scale_at < 600:
            return self._scale
        # No sidebar (start screen, loading): every read would measure again, two OCR passes each.
        if not self._measuring and time.time() - getattr(self, "_scale_failed_at", 0.0) > 15:
            self._measuring = True
            try:
                measured = self._measure_scale()
            finally:
                self._measuring = False
            if measured:
                factor = TUNED_SCALE / measured
                if abs(factor - self.win.factor) > 0.01:
                    log.info("UI scale %.2f (window %dx%d): frames resized by %.2f", measured, *size, factor)
                    self.win.factor = factor
                    self.img = None  # the frame in hand has the old size
                self._scale, self._scale_size, self._scale_at = TUNED_SCALE, size, time.time()
                return self._scale
            self._scale_failed_at = time.time()
        return self._scale if self._scale and self._scale_size == size else size[0] / REF_WIDTH

    def _measure_scale(self) -> float | None:
        """The game's real UI scale: skill rows in the sidebar are SIDEBAR_PITCH px apart at scale 1.0."""
        img = self.win.raw.capture()  # the untouched frame: this is what decides the resize
        h, w = img.shape[:2]
        rows = {}
        for scale in (1.0, 1.5):  # plain ocr.read: self.read would ask for the scale again
            for ln in ocr.read(img, (0, int(h * 0.06), int(w * 0.11), int(h * 0.89)), scale):
                text = re.sub(r"^(?:\S{1,2}\s+)+|\s*\d+\s*/\s*\d+\s*$", "", ln.text)
                for group in SIDEBAR_GROUPS:
                    for i, name in enumerate(group):
                        if similar(text, name) >= 0.9:
                            rows.setdefault(name, []).append((group, i, ln.cy))
        pitches = []
        for group in SIDEBAR_GROUPS:
            seen = sorted((i, sum(v[2] for v in rows[name]) / len(rows[name]))
                          for i, name in enumerate(group) if name in rows)
            pitches += [(y2 - y1) / (i2 - i1) for (i1, y1), (i2, y2) in zip(seen, seen[1:]) if i2 - i1 <= 4]
        if len(pitches) < 4:
            return None
        pitches.sort()
        return round(pitches[len(pitches) // 2] / SIDEBAR_PITCH, 3)

    def px(self, value: float) -> int:
        return int(round(value * self.ui))

    def ocr_scale(self, scale: float = 1.0) -> float:
        """Upscale OCR input on small windows so text stays readable."""
        return scale * max(1.0, 1.0 / self.ui)

    def read(self, rx0=0.0, ry0=0.0, rx1=1.0, ry1=1.0, scale=1.0, fresh=True) -> list[Line]:
        img = self.snap() if fresh or self.img is None else self.img
        h, w = img.shape[:2]
        region = (int(rx0 * w), int(ry0 * h), int((rx1 - rx0) * w), int((ry1 - ry0) * h))
        return ocr.read(img, region, self.ocr_scale(scale))

    def read_union(self, rx0=0.0, ry0=0.0, rx1=1.0, ry1=1.0) -> list[Line]:
        """Thorough read for pages where a missed line matters (card grids).

        A single OCR pass over a big page drops lines at random (a whole row of cards once).
        Different scales and bands miss different lines, so take the union of a few passes.
        """
        img = self.snap()
        h, w = img.shape[:2]
        x, y, rw, rh = int(rx0 * w), int(ry0 * h), int((rx1 - rx0) * w), int((ry1 - ry0) * h)
        found: list[Line] = []
        for scale in (1.5, 2.0):
            found += ocr.read(img, (x, y, rw, rh), self.ocr_scale(scale))
        bands, overlap = 3, self.px(40)
        for i in range(bands):
            y0 = max(y + i * rh // bands - overlap, 0)
            y1 = min(y + (i + 1) * rh // bands + overlap, h)
            found += ocr.read(img, (x, y0, rw, y1 - y0), self.ocr_scale())
        merged: list[Line] = []
        for ln in found:
            same = next((m for m in merged if abs(m.cx - ln.cx) < self.px(30) and abs(m.cy - ln.cy) < self.px(10)), None)
            if same is None:
                merged.append(ln)
            elif len(re.findall(r"[A-Za-z0-9]", ln.text)) > len(re.findall(r"[A-Za-z0-9]", same.text)):
                merged[merged.index(same)] = ln  # the fuller read of the same line
        # The bands overlap past the region's edge: drop what lies outside (a bank counter above an
        # HP band once passed for the HP).
        return [ln for ln in merged if y <= ln.cy <= y + rh and x <= ln.cx <= x + rw]

    def click(self, line: Line, why: str, settle: float = PAGE_SETTLE, action: bool = False) -> None:
        """Click a line. `action` marks clicks that change what the character does (skipped in dry-run)."""
        if self.guard:
            self.guard()  # raises when the bot has to stand back (the user is playing)
        if action and self.dry_run:
            log.info("DRY-RUN would click %r (%s)", line.text, why)
            return
        # A different spot on the button every time: triangular around the centre, inside the text box.
        x = random.triangular(line.x + line.w * 0.12, line.x + line.w * 0.88)
        y = random.triangular(line.y + line.h * 0.2, line.y + line.h * 0.8)
        log.info("click %-28s (%s)", repr(line.text)[:28], why)
        self.win.click(x, y, settle=random.uniform(0.04, 0.11))
        time.sleep(random.uniform(0.05, 0.25))
        self.park_mouse()
        time.sleep(settle * random.uniform(1.0, 1.5))

    def hold(self, line: Line, why: str, seconds: float, action: bool = False) -> None:
        """Press and hold on a line ("Hold to Water", "Hold to Open")."""
        if self.guard:
            self.guard()
        if action and self.dry_run:
            log.info("DRY-RUN would hold %r (%s)", line.text, why)
            return
        x = random.triangular(line.x + line.w * 0.2, line.x + line.w * 0.8)
        y = random.triangular(line.y + line.h * 0.25, line.y + line.h * 0.75)
        log.info("hold  %-28s (%s)", repr(line.text)[:28], why)
        self.win.hold(x, y, seconds * random.uniform(1.0, 1.2))
        time.sleep(random.uniform(0.2, 0.4))
        self.park_mouse()
        time.sleep(PAGE_SETTLE)

    def park_mouse(self) -> None:
        """Move the virtual pointer to an empty spot so no tooltip covers the page."""
        w, h = self.win.size
        # Empty strip of the top bar between the logo and "Bank". Not further right: the Quests
        # button there shows a "Locked" tooltip that covers half the sidebar.
        self.win.move(int(w * random.uniform(0.10, 0.14)), int(h * random.uniform(0.012, 0.028)))

    # --- status ------------------------------------------------------------------

    def status(self) -> Status:
        img = self.snap()
        h, w = img.shape[:2]
        bar_h = self.px(55)
        bar = ocr.read(img, (0, h - bar_h, int(w * 0.40), bar_h), self.ocr_scale())
        # OCR often merges the whole bar into one noisy line ("10 minutes v Woodcutting Willow Tree 198"),
        # so pick the word that best matches a skill name, then take the words after it as detail.
        words = [w for w in re.split(r"[\s|]+", " ".join(ln.text for ln in bar)) if w]
        # The skill comes first and the detail after it: "Herblore | Fishing Potion P1-A". Take the first
        # word that reads as a skill, not the best match anywhere (a slightly misread "Herblore" lost
        # to the exact "Fishing" of the potion, and Herblore was restarted every minute).
        best_r, skill, at = 0.0, None, -1
        for i, word in enumerate(words):
            if len(word) < 4:
                continue
            r, name = max((similar(word, n), n) for n in SKILLS)
            if r >= 0.8:
                best_r, skill, at = r, name, i
                break
            if r > best_r:
                best_r, skill, at = r, name, i
        if best_r < 0.75:
            skill = None
        detail_words = []
        if skill:
            for word in words[at + 1:]:
                word = re.sub(r"^[^A-Za-z]+", "", word)
                if re.search(r"\d", word) or norm(word) in ("level", "left", "hr"):
                    break
                if len(norm(word)) <= 1:  # separator glyphs read as "I", "g", ...
                    if detail_words:
                        break
                    continue
                detail_words.append(word)
        detail = " ".join(detail_words)
        top = ocr.read(img, (int(w * 0.20), self.px(50), int(w * 0.10), self.px(30)), self.ocr_scale(2))
        used = total = None
        for ln in top:
            m = re.search(r"(\d+)\s*/\s*(\d+)", ln.text.replace(" ", ""))
            if m and int(m.group(1)) <= int(m.group(2)):  # "773 / 295" was a misread 223
                used, total = int(m.group(1)), int(m.group(2))
        if used is None:  # the small strip came back empty: read the whole stretch of the top bar
            for ln in self.read_union(0.15, 0.0, 0.50, 0.08):
                m = re.fullmatch(r"\D{0,3}(\d+)\s*/\s*(\d+)", ln.text.strip())
                if m and int(m.group(2)) >= 50 and int(m.group(1)) <= int(m.group(2)):
                    used, total = int(m.group(1)), int(m.group(2))
        # "Level 27 ... 36s left to Level 28": the lower number is the current level ("Leve127" = OCR).
        levels = [int(n) for n in re.findall(r"Leve[l1I]\s?(\d{1,3})", " ".join(ln.text for ln in bar))]
        levels = [n for n in levels if 1 <= n <= 120]
        return Status(skill, detail, used, total, min(levels) if levels else None,
                      self.gp_per_hour(img) if skill else None)

    RATE = re.compile(r"([+-])\s*(\d[\d ,]*)(?:[.,]\d\d)?\s*/\s*hr", re.I)

    def gp_per_hour(self, img) -> int | None:
        """The status bar's GP per hour ("[chest + coins] +3,600.00/hr"): the value of what the action
        makes minus what it uses up (Body Runes 5,400 - Rune Essence 1,800). Item rates follow it
        ("+1,800.00/hr", "-1,800.00/hr"); the GP one is the rate with gold coins just left of it."""
        h, w = img.shape[:2]
        bar_h = self.px(55)
        top = h - bar_h
        for ln in ocr.read(img, (int(w * 0.34), top, int(w * 0.30), bar_h), self.ocr_scale(1.3)):
            text = ln.text.replace("O", "0").replace("o", "0")
            for m in self.RATE.finditer(text):
                x = int(ln.x + ln.w * m.start() / max(len(text), 1))
                left = img[max(ln.y - 2, 0):min(ln.y + ln.h + 2, h), max(x - self.px(45), 0):x].astype(int)
                r, g, b = left[..., 0], left[..., 1], left[..., 2]
                # The coins are gold on most pages and light blue on some (Runecrafting).
                coins = (((r > 200) & (g > 150) & (b < 90) & (r - b > 120))
                         | ((b > 180) & (g > 120) & (r < 90) & (b - r > 120))).sum()
                if coins >= 12:
                    value = int(re.sub(r"[ ,]", "", m.group(2)))
                    return -value if m.group(1) == "-" else value
        return None

    def skill_levels(self) -> dict[str, int]:
        """Levels of every skill the sidebar shows, from one read ("Cooking  22/99")."""
        side = self.read_union(0.0, 0.06, 0.10, 0.98)
        out = {}
        for ln in side:
            words = re.sub(r"^(?:\S{1,2}\s+)+", "", ln.text)  # icon glyphs in front
            name = next((s for s in SKILLS if similar(re.sub(r"[\d/\s]+$", "", words), s) >= 0.88), None)
            if not name:
                continue
            m = re.search(r"(\d{1,3})\s*/\s*\d{2,3}\s*$", ln.text)
            if not m:
                near = [re.fullmatch(r"(\d{1,3})\s*/\s*\d{2,3}", o.text.strip()) for o in side
                        if abs(o.cy - ln.cy) < self.px(14) and o.cx > ln.cx]
                m = next((hit for hit in near if hit), None)
            if m:
                out[name] = int(m.group(1))
        return out

    def read_notices(self) -> list[tuple[int, str]]:
        """Item notices bottom right ("+3 Potato Seed"), newest read last, as (pieces, item)."""
        out = []
        for ln in sorted(self.read_union(0.80, 0.55, 1.0, 0.955), key=lambda ln: ln.cy):
            m = re.search(r"\+\s*([\d,]+)\s+([A-Za-z][A-Za-z' -]{2,})", ln.text)
            if m:
                out.append((int(m.group(1).replace(",", "")), m.group(2).strip(" -'")))
        return out

    def skill_level(self, skill: str) -> int | None:
        """Level of a skill from the sidebar ("Cooking  22/99")."""
        side = self.read(0.0, 0.06, 0.10, 0.95)
        name = find(side, skill, 0.85)
        if not name:
            return None
        for ln in side:
            m = re.fullmatch(r"(\d{1,3})\s*/\s*\d{2,3}", ln.text.strip())
            if m and abs(ln.cy - name.cy) < self.px(14) and ln.cx > name.cx:
                return int(m.group(1))
        m = re.search(r"(\d{1,3})\s*/\s*\d{2,3}\s*$", name.text)  # name and level read as one line
        if m:
            return int(m.group(1))
        # The small grey level is often skipped in the full-sidebar read: read just that spot, enlarged.
        w = self.img.shape[1]
        x0 = name.x + name.w + self.px(10)
        crop = np.ascontiguousarray(self.img[max(name.cy - self.px(14), 0):name.cy + self.px(14), x0:int(w * 0.10)])
        for scale in (3, 4, 2):
            text = " ".join(ln.text for ln in ocr.read(crop, None, self.ocr_scale(scale)))
            m = re.search(r"(\d{1,3})\s*/\s*\d{2,3}", text)
            if m:
                return int(m.group(1))
        return None

    def page_title(self) -> str:
        lines = self.read(0.38, 0.0, 0.46, 0.07)
        return " ".join(ln.text for ln in lines)

    # --- navigation --------------------------------------------------------------

    def on_page(self, name: str) -> bool:
        """Is this page open? Looks for its title in the top bar."""
        return (find(self.read(0.38, 0.0, 0.56, 0.07), name, 0.8) is not None
                or find(self.read_union(0.38, 0.0, 0.56, 0.07), name, 0.8) is not None)

    def open_skill(self, skill: str) -> bool:
        if skill == "Combat":  # not a row in the sidebar: every combat skill leads to its page
            return self.open_combat()
        if self.on_page(skill):
            self.stuck_count = 0
            return True
        w, h = self.win.size
        for attempt in range(3):
            # Sidebar lines come with icon glyphs and levels ("'Z Cooking", "Fishing 51/99"); a
            # single OCR pass also skips lines now and then, so read thoroughly.
            side = self.read_union(0.0, 0.06, 0.10, 0.95)
            line = find([Line(re.sub(r"^(?:\S{1,2}\s+)+|\s*\d+\s*/\s*\d+\s*$", "", ln.text), ln.x, ln.y, ln.w, ln.h)
                         for ln in side], skill, 0.85)
            if line:
                self.click(line, f"open {skill}")
                if self.on_page(skill):
                    self.stuck_count = 0
                    return True
            if self.dismiss_popup():  # a dialog over the page swallowed the click
                continue
            # Not visible: combat skills sit above, scroll the sidebar toward them first.
            self.win.scroll(int(w * 0.05), int(h * 0.5), 10 if attempt == 0 else -10)
            self.park_mouse()  # a pointer left on the sidebar opens tooltips over it
            time.sleep(0.4)
        log.warning("could not open skill page %s", skill)
        self.stuck_count += 1
        if self.stuck_count == 3:  # keep one picture of what blocks the page
            Image.fromarray(self.img).save(pathlib.Path("logs") / "stuck.png")
        # No Escape here: on most pages it opens the game menu (with "Save and Quit to Desktop").
        return False

    # --- card pages (Mining, Woodcutting-like grids) --------------------------------

    def _cards_on_screen(self, spec: GridSpec, running: str = "") -> list[Card]:
        """Cards with a start button on screen. `running` = activity named in the status bar."""
        lines = self.read_union(0.10, 0.07, 1.0, 0.95)
        w = self.img.shape[1]
        # Woodcutting and Mining have a side panel on the right; Astrology's five cards fill the width
        # (Iridan, the fifth, was taken for the panel and never chosen).
        edge = 1.0 if spec.stat else 0.70
        grid = [ln for ln in lines if ln.cx < w * edge]
        panel = [ln for ln in lines if ln.cx >= w * edge]
        panel_running = bool(spec.panel_stop and find_vs(panel, spec.panel_stop, spec.panel_start or "", 0.8))
        dx_lo, dx_hi = self.px(spec.title_dx[0]), self.px(spec.title_dx[1])
        dy_lo, dy_hi = self.px(spec.title_dy[0]), self.px(spec.title_dy[1])
        min_h = self.px(spec.title_min_h)
        cards = []
        for btn in grid:
            is_start = is_button(btn.text, spec.start)
            is_stop = bool(spec.stop) and is_button(btn.text, spec.stop) and similar(btn.text, spec.stop) > similar(btn.text, spec.start)
            if not (is_start or is_stop):
                continue
            cands = [ln for ln in grid if dx_lo < ln.cx - btn.cx < dx_hi and dy_lo < btn.cy - ln.cy < dy_hi
                     and ln.h >= min_h and len(re.findall(r"[A-Za-z]", ln.text)) >= 3
                     and not re.search(r"@|\d[.,]\d+\s*s\b|ISOs", ln.text)  # type label + clock "Ore Vein @ 1.50s"
                     and not any(similar(ln.text, ui) >= 0.7 or norm(ui) in norm(ln.text) for ui in GRID_UI_TEXTS)]
            if not cands:
                log.debug("card without readable title at %d,%d", btn.cx, btn.cy)
                continue
            # The title is the big text; the small type label ("Tree") can sit above it. Among the
            # tall candidates take the farthest one.
            tall = max(ln.h for ln in cands)
            title = max((ln for ln in cands if ln.h >= 0.8 * tall), key=lambda ln: btn.cy - ln.cy)
            name = self._clean_title(title)
            # Not a card: a second read of a button already taken, a garbled title ("I-Willow„Tree"),
            # or the filter bar ("Gold O All Ore Vein Es").
            if (any(abs(c.button.cx - btn.cx) < self.px(40) and abs(c.button.cy - btn.cy) < self.px(25) for c in cards)
                    or not re.fullmatch(r"[A-Za-z' ]{3,}", name) or len(name.split()) > 3
                    or re.search(r"\b(All|Filter|Type)\b|Vein \w+ Vein", name)):
                continue
            cards.append(Card(name, btn, is_stop, False, self._card_kind(grid, title),
                              self._card_stat(grid, btn, spec.stat) if spec.stat else None))

        # Grids whose card button never changes show the running action in the side panel or only
        # in the status bar. Mark the single best-matching card: "Oak Tree" and "Teak Tree" are
        # 0.82 alike, so "any card above the threshold" marks the wrong one.
        def mark_running(texts: list[str], min_ratio: float) -> None:
            ratio, card = max(((similar(t, c.name), c) for c in cards for t in texts),
                              key=lambda rc: rc[0], default=(0.0, None))
            if card is not None and ratio >= min_ratio:
                card.active = True

        if panel_running:
            mark_running([p.text for p in panel], 0.8)
        if running:
            mark_running([running], 0.75)
        # Reading order (rows top to bottom, left to right) = the game's unlock order.
        cards.sort(key=lambda c: c.button.cy)
        rows, row_y = [], None
        for c in cards:
            if row_y is None or c.button.cy - row_y > self.px(40):
                rows.append([])
                row_y = c.button.cy
            rows[-1].append(c)
        return [c for row in rows for c in sorted(row, key=lambda c: c.button.cx)]

    def _card_stat(self, lines: list[Line], btn: Line, label: str) -> int | None:
        """Number on the same row as `label` inside the card above the button ("Current Knowledge: 2,696")."""
        labels = [ln for ln in lines if similar(ln.text.rstrip(":"), label) >= 0.75
                  and abs(ln.cx - btn.cx) < self.px(220) and 0 < btn.cy - ln.cy < self.px(250)]
        if not labels:
            return None
        lab = min(labels, key=lambda ln: btn.cy - ln.cy)
        vals = [ln for ln in lines if abs(ln.cy - lab.cy) < self.px(15) and 0 < ln.cx - lab.cx < self.px(350)
                and re.fullmatch(r"[\d,.]+", ln.text.strip())]
        if not vals:
            # A lone "0" is never read by OCR: two new constellations had no Current Knowledge and were
            # left out of "least". Read the right end of the row on its own; nothing there means 0.
            x0 = lab.x + lab.w + self.px(20)
            n = self.soft_number(x0 + self.px(150), lab.cy - self.px(12), self.px(140), self.px(24), gap=8)
            if n is None:
                n = self.read_small_number(x0 + self.px(150), lab.cy - self.px(12), self.px(140), self.px(24), light=True)
            return n if n is not None else 0
        return int(re.sub(r"\D", "", min(vals, key=lambda ln: ln.cx - lab.cx).text) or 0)

    def _card_kind(self, lines: list[Line], title: Line) -> str:
        """Small type label just above the title ("Tree", "Bush", "Ore Vein @ 1.50s" -> "Ore Vein")."""
        above = [ln for ln in lines if 0 < title.cy - ln.cy < self.px(45) and abs(ln.x - title.x) < self.px(120)
                 and ln.h < title.h]
        if above:
            text = min(above, key=lambda ln: title.cy - ln.cy).text
        else:  # Mining puts it under the title: "Ore Vein @ 1.50s"
            below = [ln for ln in lines if 0 < ln.cy - title.cy < self.px(55) and abs(ln.cx - title.cx) < self.px(140)
                     and ln.h < title.h and re.search(r"[A-Za-z]{3,}", ln.text)]
            if not below:
                return ""
            text = min(below, key=lambda ln: ln.cy - title.cy).text
        return " ".join(re.findall(r"[A-Za-z]{3,}", text.split("@")[0])).title()

    def _clean_title(self, title: Line) -> str:
        """Card titles are white text on a coloured banner; a cleaned crop reads them better."""
        # OCR boxes often clip the first/last letters, so crop wider; dark-on-light (inverted max
        # channel) at 3x reads the white-on-banner text reliably.
        y0, y1 = max(title.y - self.px(8), 0), title.y + title.h + self.px(8)
        x0, x1 = max(title.x - self.px(18), 0), title.x + title.w + self.px(48)
        crop = self.img[y0:y1, x0:x1]
        inv = 255 - crop.max(axis=2)
        reads = [title.text,
                 " ".join(ln.text for ln in ocr.read(np.dstack([inv] * 3), None, self.ocr_scale(3))),
                 " ".join(ln.text for ln in ocr.read(np.ascontiguousarray(crop), None, self.ocr_scale(3)))]
        # Strip symbols, and an icon read as a capital stuck to the name ("LTeak Tree").
        reads = [re.sub(r"^[A-Z](?=[A-Z][a-z])", "", re.sub(r"^[^A-Za-z]+|[^A-Za-z)]+$", "", r.strip())) for r in reads]
        reads = [r for r in reads if len(re.findall(r"[A-Za-z]", r)) >= 3] or [title.text.strip()]
        # Three reads; keep the one the others agree with most (one misread letter loses the vote).
        return max(reads, key=lambda r: (round(sum(similar(r, o) for o in reads), 2), len(r)))

    def _scroll_grid(self, notches: int) -> None:
        w, h = self.win.size
        self.win.scroll(int(w * random.uniform(0.35, 0.45)), int(h * random.uniform(0.45, 0.65)), notches)
        time.sleep(random.uniform(0.6, 0.9))

    def scan_cards(self, spec: GridSpec, max_pages: int = 8, running: str = "") -> list[tuple[int, Card]]:
        """Scroll through a card grid top to bottom. Returns (page_index, card) pairs in order."""
        self._scroll_grid(30)
        seen, result, empty = set(), [], 0
        for page in range(max_pages):
            new = 0
            for card in self._cards_on_screen(spec, running):
                key = norm(card.name)
                if key not in seen:
                    seen.add(key)
                    result.append((page, card))
                    new += 1
            # Pages overlap, so one page without news is normal; two in a row is the bottom.
            empty = empty + 1 if new == 0 else 0
            if empty >= 2:
                break
            self._scroll_grid(-GRID_SCROLL)
        return result

    def start_card(self, skill: str, target: str = "best") -> str | None:
        """Start the chosen card and return its name.

        target: "best" = highest unlocked card (the grid is in unlock order, locked cards have no
        button), "best Tree" / "best Bush" = highest unlocked card of that type, or a card name.
        """
        spec = GRIDS[skill]
        st = self.status()
        running = st.detail if st.skill == skill else ""
        if not self.open_skill(skill):
            return None
        if spec.back:  # the page may show one card's detail view; the cards are behind this button
            back = find(self.read(0.10, 0.07, 0.70, 0.70), spec.back, 0.8)
            if back:
                self.click(back, f"{skill}: back to the overview")
        cards = self.scan_cards(spec, running=running)
        if not cards:
            log.warning("%s: no startable cards found", skill)
            return None
        kinds = {c.kind for _, c in cards if c.kind}
        for _, c in cards:  # unread type label: names like "Ambervine Bush" carry the type
            if not c.kind:
                c.kind = next((k for k in kinds if norm(c.name).endswith(norm(k))), "")
        log.debug("%s cards: %s", skill, [(c.name, c.kind, c.stat, c.active) for _, c in cards])
        self.last_cards[skill] = [(c.name, c.kind) for _, c in cards]
        words = target.split(maxsplit=1)
        if target.lower() == "random":
            page, card = random.choice(cards)
            active = next((pc for pc in cards if pc[1].active), None)
            if active:  # already doing a random pick; keep it until the next activity
                page, card = active
        elif target.lower() == "least":
            # Balance: the card with the lowest stat (e.g. Current Knowledge). Keep the running one
            # while it is close to the lowest, so it does not flip back and forth.
            known = [(p, c) for p, c in cards if c.stat is not None] or cards
            page, card = min(known, key=lambda pc: pc[1].stat or 0)
            active = next((pc for pc in known if pc[1].active), None)
            if active and (active[1].stat or 0) <= (card.stat or 0) * 1.15 + 100:
                page, card = active
        elif words and words[0].lower() == "best":
            kind = words[1] if len(words) > 1 else ""
            # Type labels are small and often misread; the type's first word usually sits in the
            # name too ("Gold Ore" for "Ore Vein", "Teak Tree" for "Tree").
            word = norm(kind).split()[0] if kind else ""
            pool = [(p, c) for p, c in cards
                    if not kind or similar(c.kind, kind) >= 0.8 or word in norm(c.name).split()]
            if not pool:
                log.warning("%s: no unlocked %r cards; using best of all", skill, kind)
                pool = cards
            page, card = pool[-1]
        else:
            match = sorted(((p, c) for p, c in cards if similar(c.name, target) >= 0.8),
                           key=lambda pc: similar(pc[1].name, target), reverse=True)
            if not match:
                log.warning("%s: target %r not unlocked/visible; using best", skill, target)
                match = [cards[-1]]
            page, card = match[0]
        if card.active and spec.back and self.status().skill != skill:
            running = ""  # leaving the detail view stopped it; start it again below
            card.active = False
        if card.active:
            log.info("%s: %s already running", skill, card.name)
            return card.name
        # Return to the page the card was on and click it fresh (positions moved while scanning).
        self._scroll_grid(30)
        for _ in range(page):
            self._scroll_grid(-GRID_SCROLL)
        on_screen = sorted(self._cards_on_screen(spec, running), key=lambda c: similar(c.name, card.name), reverse=True)
        for c in on_screen[:1]:  # the best match only, never the first that is "close enough"
            if similar(c.name, card.name) >= 0.8 and c.button:
                if c.active:
                    return c.name
                self.click(c.button, f"{skill}: {c.name}", action=True)
                if spec.panel_start and not self.dry_run:
                    self._confirm_in_panel(skill, spec, c.name)
                if spec.after and not self.dry_run:  # the card opened a detail view with its own start button
                    go = find(self.read(0.10, 0.30, 0.95, 0.95), spec.after, 0.85)
                    if go:
                        self.click(go, f"{skill}: {spec.after} {c.name}", action=True)
                    else:
                        log.warning("%s: %s selected, but no %r button found", skill, c.name, spec.after)
                return c.name
        log.warning("%s: lost card %s after scrolling", skill, card.name)
        return None

    def _confirm_in_panel(self, skill: str, spec: GridSpec, name: str) -> None:
        """If the card click only selected it, press the side panel's start button."""
        panel = self.read(0.70, 0.07, 1.0, 0.95)
        if spec.panel_stop and find_vs(panel, spec.panel_stop, spec.panel_start or "", 0.8) and find(panel, name, 0.9):
            return
        btn = find_vs(panel, spec.panel_start, spec.panel_stop or "", 0.8)
        if btn:
            self.click(btn, f"{skill}: start {name} in side panel", action=True)
        else:
            log.warning("%s: %s clicked, but no running/start state found in the side panel", skill, name)

    # --- Fishing -----------------------------------------------------------------

    def start_fishing(self) -> bool:
        if not self.open_skill("Fishing"):
            return False
        lines = self.read(0.60, 0.40, 1.0, 0.95)
        if find_vs(lines, "Stop Fishing", "Start Fishing"):
            log.info("Fishing already running")
            return True
        btn = find_vs(lines, "Start Fishing", "Stop Fishing")
        if btn:
            self.click(btn, "start fishing (current spot)", action=True)
            return True
        log.warning("Fishing: no Start button found (close the location popup?)")
        return False

    def _location_popup(self, want_open: bool) -> list[Line]:
        """Open or close the "Select Location" popup; returns the top-area lines afterwards."""
        lines = self.read(0.10, 0.05, 1.0, 0.45)
        is_open = any("items located" in ln.text.lower() for ln in lines)  # location cards only
        if is_open and not want_open:  # it may still be closing after a location click
            time.sleep(0.8)
            lines = self.read(0.10, 0.05, 1.0, 0.45)
            is_open = any("items located" in ln.text.lower() for ln in lines)
        if is_open != want_open:
            hdr = find(lines, "Select Location", 0.8)
            if not hdr:
                return lines
            self.click(Line("location dropdown", hdr.x + hdr.w + self.px(40), hdr.y, self.px(200), hdr.h),
                       ("open" if want_open else "close") + " location popup")
            lines = self.read(0.10, 0.05, 1.0, 0.45)
        return lines

    def _location_cards(self, lines: list[Line]) -> list[tuple[Line, bool]]:
        """Unlocked location cards in the open popup: (name line, all rares found)."""
        locations = []
        for spots_line in [ln for ln in lines if "spots unlocked" in ln.text.lower()]:
            names = [ln for ln in lines if 0 < spots_line.cy - ln.cy < self.px(70) and abs(ln.cx - spots_line.cx) < self.px(150)
                     and ln.h >= self.px(14) and len(re.findall(r"[A-Za-z]", ln.text)) >= 4]
            rares = [ln for ln in lines if 0 < ln.cy - spots_line.cy < self.px(50) and abs(ln.cx - spots_line.cx) < self.px(150)
                     and "items" in ln.text.lower()]
            if not names:
                continue
            name = min(names, key=lambda ln: spots_line.cy - ln.cy)
            # Names sit on artwork and OCR boxes can hold half a name ("Rive"): re-read a crop
            # centred on the card, as wide as the card.
            box = Line(name.text, spots_line.cx - self.px(140), name.y, self.px(280), name.h)
            name = Line(self._clean_title(box), box.x, box.y, box.w, box.h)
            m = re.search(r"(\d+)\s*/\s*(\d+)", rares[0].text) if rares else None
            locations.append((name, bool(m and m.group(1) == m.group(2))))
        return locations

    def survey_fishing(self) -> list[FishSpot]:
        """All unlocked spots with xp per catch, from the hover tooltip of each location card."""
        if not self.open_skill("Fishing"):
            return []
        self._location_popup(True)
        locations = self._location_cards(self.read_union(0.10, 0.05, 1.0, 0.45))
        spots: list[FishSpot] = []
        for name, rares_done in locations:
            found: list[FishSpot] = []
            for attempt in range(2):  # the tooltip or its OCR sometimes comes up empty: try once more
                for dy in (60, 70, 80):  # a few moves so the game registers the hover
                    self.win.move(int(name.cx + random.uniform(-20, 20)), int(name.cy + self.px(dy)))
                    time.sleep(0.25)
                time.sleep(1.2 + attempt)
                tip = self.read_union(0.10, 0.05, 1.0, 0.90) if attempt else self.read(0.10, 0.05, 1.0, 0.90)
                tip = [ln for ln in tip if name.cy + self.px(120) < ln.cy < name.cy + self.px(900)
                       and -self.px(300) < ln.cx - name.cx < self.px(350)]
                titles = [ln for ln in tip if ln.h >= self.px(13) and re.fullmatch(r"[A-Za-z' ]{4,}", ln.text.strip())
                          and not re.search(r"locked|unlock|level|special|output|fishing", ln.text, re.I)]
                for xp_line in [ln for ln in tip if re.search(r"\+(\d+)", ln.text)]:
                    above = [t for t in titles if 0 < xp_line.cy - t.cy < self.px(60)]
                    if above:
                        spot = max(above, key=lambda t: t.cy)
                        xp = int(re.search(r"\+(\d+)", xp_line.text).group(1))
                        found.append(FishSpot(name.text.strip(), spot.text.strip(), xp, rares_done))
                self.park_mouse()
                time.sleep(0.4)
                if found:
                    break
            if not found:
                log.warning("fishing: no spots read for %s", name.text)
            spots += found
        # A spot can be read twice (two "+" lines); keep one per (location, spot).
        unique = {(s.location, norm(s.name)): s for s in spots}
        result = list(unique.values())
        log.info("fishing spots: %s", ", ".join(f"{s.name} +{s.xp}{' (rares done)' if s.rares_done else ''}" for s in result))
        return result

    @staticmethod
    def waiting_fish(fish: dict[str, dict[str, int | None]], cookable: set[str], limit: int) -> dict[str, int]:
        """Fish piled up in the bank that no unlocked recipe can cook yet: {name: pieces}."""
        piles: dict[str, int] = {}
        for spot_fish in fish.values():
            for name, have in spot_fish.items():
                if (have or 0) >= limit and not any(similar(name, c) >= 0.85 for c in cookable):
                    piles[name] = have
        return piles

    @staticmethod
    def choose_spot(spots: list[FishSpot], fcfg: dict, bait_needed: bool,
                    fish: dict[str, dict[str, int | None]] | None = None,
                    cookable: set[str] | None = None, heals: dict[str, int] | None = None) -> FishSpot | None:
        """Mostly the highest-xp spot; locations whose rares are all found count for less.

        With `fish` (per spot) and `cookable` (unlocked recipes) it also looks at Cooking: a fish
        that is piled up but cannot be cooked yet is not worth more fishing, and while such piles
        wait, spots with fish that can be cooked count for more - cooking them is what raises the
        Cooking level that unlocks the rest. A mixed spot counts the pile in part: Windy Bay
        (Mackerel and Cod) won on its Mackerel while it mostly added to 537 Cod nobody could cook.
        With `heals` (food -> HP, given while good food runs low) spots whose fish heals more win.
        """
        if not spots:
            return None
        if bait_needed and fcfg.get("bait_spot"):
            match = [s for s in spots if similar(s.name, fcfg["bait_spot"]) >= 0.8]
            if match:
                return match[0]
        done_weight = fcfg.get("rares_done_weight", 0.6)
        limit = fcfg.get("backlog_limit", 200)
        training = bool(fish and cookable is not None and Game.waiting_fish(fish, cookable, limit))

        def score(s: FishSpot) -> float:
            value = s.xp * (done_weight if s.rares_done else 1.0)
            here = fish.get(s.name) if fish else None
            if here and cookable is not None:
                can = [n for n in here if any(similar(n, c) >= 0.85 for c in cookable)]
                piled = [n for n in here if n not in can and (here[n] or 0) >= limit]
                if can and training:
                    value *= fcfg.get("cookable_boost", 2.0)
                if piled:
                    weight = fcfg.get("backlog_weight", 0.2)
                    value *= (len(here) - len(piled) + len(piled) * weight) / len(here)
                if heals and can:
                    best = max((h for n in can for f, h in heals.items() if similar(n, f) >= 0.85), default=0)
                    value *= 1 + best / fcfg.get("heal_scale", 150)
            return value

        ranked = sorted(spots, key=score, reverse=True)
        log.info("fishing scores: %s", ", ".join(f"{s.name} {score(s):.0f}" for s in ranked[:4]))
        # No variety while food is needed: a random pick sent the bot to Windy Bay (Cod) instead of Lobster.
        if len(ranked) > 1 and not heals and random.random() < fcfg.get("variety", 0.25):
            return random.choice(ranked[1:4])
        return ranked[0]

    def _current_spot_line(self) -> Line | None:
        """The spot dropdown (current spot name) in the right panel, right of "Select Spot:".

        Anchored on that label: the panel has two "Output" headers (fish and Special Items), and when
        OCR missed the first one the line above the second, "Special Items", was taken for the spot."""
        lines = self.read_union(0.66, 0.15, 1.0, 0.60)
        labels = [ln for ln in lines if norm(ln.text).startswith("select spot")]
        if labels:
            label = min(labels, key=lambda ln: ln.cy)
            rest = re.sub(r"(?i)^\s*select\s*spot\s*:?\s*", "", label.text).strip()
            if len(re.findall(r"[A-Za-z]", rest)) >= 4:  # label and value read as one line
                part = len(label.text) - len(rest)
                x = label.x + int(label.w * part / max(1, len(label.text)))
                return Line(rest, x, label.y, label.x + label.w - x, label.h)
            right = [ln for ln in lines if abs(ln.cy - label.cy) < self.px(12) and ln.x > label.x + label.w - self.px(5)
                     and len(re.findall(r"[A-Za-z]", ln.text)) >= 4]
            if right:
                return min(right, key=lambda ln: ln.x)
        outputs = [ln for ln in lines if similar(ln.text, "Output") >= 0.8]
        if not outputs:
            return None
        out = min(outputs, key=lambda ln: ln.cy)
        cands = [ln for ln in lines if self.px(15) < out.cy - ln.cy < self.px(90) and "%" not in ln.text
                 and "select spot" not in ln.text.lower() and len(re.findall(r"[A-Za-z]", ln.text)) >= 4]
        return max(cands, key=lambda ln: ln.cx) if cands else None

    def _select_spot(self, spot: FishSpot) -> bool:
        """Show a spot in the panel on the right (Fishing page open). Only changes the view."""
        self._location_popup(True)
        cards = self._location_cards(self.read_union(0.10, 0.05, 1.0, 0.45))
        loc = max(cards, key=lambda c: similar(c[0].text, spot.location))[0] if cards else None
        if loc and similar(loc.text, spot.location) < 0.8:
            loc = None
        if not loc:
            log.warning("Fishing: location %s not found", spot.location)
            return False
        self.click(loc, f"location {spot.location}")
        self._location_popup(False)
        cur = self._current_spot_line()
        if cur and similar(cur.text, spot.name) < 0.8:
            self.click(cur, "open spot list")
            items = [ln for ln in self.read(0.68, 0.15, 1.0, 0.90) if ln.cy > cur.cy + self.px(20)]
            target = find(items, spot.name, 0.8)
            if not target:
                log.warning("Fishing: spot %s not in the list", spot.name)
                self.click(cur, "close spot list")
                return False
            self.click(target, f"spot {spot.name}")
        return True

    def read_spot_fish(self) -> dict[str, int | None]:
        """Fish of the spot shown in the panel: {name without "Raw": pieces in the bank}."""
        lines = self.read_union(0.68, 0.15, 0.95, 0.62)
        haves = sorted((ln for ln in lines if similar(ln.text, "You Have") >= 0.8), key=lambda ln: ln.cy)
        if not haves:
            return {}
        hdr = haves[0]  # the Output table; "Special Items" below has its own header
        special = find(lines, "Special Items", 0.8)
        bottom = special.cy if special and special.cy > hdr.cy else hdr.cy + self.px(260)
        fish = {}
        for ln in lines:
            m = re.fullmatch(r"Raw ([A-Za-z' ]{3,})", ln.text.strip())
            if not m or not hdr.cy < ln.cy < bottom:
                continue
            nums = [n for n in lines if abs(n.cy - ln.cy) < self.px(12) and abs(n.cx - hdr.cx) < self.px(50)
                    and re.fullmatch(r"[\d,.]+\s*[KkMm]?", n.text.strip())]
            fish[m.group(1).strip()] = (parse_amount(nums[0].text) if nums else
                                        self.read_small_number(hdr.cx - self.px(40), ln.cy - self.px(15),
                                                               self.px(80), self.px(30), light=True))
        return fish

    def survey_fish(self, spots: list[FishSpot]) -> dict[str, dict[str, int | None]]:
        """Which fish every spot gives and how many of each are in the bank. View changes only."""
        if not self.open_skill("Fishing"):
            return {}
        table = {}
        for spot in spots:
            if self._select_spot(spot):
                table[spot.name] = self.read_spot_fish()
        log.info("fish per spot: %s", "; ".join(
            f"{name}: " + ", ".join(f"{f} {n if n is not None else '?'}" for f, n in fish.items())
            for name, fish in table.items()))
        return table

    def start_fishing_at(self, spot: FishSpot, fcfg: dict) -> tuple[bool, bool]:
        """Select location + spot, optionally bait, then start. Returns (started, bait_needed)."""
        if not self.open_skill("Fishing") or not self._select_spot(spot):
            return False, False
        bait_needed = False
        if fcfg.get("use_bait"):
            bait_needed = not self._choose_bait(tuple(fcfg.get("bait_avoid", ())))
        lines = self.read(0.60, 0.40, 1.0, 0.95)
        if find_vs(lines, "Stop Fishing", "Start Fishing"):
            log.info("Fishing: already fishing at %s", spot.name)
            return True, bait_needed
        btn = find_vs(lines, "Start Fishing", "Stop Fishing")
        if not btn:
            log.warning("Fishing: no Start button found")
            return False, bait_needed
        self.click(btn, f"start fishing at {spot.location} / {spot.name} (+{spot.xp} xp)", action=True)
        return True, bait_needed

    def _bait_state(self) -> tuple[Line | None, str, int | None, Line | None]:
        """(label, selected bait name, its count, dropdown line) from the fishing panel."""
        lines = self.read(0.60, 0.40, 1.0, 0.95)
        label = find(lines, "Select Bait", 0.75)
        if not label:
            return None, "", None, None
        # The dropdown under the label reads "None" or "<bait name> <count>"; re-read it enlarged,
        # the count is a small digit next to the arrow.
        x0, y0 = max(label.cx - self.px(180), 0), label.cy + self.px(14)
        crop = np.ascontiguousarray(self.img[y0:y0 + self.px(50), x0:x0 + self.px(360)])
        text = " ".join(ln.text for ln in ocr.read(crop, None, self.ocr_scale(3))).strip()
        name = re.sub(r"[\d\W]+$", "", text).strip()
        count = self.read_small_number(label.cx + self.px(42), label.cy + self.px(17), self.px(40), self.px(25))
        dropdown = Line("bait dropdown", int(label.cx - self.px(100)), int(label.cy + self.px(26)), self.px(120), self.px(20))
        return label, name, count, dropdown

    def soft_number(self, x: int, y: int, w: int, h: int, gap: int = 12) -> int | None:
        """A small white number at a known spot, read with its soft edges (see `sentence_read`).

        Only the first run of glyph columns counts: after a gap wider than `gap` px comes something
        else (the next icon, a panel edge).
        """
        crop = self.img[max(y, 0):y + h, max(x, 0):x + w]
        if crop.size == 0:
            return None
        lo, hi = crop.min(axis=2).astype(float), crop.max(axis=2).astype(float)
        white = np.clip((lo - 70) / 150, 0, 1) * (hi - lo < 60)
        cols = np.where((white > 0.5).any(axis=0))[0]
        if not len(cols):
            return None
        end = cols[0]
        for c in cols[1:]:
            if c - end > self.px(gap):
                break
            end = c
        return sentence_amount(white[:, cols[0]:end + 1])

    def read_small_number(self, x: int, y: int, w: int, h: int, light: bool = False, gap: int = 7) -> int | None:
        """Read a lone small number (OCR skips a single digit on its own).

        Cut the glyphs out, enlarge them and set them inside a rendered sentence; OCR reads
        "Count 0 left" where it ignored a bare "0". `light`: white digits on a dark background.
        """
        crop = self.img[max(y, 0):y + h, max(x, 0):x + w]
        if crop.size == 0:
            return None
        gray = crop.mean(axis=2)
        # Light text is near-white and unsaturated; that keeps coloured icon pixels out.
        ink = ((crop.min(axis=2) > 150) & (crop.max(axis=2).astype(int) - crop.min(axis=2) < 40)) if light else gray < gray.max() - 60
        if not ink.any():
            return None
        # Keep the first run of glyph columns only: after a gap wider than a digit spacing comes
        # something else (an icon highlight, a panel edge).
        cols = np.where(ink.any(axis=0))[0]
        end = cols[0]
        for c in cols[1:]:
            if c - end > self.px(gap):
                break
            end = c
        raw = sentence_read(ink[:, cols[0]:end + 1])
        if raw is None:
            return None
        digits = raw.translate(str.maketrans("OoQDIl|SsB", "0000111558")).replace(" ", "").replace(",", "")
        return int(digits) if digits.isdigit() else None

    def _bait_tiles(self, dropdown: Line) -> list[tuple[int, int, int | None, bool]]:
        """Bait list open: (x, y, pieces, selected) of every stack tile above the dropdown.

        Each tile carries a muted blue badge with its count; the selected tile has a green
        background. Names only show in a tooltip, which needs the real mouse, so stacks go by count.
        """
        img = self.snap()
        x0, y0 = max(dropdown.x - self.px(80), 0), max(dropdown.cy - self.px(150), 0)
        crop = img[y0:dropdown.cy - self.px(5), x0:dropdown.x + self.px(300)].astype(int)
        if crop.size == 0:
            return []
        r, g, b = crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]
        badge = (b - r > 50) & (b > 95) & (r < 80) & (g > r)
        cols = np.where(badge.sum(axis=0) >= self.px(4))[0]
        runs, start = [], None
        for i, c in enumerate(cols):
            if start is None:
                start = c
            if i + 1 == len(cols) or cols[i + 1] - c > self.px(3):
                runs.append((start, c))
                start = None
        tiles = []
        for xa, xb in runs:
            if not self.px(8) <= xb - xa <= self.px(45):
                continue
            ys = np.where(badge[:, xa:xb + 1].any(axis=1))[0]
            ya, yb = ys.min(), ys.max()
            pieces = self.soft_number(x0 + xa, y0 + ya, xb - xa + 1, yb - ya + 1, gap=6)
            cx, top = x0 + (xa + xb) // 2, y0 + ya
            bg = img[max(top - self.px(8), 0), max(cx - self.px(22), 0)].astype(int)  # tile corner, beside the icon
            tiles.append((int(cx), int(top - self.px(18)), pieces, bool(bg[1] > bg[0] + 25 and bg[1] > bg[2] + 25)))
        return tiles

    def _pick_bait(self, dropdown: Line) -> str | None:
        """Real mouse: open the bait list and select the biggest stack. Using the biggest stack each
        time spreads the use over all baits. Returns what happened, for the log."""
        with self.win.real_mouse() as m:
            m.click(dropdown.cx, dropdown.cy, settle=0.8)
            tiles = self._bait_tiles(dropdown)
            usable = [t for t in tiles if t[2]]
            if not usable:
                m.click(dropdown.cx, dropdown.cy, settle=0.4)
                return f"no stack with bait left ({len(tiles)} read)" if tiles else None
            best = max(usable, key=lambda t: t[2])
            if best[3]:
                m.click(dropdown.cx, dropdown.cy, settle=0.4)  # already the selected one: close the list
                return f"kept the biggest stack ({best[2]})"
            m.click(best[0], best[1], settle=0.8)
        return f"switched to the stack of {best[2]} (counts {', '.join(str(t[2]) for t in tiles)})"

    def _choose_bait(self, avoid: tuple[str, ...] = ()) -> bool:
        """Check the selected bait; with the real mouse allowed, switch to the biggest stack.
        Returns False when no bait is left (count 0 and nothing to switch to).

        The bait list closes on a background click without selecting and ignores posted keys,
        so choosing a stack needs the real mouse (only while the user is away).
        """
        label, name, count, dropdown = self._bait_state()
        if not label:
            # An open bait list covers the label: a click on the open water closes it.
            w, h = self.win.size
            self.win.click(w * 0.47, h * 0.62)
            time.sleep(0.5)
            label, name, count, dropdown = self._bait_state()
        if not label:
            return True  # no bait control on this spot
        if self.real_ok and not self.dry_run and self.real_ok():
            done = self._pick_bait(dropdown)
            if done:
                log.info("bait: %s", done)
                label, name, count, dropdown = self._bait_state()
                if label and count:
                    log.info("bait: %s (%s left)", name, count)
                    return True
        if not name or similar(name, "None") >= 0.8:
            log.warning("bait: none selected - pick one in the game, or the bot does it while you are away")
            return True
        if count == 0:
            log.warning("bait: %s has run out - the bot switches stacks only while you are away (real mouse)", name)
            return False
        log.info("bait: %s (%s left)", name, count if count is not None else "?")
        return True
    # --- Farming -------------------------------------------------------------------

    FARM_TABS = ("Allotments", "Herbs", "Trees", "Compost Bins")

    def badges(self, skills: tuple[str, ...] = ("Farming", "Ranching")) -> dict[str, bool | None]:
        """Which sidebar rows show the red dot (something wants attention)? None: row not visible.

        Reads the screen only, no clicks: cheap enough to look twice a minute."""
        img = self.snap()
        h, w = img.shape[:2]
        lines = ocr.read(img, (0, int(h * 0.06), int(w * 0.13), int(h * 0.92)), self.ocr_scale(1.5))
        out = {}
        for skill in skills:
            row = next((ln for ln in lines if re.search(rf"\b{skill}\b", ln.text)), None)
            if not row:
                out[skill] = None
                continue
            band = img[max(row.cy - self.px(22), 0):row.cy + self.px(22), row.x + row.w:int(w * 0.14)]
            r, g, b = (band[..., i].astype(int) for i in range(3)) if band.size else (None,) * 3
            out[skill] = bool(band.size) and int(((r > 190) & (g < 110) & (b < 120)).sum()) >= self.px(12)
        return out

    def farm_ready(self) -> bool | None:
        """Does the sidebar show the red dot at Farming (something to harvest or plant)?"""
        return self.badges(("Farming",))["Farming"]

    def tend_ranch(self) -> list[str]:
        """Ranching overview: press "Repair all" when the enclosures need maintenance (it costs a few
        Normal Logs and brings comfort back to 100%) and "Pet All" when animals want pets. Animals and
        land are never bought: that is the user's call. Returns what was done."""
        if not self.open_skill("Ranching"):
            return []
        done = []
        lines = self.read_union(0.78, 0.08, 1.0, 0.80)
        if not any(re.search(r"Repair all|Pet All|want", ln.text) for ln in lines):
            # The page reopens where it was left: inside one enclosure the overview with its Repair
            # all / Pet All buttons is out of sight, and "Return to Land" ignores posted clicks.
            back = find(self.read_union(0.08, 0.05, 0.40, 0.14), "Return to Land", 0.8)
            if back and self.real_ok and not self.dry_run and self.real_ok():
                try:
                    with self.win.real_mouse() as m:
                        m.click(back.cx, back.cy, settle=1.2)
                    done.append("back to the land overview")
                    lines = self.read_union(0.78, 0.08, 1.0, 0.80)
                except Exception as e:  # noqa: BLE001 - a failed real click only skips this round
                    log.warning("ranching: real mouse failed (%s)", e)
            elif back:
                log.info("ranching: inside an enclosure - the overview needs the real mouse (not now)")
        repair = find(lines, "Repair all", 0.8)
        if repair:
            self.click(repair, "ranching: repair all", action=True, settle=0.8)
            done.append("repaired the enclosures")
        want = next((re.search(r"([\dOo]+)\s*Animals? want", ln.text) for ln in lines if "want" in ln.text), None)
        pets = int(want.group(1).replace("O", "0").replace("o", "0")) if want else 0
        pet = find(lines, "Pet All", 0.8)
        if pets and pet:
            self.click(pet, "ranching: pet all", action=True, settle=0.8)
            done.append(f"petted {pets} animal{'s' if pets > 1 else ''}")
        return done

    def tend_farm(self) -> dict[str, int]:
        """Harvest, replant and water on every tab of the Farming page; plots without seeds are skipped.

        Each tab (Allotments, Herbs) has its own plots and its own next plot for sale; the prices
        end up in `plot_prices`. Plots keep their chosen product, so replanting is the plot's own
        "Plant" button.
        """
        done = {"harvested": 0, "planted": 0, "no_seeds": 0, "watered": 0}
        self.plot_prices: dict[str, int] = {}
        self.next_ready: float | None = None  # seconds until the first growing plot is done
        if not self.open_skill("Farming"):
            return done
        for n, tab in enumerate(self.FARM_TABS):
            head = find(self.read_union(0.08, 0.08, 0.85, 0.24), tab, 0.85)
            if head:
                self.click(head, f"farming tab {tab}", settle=0.6)
            elif n:
                continue  # no such tab (yet); the first one is what the page opens on anyway
            skipped_before = done["no_seeds"]
            self._tend_plots(done)
            if done["no_seeds"] > skipped_before:
                self.seedless_tabs.add(tab)  # plots here stood empty for want of seeds
            else:
                self.seedless_tabs.discard(tab)
            self._note_timers()
            price = self._plot_price()
            if price:
                self.plot_prices[tab] = price
        log.info("farming: %d harvested, %d planted, %d watered, %d skipped (no seeds)%s",
                 done["harvested"], done["planted"], done["watered"], done["no_seeds"],
                 "".join(f"; next plot in {tab} costs {price:,} GP" for tab, price in self.plot_prices.items()))
        return done

    # Product picker of a plot (frame pixels at the tuned scale): the "Select" button sits this far
    # right of the "Product:" label's left edge; the icon grid opens below it, 4 wide.
    PICKER_SELECT_DX = 150  # px() units (frame px / 0.866)
    PICKER_GRID = (-103, 89, 103, 104)  # first icon dx, dy from Select; column and row step

    def _plot_headers(self, lines: list[Line]) -> list[tuple[Line, str]]:
        """("Product:" line, chosen product) for every plot on screen; product "" when none is chosen."""
        out = []
        for head in sorted((ln for ln in lines if ln.text.startswith("Product")), key=lambda ln: ln.x):
            if any(abs(head.x - o.x) < self.px(30) and abs(head.cy - o.cy) < self.px(12) for o, _ in out):
                continue  # the same label from another OCR pass
            below = [ln for ln in lines if self.px(20) < ln.cy - head.cy < self.px(60)
                     and head.x <= ln.cx <= head.x + self.px(390)
                     and re.fullmatch(r"[A-Za-z' ]{3,}", ln.text.strip()) and ln.text.strip() != "Clear"]
            label = below[0].text.strip() if below else ""
            label = re.sub(r"\s*(Fertiliser|Compost|Clear)\b.*$", "", label).strip()
            out.append((head, label))
        return out

    def _crop_tooltip(self, x: float, y: float) -> dict | None:
        """Read the tooltip of the picker icon under the real mouse at x, y (frame pixels)."""
        w, h = self.win.size
        lines = self.read_union(max(x - self.px(50), 0) / w, max(y - self.px(70), 0) / h,
                                min(x + self.px(600), w) / w, min(y + self.px(340), h) / h)
        locked = next((re.search(r"Farming Level\s*([\dIlO]+)", ln.text) for ln in lines
                       if "Farming Level" in ln.text), None)
        if locked:
            return {"locked": int(locked.group(1).replace("I", "1").replace("l", "1").replace("O", "0"))}

        def numbers(row: Line) -> list[int]:
            cells = sorted((ln for ln in lines if abs(ln.cy - row.cy) < self.px(12) and ln.x > row.x + row.w
                            and re.fullmatch(r"[\d,.Oo]+", ln.text.strip())), key=lambda ln: ln.x)
            return [int(re.sub(r"\D", "", c.text.replace("O", "0").replace("o", "0")) or 0) for c in cells]

        seed = next((ln for ln in lines if re.search(r"\bSeeds?\b", ln.text) and "Have" not in ln.text), None)
        heads = [ln for ln in lines if re.fullmatch(r"[A-Za-z' ]{3,}", ln.text.strip())
                 and not re.search(r"Input|Output|Qty|Have|Seed|options", ln.text)]
        if not seed or not heads:
            return None
        # The title sits right above "Input"; the plot's own label ("Potato Fertiliser") is higher up.
        near = [ln for ln in heads if seed.cy - self.px(110) < ln.cy < seed.cy]
        if not near:
            return None
        name = min(near, key=lambda ln: ln.cy).text.strip()
        out_row = next((ln for ln in heads if ln.cy > seed.cy and similar(ln.text.strip(), name) >= 0.85), None)
        need_have = numbers(seed)
        stock = numbers(out_row) if out_row else []
        return {"name": name, "seed": seed.text.strip(),
                "need": need_have[0] if need_have else None, "have": need_have[-1] if len(need_have) > 1 else None,
                "stock": stock[-1] if len(stock) > 1 else None}

    def choose_crop(self, head: Line, current: str) -> str | None:
        """Real mouse: open the plot's product picker, read every unlocked crop and pick the one with
        the smallest stock among those with enough seeds. Returns the crop now chosen, or None.

        The crops keep a stockpile each: the plot moves on to the next crop whenever that one can be
        planted, and stays on what it has when nothing else can."""
        sx, sy = head.x + self.px(self.PICKER_SELECT_DX), head.cy
        dx0, dy0, step_x, step_y = (self.px(v) for v in self.PICKER_GRID)
        crops = []
        with self.win.real_mouse() as m:
            m.click(sx, sy, settle=0.8)
            done = False
            for row in range(3):  # three rows show without scrolling; crops unlock in this order
                for col in range(4):
                    x, y = sx + dx0 + col * step_x, sy + dy0 + row * step_y
                    m.hover(x, y, settle=0.6)
                    tip = self._crop_tooltip(x, y)
                    if not tip or "locked" in tip:
                        done = True
                        break
                    crops.append((tip, (x, y)))
                if done:
                    break
            ready = [(t, xy) for t, xy in crops if t["need"] and t["have"] is not None and t["have"] >= t["need"]]
            log.info("crops: %s", ", ".join(f"{t['name']} {t['have']}/{t['need']} seeds, {t['stock']} in bank"
                                            for t, _ in crops) or "none read")
            if not ready:
                m.click(sx, sy, settle=0.4)  # close the picker, keep what the plot has
                return None
            best, xy = min(ready, key=lambda c: c[0]["stock"] or 0)  # None = not in the bank = 0
            if similar(best["name"], current) >= 0.85:
                m.click(sx, sy, settle=0.4)
                return current
            m.click(*xy, settle=0.8)
        log.info("crops: plot now on %s (was %s)", best["name"], current or "nothing")
        return best["name"]

    def green_buttons(self, rx0=0.10, ry0=0.10, rx1=0.95, ry1=0.95) -> list[Line]:
        """Text of every bright green button in the region, each read on its own.

        White bold text on the bright green Plant button is missed by page-wide OCR at every scale
        (the plots stood empty for an hour); cut out on its own it reads fine."""
        import cv2
        img = self.img if self.img is not None else self.snap()
        h, w = img.shape[:2]
        x0, y0, x1, y1 = int(rx0 * w), int(ry0 * h), int(rx1 * w), int(ry1 * h)
        part = img[y0:y1, x0:x1].astype(int)
        mask = ((part[:, :, 1] > 80) & (part[:, :, 1] > part[:, :, 0] + 50) & (part[:, :, 1] > part[:, :, 2] + 50)).astype(np.uint8)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        out = []
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            if bw < self.px(60) or bh < self.px(18) or bh > self.px(70) or area < bw * bh * 0.4:
                continue
            for scale in (1.0, 1.5):
                found = ocr.read(img, (x0 + bx, y0 + by, bw, bh), scale)
                if found:
                    text = " ".join(ln.text for ln in found).strip()
                    out.append(Line(text, x0 + bx, y0 + by, bw, bh))
                    break
        return out

    def _farm_lines(self) -> list[Line]:
        """The Farming page, read thoroughly, green buttons included (see `green_buttons`)."""
        lines = self.read_union(0.10, 0.10, 0.95, 0.95)
        return lines + self.green_buttons()

    # Compost picker (posted input works here, unlike the product picker): Select sits this far right
    # of the plot's "Product:" label; the tiles open below, first one Fertiliser.
    COMPOST_SELECT_DX = 379  # px() units (frame px / 0.866)
    COMPOST_GRID = (51, 166, 102, 102)  # first tile dx from "Product:", dy; column and row step

    def choose_compost(self, head: Line) -> int | None:
        """Pick a compost the bank has for the plot under `head` ("Product:" label), trying the tiles
        in order (Fertiliser first). Returns the tile index chosen, or None when none is in the bank."""
        sx, sy = head.x + self.px(self.COMPOST_SELECT_DX), head.cy
        dx0, dy0, step_x, step_y = (self.px(v) for v in self.COMPOST_GRID)
        for k in range(5):
            self.win.click(sx, sy)  # open the picker
            time.sleep(0.7)
            x, y = head.x + dx0 + (k % 4) * step_x, head.cy + dy0 + (k // 4) * step_y
            self.click(Line(f"compost tile {k + 1}", int(x - self.px(15)), int(y - self.px(15)), self.px(30), self.px(30)),
                       f"choose compost {k + 1}", settle=0.8)
            lines = self._farm_lines()
            short = any(head.x <= ln.cx <= head.x + self.px(390) and head.cy < ln.cy < head.cy + self.px(260)
                        and re.search(r"no compost selected|not enough", ln.text, re.I) for ln in lines)
            if not short:
                log.info("farming: compost tile %d chosen", k + 1)
                return k
        log.info("farming: no compost in the bank")
        return None

    def _tend_plots(self, done: dict[str, int]) -> None:
        """One Farming tab: harvest, plant and water what is on screen, counting into `done`."""
        # One page-wide OCR pass drops lines at random (it missed the "Plant" button for hours),
        # so every look at the plots is a merged read.
        for _ in range(8):  # one harvest per pass; positions change after each click
            lines = self._farm_lines()
            btn = find(lines, "Harvest", 0.8) or find(lines, "Collect", 0.85)
            if not btn:
                break
            self.click(btn, "harvest plot", action=True)
            done["harvested"] += 1
            if self.dry_run:
                break
        skipped = 0
        picked: set = set()  # plots whose crop was chosen this visit
        composted: set = set()  # Apply Compost buttons pressed this visit
        compost_tried: set = set()  # plots where a compost was looked for this visit
        for _ in range(8):  # one plot per pass: planting redraws the plot
            lines = self._farm_lines()
            plants = [ln for ln in lines if similar(ln.text.strip(), "Plant") >= 0.85
                      or similar(ln.text.strip(), "Begin Compost") >= 0.85]

            def blocked(btn: Line) -> bool:
                # "Not enough Seeds" sits in the same box as the button; "Not enough Compost" is about
                # the optional compost and does not stop planting. A compost bin needs its input
                # ("Not enough ...") and a product ("No Product selected.").
                bin_ = similar(btn.text.strip(), "Begin Compost") >= 0.85
                pattern = r"not enough|no product" if bin_ else r"not enough\s+seed|no product"
                return any(abs(ln.cy - btn.cy) < self.px(110) and abs(ln.cx - btn.cx) < self.px(200)
                           and re.search(pattern, ln.text, re.I) for ln in lines)

            if self.real_ok and not self.dry_run and self.real_ok():
                # Before planting an empty plot, let the stock decide its crop (real mouse only).
                for head, current in self._plot_headers(lines):
                    if (head.x, head.cy) in picked:
                        continue
                    if any(head.x <= btn.cx <= head.x + self.px(390) and btn.cy > head.cy for btn in plants):
                        picked.add((head.x, head.cy))
                        try:
                            crop = self.choose_crop(head, current)
                        except Exception as e:  # the real mouse can fail (screen just locked): plant anyway
                            log.warning("crops: real mouse failed (%s) - planting what the plot has", e)
                            self.real_failed_at = time.time()
                            crop = None
                        if crop and crop != current:
                            done.setdefault("switched", []).append(crop)
                        lines = self._farm_lines()
                        plants = [ln for ln in lines if similar(ln.text.strip(), "Plant") >= 0.85
                                  or similar(ln.text.strip(), "Begin Compost") >= 0.85]
            todo = [btn for btn in plants if not blocked(btn)]
            skipped = len(plants) - len(todo)
            if not todo:
                break
            # Compost chosen for the plot (Fertiliser: +30% crop) is applied first; "Apply Compost"
            # sits above the Plant button. Without a compost chosen or in the bank it says so.
            btn = todo[0]
            apply = next((ln for ln in lines if similar(ln.text.strip(), "Apply Compost") >= 0.85
                          and abs(ln.cx - btn.cx) < self.px(150) and 0 < btn.cy - ln.cy < self.px(120)), None)
            if apply and (apply.cx, apply.cy) not in composted and (apply.cx, apply.cy) not in compost_tried and any(
                    abs(ln.cy - apply.cy) < self.px(45) and abs(ln.cx - apply.cx) < self.px(200)
                    and re.search(r"no compost selected|not enough", ln.text, re.I) for ln in lines):
                # No compost chosen, or the chosen one ran out: pick one the bank has.
                compost_tried.add((apply.cx, apply.cy))
                head = next((hd for hd, _ in self._plot_headers(lines)
                             if hd.x <= btn.cx <= hd.x + self.px(390) and hd.cy < btn.cy), None)
                if head and self.choose_compost(head) is not None:
                    continue
            if apply and (apply.cx, apply.cy) not in composted and not any(
                    abs(ln.cy - apply.cy) < self.px(45) and abs(ln.cx - apply.cx) < self.px(200)
                    and re.search(r"no compost selected|not enough", ln.text, re.I) for ln in lines):
                composted.add((apply.cx, apply.cy))
                self.click(apply, "apply compost", action=True)
                done["composted"] = done.get("composted", 0) + 1
                time.sleep(0.8)
                continue  # the plot redraws: look again before planting
            self.click(btn, "plant plot", action=True)
            done["planted"] += 1
            if self.dry_run:
                break
            time.sleep(1.0)
        done["no_seeds"] += skipped
        # A dry plot shows "pls water me" and a "Hold to Water" button: hold it until it is watered.
        for _ in range(8):
            btn = next((ln for ln in self._farm_lines() if is_action_button(ln.text, "Water")), None)
            if not btn:
                break
            self.hold(btn, "water plot", 2.5, action=True)
            done["watered"] += 1
            if self.dry_run:
                break

    def _note_timers(self) -> None:
        """Growing plots show their time left ("1h 22m 22s"): keep the shortest in `next_ready`."""
        for ln in self.read_union(0.10, 0.10, 0.95, 0.95):
            # OCR reads the timer loosely ("h0h 21m 5"): minutes are the anchor, hours and seconds optional.
            if "Level" in ln.text:
                continue
            mins = re.search(r"(\d+)\s*m\b", ln.text)
            if mins:
                hours = re.search(r"(\d+)\s*h", ln.text[:mins.start()])
                secs = (int(hours.group(1)) * 3600 if hours else 0) + int(mins.group(1)) * 60 + 30
            else:
                # The last minute shows seconds only ("42s"): missing it left ripe potatoes for 30 min.
                only = re.fullmatch(r"\D{0,3}(\d{1,2})\s*s", ln.text.strip())
                if not only:
                    continue
                secs = int(only.group(1)) + 5
            if 0 < secs < 48 * 3600 and (self.next_ready is None or secs < self.next_ready):
                self.next_ready = secs

    def _plot_price(self) -> int | None:
        """Farming page: gold price of the plot that can be bought ("Purchase to Unlock"), if any."""
        buy = find(self.read_union(0.10, 0.10, 0.95, 0.95), "Purchase to Unlock", 0.8)
        if not buy:
            return None
        # The price sits one line under it: coin, the gold owned in colour, then "/ 40,000" in white.
        box = self.img[buy.cy + self.px(26):buy.cy + self.px(60),
                       max(buy.x - self.px(25), 0):buy.x + buy.w + self.px(45)]
        price = white_need(np.ascontiguousarray(box)) if box.size else None
        return price if price and price >= 1000 else None

    def buy_plot(self, tab: str = "Allotments") -> bool:
        """Farming page, tab `tab`: hold "Hold to Buy Plot" on the plot that is for sale."""
        if not self.open_skill("Farming"):
            return False
        head = find(self.read_union(0.08, 0.08, 0.85, 0.24), tab, 0.85)
        if head:
            self.click(head, f"farming tab {tab}", settle=0.6)
        btn = next((ln for ln in self.read_union(0.10, 0.10, 0.95, 0.95) if is_action_button(ln.text, "Buy Plot")), None)
        if not btn:
            log.info("farming: no plot for sale")
            return False
        self.hold(btn, "buy the farming plot", 2.5, action=True)
        return True

    # --- Recipe pages (Fletching, Runecrafting, Crafting ...: same frame as Cooking) -----

    @staticmethod
    def _named(lines: list[Line]) -> list[Line]:
        """Lines with the icon scraps in front taken off ("0 Trout" -> "Trout"), same place."""
        return [Line(re.sub(r"^(?:\S{1,2}\s+)+", "", ln.text), ln.x, ln.y, ln.w, ln.h) for ln in lines]

    def recipe_categories(self) -> list[str]:
        """Categories in the left column that have something unlocked ("Arrows / Unlocked: 3 / 11")."""
        lines = self.read_union(0.10, 0.08, 0.335, 0.96)
        out = []
        for ln in sorted(lines, key=lambda ln: ln.cy):
            m = re.search(r"Unlocked\W*([\dOo]+)\s*/", ln.text)
            if not m or m.group(1).replace("O", "0").replace("o", "0") == "0":
                continue
            names = [n for n in lines if 0 < ln.cy - n.cy < self.px(110) and n.h >= self.px(11)
                     and "unlocked" not in n.text.lower() and re.search(r"[A-Za-z]{3,}", n.text)]
            if names:
                # A long name wraps over two lines ("Intermediate Wizard" / "Gear"): the first is enough to click.
                out.append(max(names, key=lambda n: (n.cy > ln.cy - self.px(45), -abs(ln.cy - n.cy - self.px(27)))).text.strip())
        return out

    def category_recipes(self, skill: str, category: str) -> list[Recipe]:
        """Unlocked recipes of one category (page open) with the first ingredient's have / need."""
        cat = find(self.read(0.10, 0.08, 0.335, 0.96), category, 0.85)
        if not cat:
            log.debug("%s: category %s not found", skill, category)
            return []
        self.click(cat, f"{skill} category {category}")
        recipes = self._recipes_on_screen(category)
        # Long categories (Copper Gear: the Platebody) go on below the fold: scroll once and add
        # what shows up, then scroll back so the first cards are where `create_recipe` looks.
        w, h = self.win.size
        self.win.scroll(int(w * 0.40), int(h * 0.60), -6)
        self.park_mouse()
        time.sleep(0.5)
        seen = {r.name for r in recipes}
        more = [r for r in self._recipes_on_screen(category) if r.name not in seen]
        self.win.scroll(int(w * 0.40), int(h * 0.60), 6)
        self.park_mouse()
        time.sleep(0.4)
        return recipes + [Recipe(r.name, r.category, r.line, r.raw, r.heals, r.stock, r.need, below=True, xp=r.xp)
                          for r in more]

    def _recipes_on_screen(self, category: str) -> list[Recipe]:
        lines = self.read_union(0.24, 0.12, 0.57, 0.95)
        recipes = []
        for name in sorted(lines, key=lambda ln: (round(ln.cy / self.px(40)), ln.cx)):
            text = name.text.strip()
            # "2H" is the one digit a recipe name holds ("Copper 2H Sword"); OCR may read it "ZH".
            text = re.sub(r"\b[2Z]H\b", "2H", text)
            plain = re.sub(r"\s+P[\dIl]+\s*-\s*[A-Z]$", "", text.replace("2H", "TwoH"))  # "Mining Potion P1-A" ("PI-A")
            if (name.h < self.px(11) or not re.fullmatch(r"[A-Za-z' ]{3,}", plain)
                    or len(text.split()[0]) < 2  # a section header read with its icon: "O Secondary Runes"
                    or re.search(r"level|choose|recipe|unlock", text, re.I)):
                continue
            count = self._count_below(lines, name)  # locked cards and section headers have none
            if count:
                # "111 +3": the bar icon reads as "111"/"III"; the nearest badge to the right is this card's.
                right = sorted((ln for ln in lines if abs(ln.cy - name.cy) < self.px(14)
                                and 0 < ln.cx - name.cx < self.px(300) and re.search(r"\+\s*\d+\s*$", ln.text)),
                               key=lambda ln: ln.cx - name.cx)
                badge = re.search(r"\+\s*(\d+)\s*$", right[0].text) if right else None
                recipes.append(Recipe(text, category, Line(text, name.x, name.y, name.w, name.h), count[0], need=count[1],
                                      xp=int(badge.group(1)) if badge else None))
        return recipes

    def survey_recipes(self, skill: str, categories: list[str] | None = None) -> list[Recipe]:
        """Every unlocked recipe of a skill, category by category, in reading order."""
        if not self.open_skill(skill):
            return []
        found = []
        names = categories or self.recipe_categories() or self.recipe_categories()  # second look if the first was empty
        for category in names:
            found += self.category_recipes(skill, category)
        log.info("%s recipes: %s", skill, ", ".join(f"{r.name} {r.raw}/{r.need}" for r in found) or "none read")
        return found

    def create_recipe(self, skill: str, recipe: Recipe) -> bool:
        """Select a recipe (its page open) and press Create."""
        cat = find(self.read(0.10, 0.08, 0.335, 0.96), recipe.category, 0.85)
        if cat:
            self.click(cat, f"{skill} category {recipe.category}")
        card = find(self._named(self.read_union(0.24, 0.12, 0.57, 0.95)), recipe.name, 0.85)
        # Not on screen: below the fold, or above it (the list keeps the scroll of the last survey,
        # so "Normal Bow" at the top of Bows was out of sight while the bot only scrolled down).
        w, h = self.win.size
        for notches in (-6, 12):
            if card:
                break
            self.win.scroll(int(w * 0.40), int(h * 0.60), notches)
            self.park_mouse()
            time.sleep(0.5)
            card = find(self._named(self.read_union(0.24, 0.12, 0.57, 0.95)), recipe.name, 0.85)
        if not card:
            log.warning("%s: recipe %s not found", skill, recipe.name)
            return False
        self.click(card, f"select {recipe.name}")
        create = find(self.read(0.57, 0.70, 0.80, 0.98), "Create", 0.8)
        if not create:
            log.warning("%s: Create button not found", skill)
            return False
        self.click(create, f"{skill}: make {recipe.name}", action=True)
        return True

    def make_count(self, skill: str, category: str, recipe_name: str, count: int, seconds_each: float) -> int:
        """Make about `count` of a recipe, then press Stop (Create keeps going until the input runs out).
        Returns how many were started for (0 when it did not start)."""
        if not self.open_skill(skill):
            return 0
        recipes = [r for r in self.category_recipes(skill, category) if similar(r.name, recipe_name) >= 0.85]
        if not recipes:
            log.info("%s: no recipe %s in %s", skill, recipe_name, category)
            return 0
        r = recipes[0]
        count = min(count, r.raw // max(r.need, 1))
        if count <= 0 or not self.create_recipe(skill, r):
            return 0
        self.last_inputs = self.recipe_inputs()
        if self.dry_run:
            return count
        time.sleep(count * seconds_each + 0.5)
        stop = find(self.read_union(0.57, 0.70, 1.0, 0.99), "Stop", 0.85)
        if stop:
            self.click(stop, f"stop {skill} after {count} {r.name}", action=True)
        time.sleep(0.5)
        self.last_inputs = self.recipe_inputs() or self.last_inputs  # what is left after making them
        log.info("%s: made about %d %s", skill, count, r.name)
        return count

    def recipe_inputs(self) -> dict[str, int]:
        """Selected recipe panel: {input item: pieces in the bank} from the Input table."""
        lines = self.read_union(0.57, 0.15, 0.80, 0.50)
        head = find(lines, "Output", 0.9)
        out = {}
        for ln in lines:
            name = ln.text.strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z' ]{2,}", name) or name in ("Input", "Qty", "You Have", "Output"):
                continue
            if head and ln.cy >= head.cy:
                continue
            nums = sorted((o for o in lines if abs(o.cy - ln.cy) < self.px(12) and o.x > ln.x + ln.w
                           and parse_amount(o.text.strip()) is not None), key=lambda o: o.x)
            if len(nums) >= 2:
                out[name] = parse_amount(nums[-1].text.strip())
        return out

    def agility_courses(self) -> list[dict]:
        """Agility page: every course card ("Climb 1") with success %, seconds per run, skill xp and
        its Start Course button."""
        lines = self.read_union(0.25, 0.15, 0.97, 0.75)
        out = []
        for start in (ln for ln in lines if similar(ln.text.strip(), "Start Course") >= 0.85):
            card = [ln for ln in lines if abs(ln.cx - start.cx) < self.px(220) and 0 < start.cy - ln.cy < self.px(560)]
            title = next((ln for ln in sorted(card, key=lambda ln: ln.cy)
                          if re.fullmatch(r"[A-Za-z]+\s*\d+", ln.text.strip())), None)
            success = next((float(m.group(1)) for m in (re.search(r"(\d+(?:\.\d+)?)\s*%", ln.text) for ln in card) if m), None)
            secs = next((float(m.group(1)) for m in (re.search(r"(\d+[.,]\d+)\s*s\b", ln.text.replace(",", ".")) for ln in card)
                         if m), None)
            xp = next((int(m.group(1)) for m in (re.search(r"\+\s*(\d+)\s*Skill\s*XP", ln.text.replace(" ", " "), re.I)
                                                 for ln in card) if m), None)
            out.append({"name": title.text.strip() if title else "?", "success": success, "seconds": secs,
                        "xp": xp, "start": start})
        return out

    def start_agility(self, min_success: float = 100.0) -> str | None:
        """Start the safe course (success >= min_success) with the most skill xp per second."""
        if not self.open_skill("Agility"):
            return None
        # Courses are spread over locations (Climbing Grounds, Jumping Grounds, Arenas ...) and the
        # page shows only the one picked last: left on Arenas, the bot saw only Arena 1 (47%).
        # The location panel is the same widget as on the Thieving page.
        found = []
        here = self.thieving_location()
        locations = self.thieving_locations()  # opens the panel
        if locations:  # the first one by its button (the panel is open), the rest through the panel again
            self.click(locations[0][1], f"Agility location {locations[0][0]}", settle=1.2)
            here = locations[0][0]
        for i, loc in enumerate([n for n, _ in locations] or [here]):
            if i and not self.go_thieving_location(loc):
                continue
            here = loc
            courses = self.agility_courses()
            if not courses:  # the page can still be drawing after the switch
                time.sleep(0.8)
                courses = self.agility_courses()
            found += [(loc, c) for c in courses]
        log.info("agility: %s", ", ".join(f"{loc}: {c['name']} {c['success']}% {c['seconds']}s +{c['xp']}"
                                          for loc, c in found) or "no course read")
        safe = [(loc, c) for loc, c in found if c["success"] is not None and c["success"] >= min_success]
        if not safe:
            return None
        loc, best = max(safe, key=lambda lc: (lc[1]["xp"] or 1) / (lc[1]["seconds"] or 5))
        if loc != here:
            if not self.go_thieving_location(loc):
                return None
            again = [c for c in self.agility_courses() if similar(c["name"], best["name"]) >= 0.9]
            if not again:
                return None
            best = again[0]
        self.click(best["start"], f"Agility: start {best['name']} ({loc})", action=True)
        return best["name"]

    # --- Hunting -------------------------------------------------------------------

    def hunting_animals(self) -> list[tuple[str, Line]]:
        """Hunting page: (animal, its Select button) of every unlocked animal in the open location."""
        lines = self.read_union(0.28, 0.12, 0.52, 0.95)
        out = []
        for btn in (ln for ln in lines if ln.text.strip() == "Select"):
            names = [ln for ln in lines if 0 < btn.cy - ln.cy < self.px(110) and abs(ln.cx - btn.cx) < self.px(260)
                     and ln.h >= self.px(14) and re.fullmatch(r"[A-Za-z' ]{4,}", ln.text.strip())
                     and not re.search(r"Trap|Tool|Select", ln.text)]
            if names:
                out.append((min(names, key=lambda ln: ln.cy).text.strip(), btn))
        return out

    def badge_tiles(self, x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int, int | None]]:
        """(x, y, count) of item tiles in a frame box, found by the muted blue count badge under each
        (bait list, trap tiles). Tiles are centred and their number varies, so positions are not fixed."""
        img = self.snap()
        crop = img[max(y0, 0):y1, max(x0, 0):x1].astype(int)
        if crop.size == 0:
            return []
        r, g, b = crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]
        badge = (b - r > 40) & (b > 95) & (r < 95) & (g > r)
        cols = np.where(badge.sum(axis=0) >= self.px(4))[0]
        runs, start = [], None
        for i, c in enumerate(cols):
            start = c if start is None else start
            if i + 1 == len(cols) or cols[i + 1] - c > self.px(3):
                runs.append((start, c))
                start = None
        out = []
        for xa, xb in runs:
            if not self.px(8) <= xb - xa <= self.px(50):
                continue
            ys = np.where(badge[:, xa:xb + 1].any(axis=1))[0]
            ya, yb = int(ys.min()), int(ys.max())
            n = self.soft_number(max(x0, 0) + xa, max(y0, 0) + ya, xb - xa + 1, yb - ya + 1, gap=6)
            out.append((int(max(x0, 0) + (xa + xb) // 2), int(max(y0, 0) + ya - self.px(20)), n))
        return out

    def hunting_locations(self) -> list[tuple[str, Line]]:
        """Hunting page, left column: (location, its name line) of every location with an animal unlocked."""
        lines = self.read_union(0.10, 0.12, 0.30, 0.90)
        out = []
        for ln in lines:
            m = re.search(r"Unlocked\W*([\dOo]+)\s*/", ln.text)
            if not m or m.group(1).replace("O", "0").replace("o", "0") == "0":
                continue
            names = [n for n in lines if 0 < ln.cy - n.cy < self.px(40) and abs(n.x - ln.x) < self.px(40)
                     and re.fullmatch(r"[A-Za-z' ]{3,}", n.text.strip())]
            if names:
                name = max(names, key=lambda n: n.cy)
                out.append((name.text.strip(), name))
        return out

    def go_hunting_location(self, name: str) -> bool:
        for loc, line in self.hunting_locations():
            if similar(loc, name) >= 0.85:
                self.click(line, f"Hunting location {loc}", settle=0.8)
                return True
        log.info("hunting: location %s not found", name)
        return False

    def hunting_animal_xp(self) -> dict[str, float]:
        """XP per catch of each animal card in the open location ("+54-162": the middle, 108)."""
        lines = self.read_union(0.28, 0.12, 0.52, 0.95)
        out = {}
        for name, btn in self.hunting_animals():
            row = next((ln for ln in lines if abs(ln.cy - (btn.cy - self.px(150))) < self.px(60)
                        and re.search(r"\+\s*\d+\s*-\s*\d+", ln.text) and ln.cx > btn.cx), None)
            row = row or next((ln for ln in lines if re.search(r"\+\s*\d+\s*-\s*\d+", ln.text)
                               and 0 < btn.cy - ln.cy < self.px(170)), None)
            m = re.search(r"\+\s*(\d+)\s*-\s*(\d+)", row.text) if row else None
            out[name] = (int(m.group(1)) + int(m.group(2))) / 2 if m else 0.0
        return out

    def hunting_trap_tiles(self) -> list[tuple[int, int, int | None]]:
        """Right panel: (x, y, traps in the bank) of the trap tiles under "Select Trap"."""
        # "Select Trap | Snare | Created in Crafting" comes back as one OCR line.
        head = next((ln for ln in self.read_union(0.52, 0.30, 0.75, 0.50) if ln.text.startswith("Select Trap")), None)
        if not head:
            return []
        tiles = self.badge_tiles(head.x - self.px(60), head.cy + self.px(30), head.x + self.px(420), head.cy + self.px(110))
        rows = [y for _, y, n in tiles if n is not None]
        if not rows:
            return []
        row = max(set(rows), key=rows.count)  # a blue trap icon can pass for a badge: keep the badge row
        return [t for t in tiles if abs(t[1] - row) <= self.px(8)]

    def hunting_trap_type(self) -> str:
        """Trap type of the selected animal ("Select Trap | Small Bird Catcher | Created in Crafting")."""
        head = next((ln for ln in self.read_union(0.52, 0.30, 0.75, 0.50) if ln.text.startswith("Select Trap")), None)
        if not head:
            return ""
        m = re.match(r"Select Trap\W*(?:I\s+)?(.+?)\s*(?:[I|]\s*)?Created", head.text)
        return m.group(1).strip(" I|") if m else ""

    def start_hunting(self, animal: str = "best") -> str | None:
        """Hunt `animal` ("best" = the last one with a Select button). Returns the animal, or None.
        After a None, `no_traps` tells whether traps were the problem and `trap_type` which ones."""
        self.no_traps, self.trap_type = False, ""
        if not self.open_skill("Hunting"):
            return None
        animals = self.hunting_animals()
        if not animals:
            log.info("hunting: no animal to select")
            return None
        pick = animals[-1] if animal == "best" else next(((n, b) for n, b in animals if similar(n, animal) >= 0.8), None)
        if not pick:
            log.info("hunting: %s not found", animal)
            return None
        self.click(pick[1], f"Hunting: select {pick[0]}", settle=0.8)
        self.trap_type = self.hunting_trap_type()
        lines = self.read_union(0.52, 0.30, 0.75, 0.99)
        if find(lines, "Not enough Traps", 0.8) or find(lines, "No Trap selected", 0.8):
            have = [tile for tile in self.hunting_trap_tiles() if tile[2]]
            if not have:
                log.info("hunting: no %s traps in the bank for %s", self.trap_type or "?", pick[0])
                self.no_traps = True
                return None
            x, y, _ = have[0]
            self.click(Line("trap tile", int(x - self.px(12)), int(y - self.px(12)), self.px(24), self.px(24)),
                       "Hunting: choose the trap", settle=0.6)
            lines = self.read_union(0.52, 0.30, 0.75, 0.99)
        begin = find(lines, "Begin Hunting", 0.8)
        if not begin:
            log.info("hunting: Begin Hunting not found")
            return None
        self.click(begin, f"Hunting: begin {pick[0]}", action=True)
        if self.dry_run:
            return pick[0]
        for _ in range(4):  # the status bar can lag a few seconds behind (Wild Boar ran, 1.5 s said no)
            time.sleep(1.2)
            st = self.status()
            if st.skill == "Hunting" and similar(re.sub(r"\s+[I|l]{1,3}$", "", st.detail), pick[0]) >= 0.7:
                return pick[0]
            if find(self.read_union(0.75, 0.08, 1.0, 0.95), "Stop Hunting", 0.8) and \
                    find(self.read_union(0.75, 0.08, 1.0, 0.20), pick[0], 0.75):
                return pick[0]  # the Hunting panel on the right shows it running
        log.info("hunting: %s did not start (a tool missing?)", pick[0])
        return None

    # --- Cooking -------------------------------------------------------------------

    def cooking_recipes(self, category: str) -> list[Recipe]:
        """Unlocked recipes of one Cooking category (page already open), with ingredients in the bank."""
        cat = find(self.read(0.10, 0.08, 0.335, 0.70), category, 0.85)
        if not cat:
            log.debug("cooking: category %s not found", category)
            return []
        self.click(cat, f"cooking category {category}")
        lines = self.read_union(0.24, 0.12, 0.57, 0.95)
        # Locked cards only say "Cooking Level 23": remember them, the lowest is the next unlock.
        self.cooking_locked |= {int(m.group(1)) for ln in lines
                                for m in [re.search(r"Cooking Level\s*(\d+)", ln.text, re.I)] if m}
        recipes = []
        for name in lines:
            text = re.sub(r"^(?:\S{1,2}\s+)+(?=[A-Z])", "", name.text.strip())  # "o Trout": icon read as a letter
            if (name.h < self.px(11) or not re.fullmatch(r"[A-Za-z' ]{3,}", text)
                    or re.search(r"cooking|level|choose|recipe|food|proteins|unlock", text, re.I)):
                continue
            count = self._count_below(lines, name)  # locked cards and headers have none
            if count:
                recipes.append(Recipe(text, category, name, count[0]))
        return recipes

    def _recipe_panel(self) -> tuple[int | None, int | None]:
        """(heals, cooked pieces in the bank) of the selected recipe, from the panel on the right."""
        lines = self.read_union(0.57, 0.08, 0.80, 0.62)
        heals = None
        for ln in lines:
            m = re.search(r"heals\s*for:?\s*\+?\s*(\d+)", ln.text, re.I)
            if m:
                heals = int(m.group(1))
        haves = sorted((ln for ln in lines if similar(ln.text, "You Have") >= 0.8), key=lambda ln: ln.cy)
        stock = None
        if len(haves) >= 2:  # Input (ingredients) on top, Output (the food) below
            hdr = haves[-1]
            nums = [ln for ln in lines if 0 < ln.cy - hdr.cy < self.px(70) and abs(ln.cx - hdr.cx) < self.px(60)
                    and re.fullmatch(r"[\d,.]+\s*[KkMm]?", ln.text.strip())]
            if nums:
                stock = parse_amount(min(nums, key=lambda ln: ln.cy).text)
            else:  # a lone "0" or "5": OCR skips single digits
                stock = self.read_small_number(hdr.cx - self.px(35), hdr.cy + self.px(22), self.px(70), self.px(32),
                                               light=True)
        return heals, stock

    def survey_cooking(self, categories: list[str]) -> list[Recipe]:
        """Every unlocked food recipe with its ingredient count, healing and cooked stock."""
        if not self.open_skill("Cooking"):
            return []
        self.cooking_locked = set()
        found = []
        for category in categories:
            for r in self.cooking_recipes(category):
                self.click(r.line, f"look at recipe {r.name}", settle=0.6)
                r.heals, r.stock = self._recipe_panel()
                if r.heals is None:  # panel not drawn yet, or a missed line: look once more
                    time.sleep(0.6)
                    r.heals, r.stock = self._recipe_panel()
                if r.heals is not None:  # no healing = not food (bait and the like)
                    found.append(r)
        log.info("food: %s", ", ".join(f"{r.name} x{r.stock if r.stock is not None else '?'} (+{r.heals} HP, raw {r.raw})"
                                       for r in found) or "no food recipes read")
        return found

    def cook(self, recipe: Recipe) -> bool:
        """Select a recipe (Cooking page open) and press Create."""
        cat = find(self.read(0.10, 0.08, 0.335, 0.70), recipe.category, 0.85)
        if cat:
            self.click(cat, f"cooking category {recipe.category}")
        card = find(self._named(self.read_union(0.24, 0.12, 0.57, 0.95)), recipe.name, 0.85)
        if not card:
            log.warning("cooking: recipe %s not found", recipe.name)
            return False
        self.click(card, f"select {recipe.name}")
        create = find(self.read(0.57, 0.70, 0.80, 0.98), "Create", 0.8)
        if not create:
            log.warning("cooking: Create button not found")
            return False
        self.click(create, f"cook {recipe.name}", action=True)
        return True

    # --- Firemaking ----------------------------------------------------------------

    def survey_firemaking(self) -> list[Recipe]:
        """Unlocked log recipes in list order (= unlock order) with the logs in the bank."""
        if not self.open_skill("Firemaking"):
            return []
        head = self.read(0.10, 0.06, 0.32, 0.16)
        if not find(head, "Logs", 0.85, where=lambda ln: len(ln.text.strip()) <= 6):
            # The categories are icons without text, stacked left of the list; logs is the first.
            label = find(head, "Choose Recipe", 0.8)
            if not label:
                log.warning("firemaking: recipe list not found")
                return []
            self.click(Line("logs category", label.cx - self.px(237) - self.px(10), label.cy + self.px(69) - self.px(10),
                            self.px(20), self.px(20)), "firemaking: logs category")
        lines = self.read_union(0.12, 0.07, 0.32, 0.96)
        self.firemaking_locked = {int(m.group(1)) for ln in lines
                                  for m in [re.search(r"Firemaking Level\s*(\d+)", ln.text, re.I)] if m}
        logs = []
        for name in sorted(lines, key=lambda ln: ln.cy):
            text = name.text.strip()
            if (name.h < self.px(11) or not re.fullmatch(r"[A-Za-z' ]{3,}", text)
                    or re.search(r"choose|recipe|firemaking|level|^logs$|lanterns", text, re.I)):
                continue
            count = self._count_below(lines, name)
            if count:
                logs.append(Recipe(text, "Logs", name, count[0]))
        log.info("firemaking: %s%s", ", ".join(f"{r.name} {r.raw}" for r in logs) or "no log recipes read",
                 f"; next recipe at level {min(self.firemaking_locked)}" if self.firemaking_locked else "")
        return logs

    def burn(self, recipe: Recipe) -> bool:
        """Select a log recipe (Firemaking page open, logs category shown) and start burning."""
        card = find(self.read_union(0.12, 0.07, 0.32, 0.96), recipe.name, 0.85)
        if not card:
            log.warning("firemaking: recipe %s not found", recipe.name)
            return False
        self.click(card, f"select {recipe.name}")
        lines = self.read(0.40, 0.70, 0.85, 0.97)
        for ln in lines:  # "Oak Log [clock] 4.00s" under the button
            m = re.search(r"(\d+)[.,](\d{2})\s*s\b", ln.text)
            if m and 0.5 <= float(f"{m.group(1)}.{m.group(2)}") <= 60:
                self.burn_interval = float(f"{m.group(1)}.{m.group(2)}")
        if find_vs(lines, "Stop Burning", "Start Burning"):
            log.info("Firemaking: %s already burning", recipe.name)
            return True
        start = find_vs(lines, "Start Burning", "Stop Burning")
        if not start:
            log.warning("firemaking: Start Burning button not found")
            return False
        self.click(start, f"burn {recipe.name}", action=True)
        return True

    def stop_burning(self) -> bool:
        """Firemaking page open: press Stop Burning."""
        stop = find_vs(self.read(0.40, 0.70, 0.85, 0.97), "Stop Burning", "Start Burning")
        if not stop:
            return False
        self.click(stop, "stop burning", action=True)
        return True

    # --- Thieving (needs HP and food) --------------------------------------------------

    def read_vitals(self) -> Vitals:
        """HP and the equipped food from the header of the Thieving page (page already open)."""
        top = self.read(0.40, 0.05, 1.0, 0.15)
        eat = find(top, "Eat", 0.8, where=lambda ln: len(ln.text.strip()) <= 4)
        if not eat:  # page still drawing after a switch
            time.sleep(0.8)
            top = self.read(0.40, 0.05, 1.0, 0.15)
            eat = find(top, "Eat", 0.8, where=lambda ln: len(ln.text.strip()) <= 4)
        h, w = self.img.shape[:2]
        hp = hp_max = heal = count = None
        if eat:
            x0 = int(w * 0.40)
            text = self.read_white_text(x0, eat.cy - self.px(22), eat.x - self.px(20) - x0, self.px(44))
            m = re.search(r"(\d+)\s*/\s*(\d+)", text)
            if m and int(m.group(1)) <= int(m.group(2)):
                hp, hp_max = int(m.group(1)), int(m.group(2))
        food = next((ln for ln in top if re.search(r"\+\s*\d+\s*HP", ln.text)), None)
        if food:
            heal = int(re.search(r"\+\s*(\d+)", food.text).group(1))
            # "+63 HP [chest] 0": the count sits right of a small chest icon. The soft read first; the
            # old one (a hard mask) read 146 as 0 a few times, which sent the bot to the bank for nothing.
            count = self.soft_number(food.x + food.w + self.px(37), food.y - self.px(8), self.px(57),
                                     food.h + self.px(16), gap=9)
            if count is None:
                count = self.read_small_number(food.x + food.w + self.px(37), food.y - self.px(8),
                                               self.px(57), food.h + self.px(16), light=True)
        return Vitals(hp, hp_max, heal, count)

    def eat(self, guarded: bool = True) -> bool:
        btn = find(self.read(0.40, 0.05, 1.0, 0.15), "Eat", 0.8, where=lambda ln: len(ln.text.strip()) <= 4)
        if not btn:
            log.warning("thieving: Eat button not found")
            return False
        if guarded:
            self.click(btn, "eat", settle=0.5, action=True)
        else:  # safety eat while the user plays: past the guard that stands back for them
            if not self.dry_run:
                self.win.click(btn.cx, btn.cy)
            time.sleep(0.5)
        return True

    def read_vitals_here(self) -> Vitals | None:
        """HP and food when the Thieving header is on screen now; None otherwise. No clicks."""
        top = self.read(0.40, 0.05, 1.0, 0.15)
        if not find(top, "Eat", 0.8, where=lambda ln: len(ln.text.strip()) <= 4):
            return None
        return self.read_vitals()

    def thieving_targets(self, running: str = "") -> list[ThiefTarget]:
        """NPCs in the current location, easiest first, with their success chance."""
        lines = self.read_union(0.10, 0.12, 0.34, 0.96)
        targets = []
        for btn in lines:
            word = norm(btn.text)
            if word not in ("start", "stop"):
                continue
            # The NPC name is the big line up and to the left of its Start button.
            names = [ln for ln in lines if -self.px(320) < ln.cx - btn.cx < -self.px(100)
                     and self.px(150) < btn.cy - ln.cy < self.px(230) and ln.h >= self.px(18)
                     and re.fullmatch(r"[A-Za-z' ]{3,}", ln.text.strip())]
            if not names:
                continue
            # "Success Chance: 100.00%" is one OCR line, but "Success Chance:" and "63.64%" can come
            # back as two. Any percentage in the row of the button counts ("60 91%": dot unread).
            row = [ln for ln in lines if abs(ln.cy - btn.cy) < self.px(40) and abs(ln.cx - btn.cx) < self.px(260)
                   and re.search(r"\d\s*%", ln.text)]
            m = next((hit for hit in (re.search(r"(\d+(?:[., ]\d+)?)\s*%", ln.text) for ln in row) if hit), None)
            # "+13" sits under the portrait, left of "Your Stats"; "3.00s" right of "Waiting".
            xp_line = next((hit for hit in (re.match(r"\+\s*(\d+)", ln.text.strip()) for ln in lines
                                            if -self.px(380) < ln.cx - btn.cx < -self.px(180)
                                            and self.px(55) < btn.cy - ln.cy < self.px(120)) if hit), None)
            secs = next((hit for hit in (re.search(r"(\d+[.,]\d+)\s*s\b", ln.text) for ln in lines
                                         if abs(ln.cx - btn.cx) < self.px(160)
                                         and self.px(15) < btn.cy - ln.cy < self.px(70)) if hit), None)
            targets.append(ThiefTarget(max(names, key=lambda ln: ln.h).text.strip(),
                                       float(m.group(1).replace(",", ".").replace(" ", ".")) if m else None, btn, word == "stop",
                                       int(xp_line.group(1)) if xp_line else None,
                                       float(secs.group(1).replace(",", ".")) if secs else None))
        targets.sort(key=lambda t: t.button.cy)
        if running and targets:
            best = max(targets, key=lambda t: similar(t.name, running))
            if similar(best.name, running) >= 0.75:
                best.active = True
        return targets

    def thieving_location(self) -> str:
        """Name of the location open on the Thieving page ("Select Location   Lowtown District")."""
        lines = self.read_union(0.0, 0.05, 0.30, 0.15)  # small grey text: one pass read "LOMO'.nm District"
        head = find(lines, "Select Location", 0.8)
        if not head:
            return ""
        right = [ln for ln in lines if abs(ln.cy - head.cy) < self.px(10) and ln.x > head.x + head.w]
        return right[0].text.strip() if right else ""

    def thieving_locations(self) -> list[tuple[str, Line]]:
        """Open the location panel: (name, its Select button) of every unlocked location.
        The panel stays open; select one or close it with `close_thieving_locations`."""
        lines = self.read_union(0.10, 0.12, 1.0, 0.95)
        if not any(ln.text.strip() == "Select" for ln in lines):  # panel not open yet (a click would close it)
            head = find(self.read(0.0, 0.05, 0.40, 0.15), "Select Location", 0.8)
            if not head:
                return []
            self.click(head, "Thieving locations", settle=1.0)
            lines = self.read_union(0.10, 0.12, 1.0, 0.95)
        out = []
        for btn in (ln for ln in lines if ln.text.strip() == "Select"):
            names = [ln for ln in lines if ln.h >= self.px(16) and 0 < btn.cy - ln.cy < self.px(320)
                     and abs(ln.cx - btn.cx) < self.px(220) and re.fullmatch(r"[A-Za-z' ]{4,}", ln.text.strip())]
            if names and not any(abs(b.cx - btn.cx) < self.px(30) and abs(b.cy - btn.cy) < self.px(20) for _, b in out):
                name = min(names, key=lambda ln: ln.cy).text.strip()
                out.append((re.sub(r"^[A-Za-z]\s+(?=\w{3})", "", name), btn))  # "Z Arenas": an icon read as a letter
        return out

    def go_thieving_location(self, name: str) -> bool:
        """Switch the Thieving page to location `name` (panel closed or open)."""
        if similar(self.thieving_location(), name) >= 0.85:
            return True
        found = [(n, b) for n, b in self.thieving_locations() if similar(n, name) >= 0.85]
        if not found:
            log.info("thieving: location %s not found", name)
            return False
        self.click(found[0][1], f"Thieving location {name}", settle=1.2)
        return similar(self.thieving_location(), name) >= 0.85

    def start_thieving_npc(self, npc: str, min_success: float) -> str | None:
        """Pickpocket one named NPC (location already open) when its success chance is high enough."""
        targets = self.thieving_targets()
        for _ in range(2):
            if targets:
                break
            time.sleep(0.9)
            targets = self.thieving_targets()
        pick = next((t for t in targets if similar(t.name, npc) >= 0.85), None)
        if not pick or pick.success is None or pick.success < min_success:
            log.info("thieving: %s not there or below %.0f%% (%s)", npc, min_success, pick and pick.success)
            return None
        if pick.active:
            return pick.name
        self.click(pick.button, f"Thieving: {pick.name} ({pick.success:.0f}%)", action=True)
        return pick.name

    def start_thieving(self, min_success: float = 90.0) -> str | None:
        """Pickpocket the hardest NPC whose success chance is still at least `min_success`."""
        st = self.status()
        running = st.detail if st.skill == "Thieving" else ""
        if not self.open_skill("Thieving"):
            return None
        targets = self.thieving_targets(running)
        safe = [t for t in targets if t.success is not None and t.success >= min_success]
        for _ in range(2):  # right after a location switch the page can still be drawing
            if safe:
                break
            time.sleep(0.9)
            targets = self.thieving_targets(running)
            safe = [t for t in targets if t.success is not None and t.success >= min_success]
        log.debug("thieving targets: %s", [(t.name, t.success, t.active) for t in targets])
        if not safe:
            log.warning("thieving: no target with a success chance of %.0f%% or more", min_success)
            return None
        pick = max(safe, key=lambda t: (t.rate, t.button.cy)) if any(t.xp for t in safe) else safe[-1]
        if pick.active:
            log.info("Thieving: %s already running", pick.name)
            return pick.name
        self.click(pick.button, f"Thieving: {pick.name} ({pick.success:.0f}%)", action=True)
        return pick.name

    # --- Combat ----------------------------------------------------------------------

    COMBAT_STATS = ("Attack", "Strength", "Defence", "Ranged", "Magic", "Hitpoints")
    STANCES = ("Stab", "Slash", "Block", "Crush", "Accurate", "Powerful", "Long-Range", "Empowered", "Focused")

    def open_combat(self) -> bool:
        """The Combat page; every combat skill in the sidebar leads to it."""
        # The page title is not always read during a fight; the button row at the bottom is the
        # surer sign. A missed check cost three looks in a row without tending the fight.
        def here() -> bool:
            return self.on_page("Combat") or bool(find(self.read_union(0.60, 0.84, 1.0, 0.99), "Run from Combat", 0.8))

        if here():
            return True
        h, w = self.img.shape[:2]
        for attempt in range(3):
            side = self.read_union(0.0, 0.06, 0.10, 0.60)
            line = find([Line(re.sub(r"^(?:\S{1,2}\s+)+|\s*\d+\s*/\s*\d+\s*$", "", ln.text), ln.x, ln.y, ln.w, ln.h)
                         for ln in side], "Attack", 0.85)
            if line:
                self.click(line, "open Combat")
                return here()
            # On a larger UI scale the sidebar does not fit and may be scrolled down: "Attack" is at its top.
            self.win.scroll(int(w * 0.05), int(h * 0.4), 10)
            self.park_mouse()
            time.sleep(0.4)
        return here()

    def hp_markers(self, hp: int | None, hp_max: int) -> tuple[int | None, int | None]:
        """(auto-eat HP, max hit HP) from the two marker lines on the own HP bar (Combat, Thieving).

        The game draws a white line where its auto-eat kicks in (shrimp icon above it) and an
        orange one at the opponent's max hit (sword icon), over the full height of the bar. The
        bar's ends are measured at full HP, when the red fill spans all of it, and remembered per
        page (an empty bar part is dark navy and its edges are not reliable to find otherwise)."""
        if not hp_max:
            return None, None
        img = self.snap()
        h, w = img.shape[:2]
        text = next((ln for ln in self.read(0.11, 0.04, 0.60, 0.20)
                     if re.fullmatch(r"\d+\s*/\s*\d+", ln.text.strip())
                     and ln.cy > self.px(80)), None)  # not the bank count "145/270" in the top bar
        eat = None
        if not text:  # Thieving: OCR rarely reads "180 / 180" there as one line; the bar ends left of Eat
            eat = find(self.read(0.40, 0.05, 1.0, 0.15), "Eat", 0.8, where=lambda ln: len(ln.text.strip()) <= 4)
            if not eat:
                return None, None
        page = "combat" if text else "thieving"
        y = text.cy if text else eat.cy
        if hp is not None and hp >= hp_max:
            row = img[y].astype(int)
            red = (row[:, 0] > 120) & (row[:, 1] < 50) & (row[:, 2] < 50)
            lo = max((text.x if text else eat.x) - self.px(900), 0)
            hi = min(text.x + text.w + self.px(900), w) if text else eat.x
            xs = np.where(red[lo:hi])[0] + lo
            if len(xs) > self.px(100):
                # The run that holds the anchor: stop at the first gap wider than the text or a marker.
                anchor = text.cx if text else xs.max()
                near = xs[np.abs(xs - anchor).argmin()]
                left = right = near
                for x in sorted(xs[xs < near], reverse=True):
                    if left - x > self.px(70):
                        break
                    left = x
                for x in sorted(xs[xs > near]):
                    if x - right > self.px(70):
                        break
                    right = x
                if right - left > self.px(100):
                    self.bar_extent[page] = (int(left), int(right), w, y)
        ext = self.bar_extent.get(page)
        if not ext or ext[2] != w or abs(ext[3] - y) > self.px(6):
            return None, None
        left, right = ext[0], ext[1] + 1
        # A marker runs the full height of the bar; the HP digits do not. Look above and below them.
        band = np.concatenate([img[y - self.px(8):y - self.px(6), left:right],
                               img[y + self.px(9):y + self.px(13), left:right]]).astype(int)
        white = (band.min(axis=2) > 225).all(axis=0)
        orange = ((band[:, :, 0] > 190) & (band[:, :, 1] > 80) & (band[:, :, 1] < 200) & (band[:, :, 2] < 110)).all(axis=0)

        def at(cols) -> int | None:
            xs = np.where(cols)[0]
            xs = xs[xs > (right - left) * 0.02]  # a marker parked at the left end means "none"
            return round(xs.mean() / (right - left) * hp_max) if len(xs) else None

        return at(white), at(orange)

    def combat_vitals(self) -> Vitals:
        """Own HP (bar top left) and the food in the Combat slot (left of the Eat button)."""
        hp = hp_max = heal = count = None
        # Left of 0.11 w is the sidebar, whose skill levels ("15/99") look just like an HP reading.
        # The HP bar hangs a fixed distance under the top edge (the UI scales with the width, not
        # the height), so its band is in px, not a fraction of the height.
        h, w = self.snap().shape[:2]
        y0, y1 = self.px(100), self.px(180)
        for ln in self.read_union(0.11, y0 / h, 0.45, y1 / h):
            m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", ln.text.strip())
            if m and int(m.group(1)) <= int(m.group(2)) and int(m.group(2)) != 99:
                hp, hp_max = int(m.group(1)), int(m.group(2))
        if hp is None:  # white digits on the red bar, crossed by marker lines: read the cleaned bar
            for text in self.white_text_reads(int(w * 0.11), y0, int(w * 0.31), y1 - y0):
                m = re.search(r"(\d+)\s*/\s*(\d+)", text)
                if m and int(m.group(1)) <= int(m.group(2)) and int(m.group(2)) != 99:
                    hp, hp_max = int(m.group(1)), int(m.group(2))
                    break
        low = self.read_union(0.22, 0.84, 0.52, 0.99)
        food = next((ln for ln in low if re.search(r"\+\s*\d+\s*HP", ln.text)), None)
        if food:
            heal = int(re.search(r"\+\s*(\d+)", food.text).group(1))
            # "[chest] 177" under it. OCR reads the chest icon as a digit or letter ("8176", "S 177"),
            # so read the number on its own, right of the icon.
            # The chest starts under the "+" of "+82 HP" and the number about 30 px further. An OCR
            # line that starts right of the chest is the number itself; one that starts earlier has
            # the chest in it ("8176", "S 177"). Cutting 24 px off a clean "381" left "3" or "1".
            under = [ln for ln in low if 0 < ln.cy - food.cy < self.px(40) and abs(ln.cx - food.cx) < self.px(60)]
            clean = [ln for ln in under if ln.x >= food.x + self.px(22) and re.fullmatch(r"[\d,]{1,7}", ln.text.strip())]
            if clean:
                count = int(clean[0].text.strip().replace(",", ""))
            if count is None and under:
                # "a 148": the chest read as a separate scrap in front, the number after the space.
                words = min(under, key=lambda ln: ln.cy).text.split()
                if len(words) > 1 and re.fullmatch(r"[\d,]{1,7}", words[-1]):
                    count = int(words[-1].replace(",", ""))
            if count is None:
                count = self.soft_number(food.x + self.px(31), food.y + food.h + self.px(6), self.px(64), self.px(36))
        return Vitals(hp, hp_max, heal, count)

    def combat_eat(self) -> bool:
        btn = find(self.read_union(0.30, 0.84, 0.60, 0.99), "Eat", 0.8, where=lambda ln: len(ln.text.strip()) <= 4)
        if not btn:
            log.warning("combat: Eat button not found")
            return False
        self.click(btn, "eat", settle=0.5, action=True)
        return True

    def stance_trains(self) -> dict[str, list[str]]:
        """What every stance of the equipped weapon trains, from the tooltip (shown top right)."""
        buttons = [ln for ln in self.read_union(0.10, 0.50, 0.27, 0.92)
                   if any(similar(ln.text, s) >= 0.9 for s in self.STANCES)]
        out = {}
        for btn in buttons:
            for d in (5, 2, 0):
                self.win.move(btn.cx - d, btn.cy - d)
                time.sleep(0.25)
            time.sleep(1.4)
            img = self.snap()
            h, w = img.shape[:2]
            crop = np.ascontiguousarray(img[0:int(h * 0.10), int(w * 0.60):int(w * 0.88)])
            words = " ".join(ln.text for ln in ocr.read(crop, None, self.ocr_scale(2)))
            out[btn.text.strip()] = [s for s in self.COMBAT_STATS if s.lower() in words.lower()]
            self.park_mouse()
            time.sleep(0.5)
        log.info("stances: %s", "; ".join(f"{k} trains {', '.join(v) or '?'}" for k, v in out.items()))
        return out

    def stance_buttons(self) -> list[Line]:
        """Stance buttons of the equipped weapon. They sit under "Stance" at the bottom of the
        equipment panel, below the fold: scroll that panel down first."""
        def read() -> list[Line]:
            return [ln for ln in self.read_union(0.10, 0.30, 0.30, 0.95)
                    if any(similar(ln.text.strip(), s) >= 0.9 for s in self.STANCES)]
        found = read()
        if not found:
            w, h = self.win.size
            self.win.scroll(int(w * 0.20), int(h * 0.62), -5)
            self.park_mouse()
            time.sleep(0.6)
            found = read()
        return found

    def stance_trains_real(self) -> dict[str, list[str]]:
        """Like `stance_trains`, with the real mouse (the tooltip ignores a posted hover). Combat page open;
        only while the user is away."""
        buttons = self.stance_buttons()
        out = {}
        if not buttons:
            return out
        with self.win.real_mouse() as m:
            for btn in buttons:
                m.hover(btn.cx, btn.cy, settle=1.4)
                img = self.snap()
                h, w = img.shape[:2]
                # The tooltip can show near the button or top right: read both.
                words = " ".join(ln.text for ln in self.read_union(0.10, 0.30, 0.60, 0.95)) + " " + \
                    " ".join(ln.text for ln in ocr.read(np.ascontiguousarray(img[0:int(h * 0.10), int(w * 0.60):int(w * 0.88)]),
                                                        None, self.ocr_scale(2)))
                # Since v0.11.7 the tooltip also shows the XP split ("Attack 50%"); 0% trains nothing.
                found = [s for s in ("Attack", "Strength", "Defence")
                         if re.search(rf"{s}\s*(?:XP|Experience)", words, re.I)
                         or any(int(p) > 0 for p in re.findall(rf"{s}\W{{0,3}}(\d+)\s*%", words, re.I))]
                out[btn.text.strip()] = found
        log.info("stances: %s", "; ".join(f"{k} trains {', '.join(v) or '?'}" for k, v in out.items()))
        return out

    def current_stance(self) -> str | None:
        """The stance named in Offensive Stats ("Attack Stance  Block")."""
        lines = self.read_union(0.25, 0.15, 0.45, 0.70)
        head = next((ln for ln in lines if "Attack Stance" in ln.text), None)
        if not head:
            return None
        m = re.search(r"Attack Stance\s+(\w+)", head.text)
        if m:
            return m.group(1)
        right = [ln for ln in lines if abs(ln.cy - head.cy) < self.px(10) and ln.x > head.x + head.w]
        return right[0].text.strip() if right else None

    def set_stance(self, name: str) -> bool:
        btn = find(self.stance_buttons(), name, 0.9)
        if not btn:
            log.warning("combat: stance %s not available with this weapon", name)
            return False
        self.click(btn, f"stance {name}", settle=0.4, action=True)
        return True

    def start_fight(self, area: str, monster: str) -> bool:
        """Pick an area and a monster and press Fight (Combat page open)."""
        low = self.read_union(0.45, 0.84, 0.80, 0.99)
        opener = (find(low, "No Area Selected", 0.7) or find(low, area, 0.8) or find(low, "Combat Areas", 0.8))
        if not opener:
            log.warning("combat: area button not found")
            return False
        self.click(opener, "open the area list")
        # With no area chosen the popup opens on "No Category selected": pick Combat Areas first.
        top = self.read_union(0.05, 0.08, 0.97, 0.96)
        if find(top, "No Category selected.", 0.7):
            cat = find(top, "Combat Areas", 0.85)
            if cat:
                self.click(cat, "category Combat Areas", settle=0.6)
            else:
                log.warning("combat: no Combat Areas category in the popup")
        # The list keeps its scroll position: after a Slayer area further down, the first area was
        # out of view ("Corrupted Farmlands not in the list" three times in a row). Scroll both ways.
        h, w = self.img.shape[:2]
        place, seen = None, []
        for notches in (0, 10, -10):
            if notches:
                self.win.scroll(int(w * 0.22), int(h * 0.60), notches)
                self.park_mouse()
                time.sleep(0.5)
            seen = self.read_union(0.10, 0.22, 0.34, 0.96)
            place = find(seen, area, 0.85)
            if place:
                break
        if not place:
            log.warning("combat: area %s not in the list (saw: %s)", area,
                        ", ".join(ln.text for ln in seen[:8]) or "nothing")
            self._close_popup()
            return False
        self.click(place, f"area {area}")
        lines = self.read_union(0.32, 0.18, 0.97, 0.96)
        words = len(monster.split())
        names = [ln for ln in lines if similar(" ".join(ln.text.split()[:words]), monster) >= 0.85]
        fights = [ln for ln in lines if similar(ln.text, "Fight") >= 0.9]
        if not names or not fights:
            log.warning("combat: %s not found in %s", monster, area)
            self._close_popup()
            return False
        mon = names[0]
        self.click(min(fights, key=lambda f: abs(f.cy - mon.cy)), f"fight {monster}", action=True)
        if self.dry_run:
            self._close_popup()
        return True

    def slayer_task(self) -> tuple[str, int, int] | None:
        """Combat page: the Slayer task as (monster, kills done, kills needed).

        The block under the stats reads "Slayer Task", the master, the monster and "12 / 27"; the
        monster is the line right above the progress.
        """
        lines = self.read_union(0.20, 0.64, 0.50, 0.92)
        head = find(lines, "Slayer Task", 0.8)
        if not head:
            return None
        below = [ln for ln in lines if 0 < ln.cy - head.cy < self.px(140)]
        for ln in below:
            m = re.fullmatch(r"\s*([\dOo,]+)\s*/\s*([\dOo,]+)\s*", ln.text)
            if not m and re.fullmatch(r"\s*[\dOo,]{3,7}\s*", ln.text):
                # The slash often comes back as a 1 ("23127"): read that spot again with its soft edges.
                # The OCR box of a merged read can sit half a line too high: take a generous band.
                box = self.img[max(ln.cy - self.px(18), 0):ln.cy + self.px(22),
                               max(ln.x - self.px(6), 0):ln.x + ln.w + self.px(10)]
                lo, hi = box.min(axis=2).astype(float), box.max(axis=2).astype(float)
                raw = sentence_read(np.clip((lo - 70) / 150, 0, 1) * (hi - lo < 60)) or ""
                m = re.fullmatch(r"\s*([\dOo,]+)\s*[/|\\Il]\s*([\dOo,]+)\s*", raw)
            if not m:
                continue
            done, total = (int(re.sub(r"\D", "", g.replace("O", "0").replace("o", "0")) or 0) for g in m.groups())
            # The monster's picture left of the name reads as a few stray letters ("STL"): take the
            # longest word line right above the progress.
            names = [n for n in below if 0 < ln.cy - n.cy < self.px(45)
                     and re.fullmatch(r"[A-Za-z][A-Za-z' -]{3,}", n.text.strip())]
            if names and total:
                return max(names, key=lambda n: len(n.text.strip())).text.strip(), done, total
        return None

    def jump_to_enemy(self) -> bool:
        """Combat page: "Jump To Enemy" in the Slayer Task block starts the fight with the task's monster."""
        btn = find(self.read_union(0.20, 0.64, 0.50, 0.95), "Jump To Enemy", 0.8)
        if not btn:
            log.warning("combat: Jump To Enemy not found")
            return False
        self.click(btn, "jump to the Slayer task enemy", action=True)
        return True

    def _close_popup(self) -> None:
        close = [ln for ln in self.read_union(0.85, 0.08, 1.0, 0.22) if ln.text.strip().lower() == "x"]
        if close:
            self.click(close[0], "close popup", settle=0.5)

    def run_from_combat(self) -> bool:
        if not self.open_combat():
            return False
        btn = find(self.read_union(0.60, 0.84, 1.0, 0.99), "Run from Combat", 0.8)
        if not btn:
            return False
        self.click(btn, "run from combat", action=True)
        return True

    # --- Shop and Ranching: read only ---------------------------------------------------

    def read_gold(self) -> int | None:
        """Gold from the top bar, under "Shop". Rounded by the game ("3.4K"), so only approximate."""
        lines = self.read_union(0.15, 0.0, 0.50, 0.08)
        shop = find(lines, "Shop", 0.85)
        if not shop:
            return None
        for ln in sorted(lines, key=lambda ln: abs(ln.cx - shop.cx)):
            if ln is shop or ln.cy <= shop.cy or abs(ln.cx - shop.cx) > self.px(70):
                continue
            m = re.search(r"(\d[\d.,]*\s*[KkMm]?)\s*$", ln.text)  # the coin icon can read as a letter in front
            if m:
                return parse_amount(m.group(1).replace(" ", ""))
        return None

    def read_drop(self) -> tuple[str, int] | None:
        """The item notice bottom right ("+1 Maple Log" over "774 in Bank") as (item, pieces in the bank).
        The gained amount is kept in `last_drop_plus`."""
        name = count = None
        self.last_drop_plus = None
        for ln in self.read(0.88, 0.905, 1.0, 0.95):
            got = re.search(r"\+\s*(\d+)\s+([A-Za-z][A-Za-z' -]{2,})", ln.text)
            bank = re.search(r"([\d,.]+)\s*in\s*Ban", ln.text)
            if got and name is None:
                name = got.group(2).strip(" -'")
                self.last_drop_plus = int(got.group(1))
            if bank and count is None:
                digits = re.sub(r"[,.]", "", bank.group(1))
                count = int(digits) if digits.isdigit() else None
        return (name, count) if name and count is not None else None

    def _shop_category(self, category: str) -> Line | None:
        """The line of `category` in the Shop's category list, scrolling the list when needed.

        The list holds more categories than fit (Woodcutting is below the fold), and it keeps its
        scroll position between looks.
        """
        h, w = self.img.shape[:2]
        for notches in (0, -10, 10):
            if notches:
                self.win.scroll(int(w * 0.16), int(h * 0.55), notches)
                self.park_mouse()
                time.sleep(0.5)
            cat = find([Line(re.sub(r"\s*[^A-Za-z&\s]+\s*$", "", ln.text), ln.x, ln.y, ln.w, ln.h)
                        for ln in self.read_union(0.08, 0.06, 0.24, 0.98)], category, 0.85)
            if cat:
                return cat
        return None

    def survey_shop(self, categories: list[str]) -> list[dict]:
        """Upgrades on the first screen of each Shop category, with what they cost and what is there.

        Reads only. Each entry: {category, name, skill, level, costs: [(have, need), ...]}; the first
        cost is gold, the rest are materials (shown as icons, so they have no name here).
        """
        shop = find(self.read_union(0.15, 0.0, 0.50, 0.08), "Shop", 0.85)
        if not shop:
            log.warning("shop: button not found")
            return []
        self.click(self._top_icon(shop), "open shop")
        found, missed = [], []
        for category in categories:
            cat = self._shop_category(category)
            if not cat:
                missed.append(category)
                continue
            self.click(cat, f"shop category {category}")
            lines = self.read_union(0.22, 0.10, 1.0, 0.97)
            for limit in [ln for ln in lines if "buy limit" in ln.text.lower()]:
                card = [ln for ln in lines if abs(ln.cx - limit.cx) < self.px(240)
                        and 0 < ln.cy - limit.cy < self.px(380)]
                titles = [ln for ln in card if ln.h >= self.px(15) and ln.cy - limit.cy < self.px(60)
                          and re.search(r"[A-Za-z]{3,}", ln.text)]
                if not titles:
                    continue
                entry = {"category": category, "name": min(titles, key=lambda ln: ln.cy).text.strip(),
                         "skill": None, "level": None, "costs": []}
                for ln in sorted(card, key=lambda ln: (ln.cy, ln.cx)):
                    m = re.search(r"([A-Z][a-z]+) Level\s*(\d+|IO|I)\b", ln.text)
                    if m and entry["skill"] is None:
                        entry["skill"] = m.group(1)
                        entry["level"] = int(m.group(2).replace("IO", "10").replace("I", "1"))
                    # "Complete Hobgoblin Camp 0 / 10 times": a requirement the bot cannot buy its way past.
                    done = re.search(r"Complete\s+(.+?)\s+([\dOo]+)\s*/\s*([\dOo]+)", ln.text)
                    if done:
                        entry["task"] = (done.group(1), int(done.group(2).replace("O", "0").replace("o", "0")),
                                         int(done.group(3).replace("O", "0").replace("o", "0")))
                # Prices: small "have / need" pairs under "Costs & Requirements". In the page-wide read
                # the slash often comes out as a 1 ("27.17K 125.000"), so read that strip enlarged.
                head = [ln for ln in card if "costs" in ln.text.lower()]
                if head:
                    y0 = head[0].cy + self.px(8)
                    x0 = max(limit.cx - self.px(235), 0)
                    # Two-line requirements push the chips down, so look further than one line needs.
                    tall = np.ascontiguousarray(self.img[y0:y0 + self.px(165), x0:limit.cx + self.px(235)])
                    chips = [read_cost_chip(tall[b:d, a:c])
                             for a, b, c, d in cost_chips(tall, self.px(70), self.px(38))]
                    entry["costs"] = [(have or 0, need) for have, need in chips if need]
                    entry["exact"] = bool(chips) and all(need for _, need in chips)
                    strip = tall[:self.px(120)]
                    for scale in (2.5, 3.5) if not entry["exact"] else ():
                        text = " ".join(ln.text for ln in ocr.read(strip, None, self.ocr_scale(scale)))
                        pairs = []
                        # Zeros come back as "O" and the thousands as a space: "15,OOO", "15 000".
                        for have, need in re.findall(r"([\dOo][\dOo.,]*K?)\s*/\s*(\d[\dOo.,]*(?:\s[\dOo]{3})*)", text):
                            a = parse_amount(have.replace("O", "0").replace("o", "0"))
                            b = parse_amount(need.replace("O", "0").replace("o", "0").replace(" ", "").rstrip(".,"))
                            if a is not None and b:
                                pairs.append((a, b))
                        if len(pairs) > len(entry["costs"]):
                            entry["costs"] = pairs
                found.append(entry)
        log.info("shop: %d upgrades read in %s%s", len(found), ", ".join(c for c in categories if c not in missed),
                 f"; category not found: {', '.join(missed)}" if missed else "")
        return found

    def buy_upgrade(self, entry: dict) -> bool:
        """Shop: hold "Hold to Buy" on the card of `entry` (as returned by survey_shop)."""
        shop = find(self.read_union(0.15, 0.0, 0.50, 0.08), "Shop", 0.85)
        if not shop:
            return False
        self.click(self._top_icon(shop), "open shop")
        cat = self._shop_category(entry["category"])
        if not cat:
            return False
        self.click(cat, f"shop category {entry['category']}")
        lines = self.read_union(0.22, 0.10, 1.0, 0.97)
        for limit in [ln for ln in lines if "buy limit" in ln.text.lower()]:
            # The buy button is the last line of a card, about 417 reference px under "Buy limit"; the
            # next row starts at 493.
            card = [ln for ln in lines if abs(ln.cx - limit.cx) < self.px(240) and 0 < ln.cy - limit.cy < self.px(455)]
            titles = [ln for ln in card if ln.h >= self.px(15) and ln.cy - limit.cy < self.px(60)
                      and re.search(r"[A-Za-z]{3,}", ln.text)]
            if not titles or similar(min(titles, key=lambda ln: ln.cy).text, entry["name"]) < 0.85:
                continue
            # Several cards share a name ("+25 Bank Slots"): the level requirement tells them apart.
            if entry.get("skill") and not any(
                    re.search(rf"{entry['skill']} Level\s*{entry['level']}\b", re.sub(r"(?<=[\dIO])O|O(?=\d)", "0", ln.text.replace("IO", "10"))) for ln in card):
                continue
            buy = [ln for ln in card if is_action_button(ln.text, "Buy")]
            if not buy:
                continue
            # 2.5 s was not always enough: Slayer Loadout was "bought" eight times and never got.
            self.hold(buy[0], f"buy {entry['name']}", 4.0, action=True)
            if self.dry_run:
                return True
            time.sleep(1.2)
            # Bought: the card leaves the page (purchased ones are hidden) or its buy limit goes up.
            after = self.read_union(0.22, 0.10, 1.0, 0.97)
            same = [ln for ln in after if abs(ln.cy - limit.cy) < self.px(20) and abs(ln.cx - limit.cx) < self.px(60)
                    and "buy limit" in ln.text.lower()]
            still = any(similar(ln.text, entry["name"]) >= 0.85 and abs(ln.cx - titles[0].cx) < self.px(80)
                        and abs(ln.cy - titles[0].cy) < self.px(30) for ln in after)
            if still and same and re.sub(r"\s", "", same[0].text) == re.sub(r"\s", "", limit.text):
                log.warning("shop: %s did not get bought (button held, nothing changed)", entry["name"])
                return False
            return True
        log.info("shop: card for %s not found", entry["name"])
        return False

    def shop_item(self, category: str, name: str) -> dict | None:
        """Shop: find the card of an item for sale (no buy limit, e.g. "Normal Scarecrow"), using the
        "Jump to" icons of the category. Returns {costs: [(have, need)], buy: Line, enabled} or None.
        The amounts are for the Buy Quantity set at the top (the card says "x100")."""
        shop = find(self.read_union(0.15, 0.0, 0.50, 0.08), "Shop", 0.85)
        if not shop:
            return None
        self.click(self._top_icon(shop), "open shop")
        cat = self._shop_category(category)
        if not cat:
            return None
        self.click(cat, f"shop category {category}", settle=0.8)
        jump = find(self.read_union(0.22, 0.08, 0.60, 0.16), "Jump to", 0.7)
        for k in range(-1, 5):
            if k >= 0:
                if not jump:
                    break
                self.win.click(jump.x + jump.w + self.px(22) + k * self.px(46), jump.cy)
                time.sleep(0.9)
            lines = self.read_union(0.22, 0.06, 1.0, 0.97)
            title = find(lines, name, 0.85)
            if not title:
                continue
            card = [ln for ln in lines if abs(ln.cx - title.cx) < self.px(260) and 0 <= ln.cy - title.cy < self.px(420)]
            buy = next((ln for ln in card if is_action_button(ln.text, "Buy")), None)
            head = next((ln for ln in card if "costs" in ln.text.lower()), None)
            if not buy or not head:
                continue  # the card runs off the bottom: try the next jump
            img = self.snap()
            x0 = max(title.cx - self.px(250), 0)
            tall = np.ascontiguousarray(img[head.cy + self.px(8):buy.cy - self.px(15), x0:title.cx + self.px(250)])
            chips = [read_cost_chip(tall[b:d, a:c]) for a, b, c, d in cost_chips(tall, self.px(70), self.px(38))]
            costs = [(have or 0, need) for have, need in chips if need]
            px = img[buy.cy, buy.cx].astype(int)
            enabled = bool(px[1] > px[0] + 40 and px[1] > px[2] + 40)  # green when it can be bought, grey when not
            return {"costs": costs, "buy": buy, "enabled": enabled}
        log.info("shop: no card for %s in %s", name, category)
        return None

    def upgrade_item(self, base: str, target: str) -> bool:
        """Bank: upgrade one `base` into `target` ("Firefly" -> "Fireflies in a Jar (Ranching)").
        The panel's "Upgrade Item" block shows one variant; "Select Upgrade (n)" lists them all."""
        if not self._open_bank():
            return False
        try:
            if not self._bank_select(base):
                return False

            def shows_target(lines: list[Line]) -> bool:
                return any("x " in ln.text and similar(ln.text.split("x ", 1)[1], target) >= 0.9 for ln in lines
                           if "Fireflies" in ln.text or target.split()[0] in ln.text)

            lines = self.read_union(0.72, 0.06, 1.0, 0.90)
            if not shows_target(lines):
                if not any(ln.text.strip() == "Select" for ln in lines):
                    pick = find(lines, "Select Upgrade", 0.7)
                    if not pick:
                        log.info("bank: %s has no upgrades", base)
                        return False
                    self.click(pick, "list the upgrades", settle=0.8)
                    lines = self.read_union(0.72, 0.06, 1.0, 0.90)
                row = next((ln for ln in lines if similar(ln.text.strip(), target) >= 0.9 and ln.cy > self.px(250)), None)
                buttons = [ln for ln in lines if ln.text.strip() == "Select"]
                if not row or not buttons:
                    log.info("bank: upgrade %s not listed", target)
                    return False
                btn = min(buttons, key=lambda b: abs(b.cy - row.cy - self.px(17)))
                self.click(btn, f"choose {target}", settle=0.8)
                lines = self.read_union(0.72, 0.06, 1.0, 0.90)
                if not shows_target(lines):
                    log.info("bank: could not choose %s", target)
                    return False
            hold = next((ln for ln in lines if is_action_button(ln.text, "Upgrade")), None)
            if not hold:
                return False
            self.hold(hold, f"upgrade {base} into {target}", 2.5, action=True)
            return True
        finally:
            self._clear_bank_search()

    def ranch_land(self) -> tuple[int, Line] | None:
        """Ranching page open: (price, Purchase button) of the next land for sale, or None."""
        lines = self.read_union(0.10, 0.06, 0.80, 0.60)
        for head in sorted((ln for ln in lines if similar(ln.text.strip(), "Purchase Land") >= 0.85), key=lambda ln: ln.cx):
            price_line = next((ln for ln in lines if 0 < ln.cy - head.cy < self.px(60) and abs(ln.cx - head.cx) < self.px(120)
                               and "/" in ln.text), None)
            button = next((ln for ln in lines if similar(ln.text.strip(), "Purchase") >= 0.9
                           and 0 < ln.cy - head.cy < self.px(110) and abs(ln.cx - head.cx) < self.px(80)), None)
            if price_line and button:
                price = parse_amount(price_line.text.split("/")[-1].strip())
                if price:
                    return price, button
        return None

    def buy_ranch_land(self) -> bool:
        """Buy the next Ranching land (the leftmost "Purchase Land" card). Land only, never animals."""
        if not self.open_skill("Ranching"):
            return False
        land = self.ranch_land()
        if not land:
            return False
        before = land[0]
        self.click(land[1], f"buy Ranching land ({before:,} GP)", action=True, settle=1.2)
        lines = self.read_union(0.30, 0.25, 0.70, 0.80)
        confirm = find(lines, "Confirm", 0.85) or find(lines, "Purchase", 0.9)
        if confirm and confirm.cy > land[1].cy + self.px(40):
            self.click(confirm, "confirm the land purchase", action=True, settle=1.2)
        after = self.ranch_land()
        return self.dry_run or not after or after[0] != before

    def ranch_stats(self) -> dict[str, int]:
        """Happiness, Hunger, Comfort and Satisfaction of the enclosure, in percent."""
        if not self.open_skill("Ranching"):
            return {}
        text = " ".join(ln.text for ln in self.read_union(0.30, 0.08, 0.80, 0.18))
        return {key.lower(): int(value) for key, value in
                re.findall(r"(Happiness|Hunger|Comfort|Satisfaction)\W{0,3}(\d{1,3})\s*%", text)}

    # --- Bank ------------------------------------------------------------------------
    # The item panel holds Equip, Open and Sell controls close together. Every click target below is
    # looked up under its own header and above "Sell Item"; nothing in the Sell block is ever clicked.

    def _top_icon(self, label: Line) -> Line:
        """The icon above a top-bar label ("Bank", "Shop"). A click on the label itself was lost after
        the Cooking page had been read (four times in a row); the icon always took it."""
        return Line(label.text, label.x, label.y - self.px(26), label.w, self.px(18))

    def _open_bank(self) -> bool:
        bank = find(self.read_union(0.15, 0.0, 0.50, 0.08), "Bank", 0.85)
        if not bank:
            log.warning("bank: button not found")
            return False
        self.click(self._top_icon(bank), "open bank")
        # Right after containers were opened the header was not readable three times in a row
        # (the notices of what came out were still on screen): give it a few seconds. A click can
        # also be lost (the Cooking page stayed up once, nothing in the way): then click again.
        for attempt in range(5):
            if find(self.read_union(0.10, 0.07, 0.40, 0.13), "Bank Space", 0.8):
                return True
            if self.confirm_item_opened() is None and self.dismiss_popup() is None:
                if attempt in (1, 3) and not self.on_page("Bank"):
                    self.click(self._top_icon(bank), "open bank (again)")
                time.sleep(1.5)
        log.warning("bank: page did not open")
        try:  # keep what the screen looked like, to find out what was in the way
            Image.fromarray(self.img).save(pathlib.Path(__file__).resolve().parent.parent / "logs" / "bank_not_open.png")
        except OSError:
            pass
        return False

    def _clear_bank_search(self) -> None:
        # The same box the search field is read in: a narrower one missed the lone "x" every time.
        clear = [ln for ln in self.read_union(0.40, 0.05, 0.75, 0.15) if ln.text.strip().lower() == "x"]
        if clear:
            self._bank_clear = clear[0]
            self.click(clear[0], "clear bank search", settle=0.4)
            return
        # The lone "x" is not always read (with "Coal" in the box it was missed six times in a row,
        # which blocked every bank action after it). Fall back on where it was, then on Backspace.
        if find(self.read_union(0.40, 0.05, 0.75, 0.15), "Search for item...", 0.6):
            return  # the box is empty already
        if getattr(self, "_bank_clear", None):
            self.click(self._bank_clear, "clear bank search (remembered spot)", settle=0.4)
            if find(self.read_union(0.40, 0.05, 0.75, 0.15), "Search for item...", 0.6):
                return
        if getattr(self, "_bank_box", None):
            self.click(self._bank_box, "bank search box", settle=0.3)
            self.win.clear_field(40)
            time.sleep(0.4)

    def _bank_tabs(self) -> list[Line]:
        """All bank tab labels; tabs OCR missed are filled in from the even spacing of the others."""
        seen = {}
        for ln in self.read_union(0.10, 0.12, 0.60, 0.19):
            m = re.fullmatch(r"Tab\s*(\d+)", ln.text.strip())
            if m:
                seen[int(m.group(1))] = ln
        if len(seen) < 2:
            return list(seen.values())
        lo, hi = min(seen), max(seen)
        step = (seen[hi].cx - seen[lo].cx) / (hi - lo)
        ref = seen[lo]
        return [seen.get(n) or Line(f"Tab {n}", int(ref.x + (n - lo) * step), ref.y, ref.w, ref.h)
                for n in range(1, hi + 1)]

    @staticmethod
    def item_title(text: str) -> str:
        """Item name from a bank panel title: icon scraps in front ("o Trout") and the price after it go."""
        text = re.sub(r"^(?:\S{1,2}\s+)+", "", text.strip())
        words = [("2H" if wd in ("ZH", "2h") else wd) for wd in text.split()]  # "Copper 2H Sword"
        # "Fireflies in a Jar (Firemaking)": the skill in brackets is part of the name.
        name = list(itertools.takewhile(lambda wd: re.fullmatch(r"[A-Za-z']{1,}|2H|\([A-Za-z]+\)", wd), words))
        # The coin icon of the price can read as one letter ("Sardine e", "Pike a"): that cost the
        # match, so Sardine 334 and Pike were "not found" though they sat in the bank.
        while len(name) > 1 and len(name[-1]) == 1:
            name.pop()
        return " ".join(name)

    def _result_tabs(self, tabs: list[Line]) -> list[Line]:
        """Bank tabs worth a look during a search, best first.

        The game colours a tab that holds a search result orange (about 68, 59, 20 against the plain
        34, 33, 30). The open tab is green (23, 66, 20) whatever it holds, so it stays a candidate.
        Plain tabs hold nothing and are skipped; if no colour is recognised at all, every tab is tried.
        """
        img = self.snap()
        hits, opened = [], []
        for tab in tabs:
            patch = img[max(tab.y - self.px(12), 0):max(tab.y - self.px(4), 1), tab.x:tab.x + tab.w]
            if not patch.size:
                continue
            r, g, b = patch.reshape(-1, 3).mean(axis=0)
            if r - b > 25 and g - b > 20 and r >= g - 5:
                hits.append(tab)
            elif g - r > 20 and g - b > 20:
                opened.append(tab)
        return (hits + opened) or tabs

    def _bank_select(self, name: str) -> list[Line] | None:
        """Bank open: search `name`, select that exact item and return the lines of its panel.

        Tried twice: a Balaclava lying in tab 1 was missed on one look and found on the next.
        """
        self.bank_seen: list[str] = []
        panel = self._bank_select_once(name)
        if panel is None:
            self._clear_bank_search()
            panel = self._bank_select_once(name)
        if panel is None:
            # Mackerel x371 on the Cooking page was "not found" in three results: log what they were.
            log.info("bank: %s not found (results: %s)", name, "; ".join(dict.fromkeys(self.bank_seen)) or "none")
            self.bank_missing = name  # the search found nothing (not a bank that failed to open)
        return panel

    def _bank_select_once(self, name: str) -> list[Line] | None:
        head = self.read_union(0.40, 0.05, 0.75, 0.15)
        box = find(head, "Search for item...", 0.6)
        if not box:  # an old search is still in the box
            self._clear_bank_search()
            box = find(self.read_union(0.40, 0.05, 0.75, 0.15), "Search for item...", 0.6)
        if not box:
            log.warning("bank: search box not found")
            return None
        self._bank_box = box
        self.click(box, "bank search box", settle=0.4)
        # A posted "(" never arrives ("Fireflies in a Jar Firemaking)" found nothing): search the name
        # without the bracketed part and pick the right result by its title below.
        self.win.type_text(re.sub(r"\s*\(.*?\)", "", name).strip())
        time.sleep(0.9)
        w = self.img.shape[1]
        tabs = self._bank_tabs()
        for tab in (self._result_tabs(tabs) if tabs else [None]):
            if tab:
                self.click(tab, f"bank {tab.text}", settle=0.5)
            # Item cells hang under the tab labels: first cell just left of "Tab 1", 95 px apart.
            first = tabs[0] if tabs else None
            row_y = (tab.cy if tab else self.px(205)) + self.px(116)
            img = self.snap()
            for i in range(4):
                cx = (first.cx - self.px(20) if first else int(w * 0.125)) + i * self.px(95)
                # An item cell has a picture in it; an empty spot is flat dark (search hides the rest).
                if img[row_y - self.px(20):row_y + self.px(20), cx - self.px(20):cx + self.px(20)].std() < 12:
                    break
                cell = Line(f"bank item {i + 1}", cx - self.px(12), row_y - self.px(12), self.px(24), self.px(24))
                self.click(cell, "select bank item", settle=0.6)
                panel = self.read_union(0.72, 0.06, 1.0, 0.90)
                # The title line also holds the price ("Sardine [coin] 22") and can start with the
                # icon read as letters ("o Trout 66"): compare the bare name.
                titles = [self.item_title(ln.text) for ln in panel if ln.cy < self.px(200) and ln.h >= self.px(15)]
                if any(similar(t, name) >= 0.9 for t in titles):
                    return panel
                self.bank_seen.append(" / ".join(t for t in titles if t)[:60] or "?")
        return None

    def equip_food(self, name: str, slot: str) -> bool:
        """Bank: equip the food `name` in the food slot of `slot` (e.g. "Thieving")."""
        self.food_already_equipped = False
        self.bank_missing = None
        if not self._open_bank():
            return False
        try:
            panel = self._bank_select(name)
            if not panel or not any(similar(ln.text, "Food") >= 0.8 for ln in panel):
                return False
            sell = find(panel, "Sell Item", 0.7)
            heads = [ln for ln in panel if norm(ln.text).split()[-1:] == [norm(slot)]
                     and (not sell or ln.cy < sell.cy)]
            if not heads:
                log.warning("bank: no %s food slot shown for %s", slot, name)
                return False
            head_line = min(heads, key=lambda ln: ln.cy)
            below = [ln for ln in panel if 0 < ln.cy - head_line.cy < self.px(120)
                     and (not sell or ln.cy < sell.cy - self.px(20))]
            if any(similar(ln.text, "Already Equipped") >= 0.8 for ln in below):
                log.info("bank: %s already equipped for %s", name, slot)
                self.food_already_equipped = True
                return True
            equip = [ln for ln in below if norm(ln.text) == "equip"]
            if not equip:
                log.warning("bank: no Equip button under %s for %s", slot, name)
                return False
            self.click(min(equip, key=lambda ln: ln.cy), f"equip {name} for {slot}", action=True)
            return True
        finally:
            self._clear_bank_search()

    def worn_in_panel(self, panel: list[Line], name: str) -> bool:
        """Is the selected bank item also worn? Its Equip block then says "Will Replace: <itself>".

        A worn piece is not in the bank stack, so the stack alone undercounts what the user owns.
        """
        for ln in panel:
            if "will replace" not in ln.text.lower():
                continue
            near = [ln] + [o for o in panel if 0 < o.cy - ln.cy < self.px(45) and abs(o.x - ln.x) < self.px(120)]
            for o in near:
                text = re.sub(r"\s*\(\d+\)\s*$", "", re.sub(r"^.*Will Replace\W*", "", o.text)).strip()
                if text and similar(text, name) >= 0.85:
                    return True
        return False

    def _sell_rows(self) -> tuple[list[Line], tuple[int, int] | None]:
        """Lines of the Sell block of the selected item and its "Sell N for X GP" as (N, X)."""
        lines = self.read_union(0.72, 0.06, 1.0, 0.95)
        # The block header, not the button: with Disable Press and Hold the button reads "Sell Items",
        # which beat a header read with its icon ("(9 Sell Item"). The header is the upper one.
        heads = [ln for ln in lines if re.search(r"Sell Item", ln.text) and not re.search(r"Sell Items\s*$", ln.text.strip())]
        head = min(heads, key=lambda ln: ln.cy) if heads else find(lines, "Sell Item", 0.7)
        if not head:
            return [], None
        rows = [ln for ln in lines if head.cy + self.px(10) < ln.cy < head.cy + self.px(150)]
        offer = None
        for ln in rows:
            m = re.search(r"sell\s+([\d,.]+)\s+for\D{0,3}([\d,.]+)\s*GP", ln.text, re.I)
            if m:
                offer = (parse_amount(m.group(1)) or 0, parse_amount(m.group(2)) or 0)
        return rows, offer

    def appraise_item(self, name: str) -> tuple[int, float] | None:
        """Bank: (pieces in the bank, GP per piece) of `name`, read from the Sell block. Sells nothing."""
        if not self._open_bank():
            return None
        try:
            if not self._bank_select(name):
                return None
            rows, _ = self._sell_rows()
            every = [ln for ln in rows if re.search(r"\bAll\s*$", ln.text)]
            if not every:
                return None
            ln = every[0]
            self.click(Line("All", ln.x + ln.w - self.px(26), ln.y, self.px(22), ln.h), f"count the stack of {name}", settle=0.5)
            _, offer = self._sell_rows()
            if not offer or not offer[0]:
                return None
            return offer[0], offer[1] / offer[0]
        finally:
            self._clear_bank_search()

    def sell_item(self, name: str, plan) -> int:
        """Bank: sell part of the stack of `name`. Returns the GP received (0 = nothing sold).

        `plan(have, unit_price)` decides how many to sell once the stack size and price are known.
        The amount the game says it will sell is checked against that number before the hold; any
        mismatch aborts. This is the only place that touches the Sell block.
        """
        self.last_stack = None
        if not self._open_bank():
            return 0
        try:
            panel = self._bank_select(name)
            if not panel:
                return 0
            self.last_worn = self.worn_in_panel(panel, name)
            rows, _ = self._sell_rows()
            every = [ln for ln in rows if re.search(r"\bAll\s*$", ln.text)]
            if not every:
                log.warning("sell: no All button for %s", name)
                return 0
            ln = every[0]  # "All but 1 | All": the right end is All; it only fills in the amount
            self.click(Line("All", ln.x + ln.w - self.px(26), ln.y, self.px(22), ln.h), f"count the stack of {name}", settle=0.5)
            rows, offer = self._sell_rows()
            if not offer or not offer[0]:
                log.warning("sell: could not read the stack of %s", name)
                return 0
            have, total = offer
            self.last_stack = have
            unit = total / have
            amount = int(plan(have, unit))
            if amount <= 0:
                log.info("sell: %s x%d at %.1f GP - nothing to spare", name, have, unit)
                return 0
            amount = min(amount, have)
            field = [r for r in rows if re.fullmatch(r"[\d,.]+", r.text.strip()) and abs(r.cy - ln.cy) < self.px(12)]
            fx, fy = (field[0].cx, field[0].cy) if field else (ln.x - self.px(100), ln.cy)
            self.win.click(fx, fy, settle=0.1)
            time.sleep(0.3)
            self.win.clear_field()
            self.win.type_text(str(amount))
            self.win.press_enter()
            time.sleep(0.6)
            self.park_mouse()
            rows, offer = self._sell_rows()
            if not offer or offer[0] != amount:
                log.warning("sell: wanted %d %s but the game shows %s - not selling", amount, name, offer)
                return 0
            hold = [r for r in rows if is_action_button(r.text, "Sell Items")]
            if not hold:
                log.warning("sell: no sell button for %s", name)
                return 0
            self.hold(hold[0], f"sell {amount} {name} for {offer[1]} GP", 2.5, action=True)
            if self.dry_run:
                return 0
            log.info("sold %d %s for %d GP (%d left)", amount, name, offer[1], have - amount)
            self.last_stack = have - amount
            self.last_sale = (amount, name, offer[1])
            return offer[1]
        finally:
            self._clear_bank_search()

    def _bank_matches(self, word: str):
        """Bank open: type `word` in the search box and yield (title, panel lines) of every item shown.

        The search also matches the type and description lines of an item, not only its name:
        "Utility" lists every Toolbelt item ("Slot: Fishing Utility").
        """
        box = find(self.read_union(0.40, 0.05, 0.75, 0.15), "Search for item...", 0.6)
        if not box:  # an old search is still in the box
            self._clear_bank_search()
            box = find(self.read_union(0.40, 0.05, 0.75, 0.15), "Search for item...", 0.6)
        leftover = False
        if not box and getattr(self, "_bank_box", None):
            # An old word the clear did not remove hides the placeholder: use the box where it was
            # and empty it with Backspace (a warning here cost half a minute each errand round).
            box, leftover = self._bank_box, True
        if not box:
            log.warning("bank: search box not found")
            return
        self._bank_box = box
        self.click(box, "bank search box", settle=0.4)
        if leftover:
            self.win.clear_field(40)
        self.win.type_text(word)
        time.sleep(0.9)
        tabs = self._bank_tabs()
        seen = set()
        for tab in self._result_tabs(tabs):
            self.click(tab, f"bank {tab.text}", settle=0.5)
            row_y = tab.cy + self.px(116)
            img = self.snap()
            for i in range(8):
                cx = tabs[0].cx - self.px(20) + i * self.px(95)
                if img[row_y - self.px(20):row_y + self.px(20), cx - self.px(20):cx + self.px(20)].std() < 12:
                    break
                self.click(Line(f"bank item {i + 1}", cx - self.px(12), row_y - self.px(12), self.px(24), self.px(24)),
                           "select bank item", settle=0.6)
                panel = self.read_union(0.72, 0.06, 1.0, 0.97)
                # The panel's own tabs ("Selected Item", "Bank Settings") sit just above the item title.
                heads = [ln for ln in panel if ln.cy < self.px(200) and ln.h >= self.px(15)
                         and re.match(r"[A-Za-z']{3,}", ln.text.strip())
                         and not re.search(r"selected item|bank settings", ln.text, re.I)]
                if not heads:
                    continue
                title = self.item_title(min(heads, key=lambda ln: ln.cy).text)
                if title and title not in seen:
                    seen.add(title)
                    yield title, panel

    def gear_panel(self, name: str) -> dict | None:
        """Bank: look up a piece of equipment. None when it is not in the bank (no panel at all).

        Returns {stack, worn, need: (skill, level) or None, can_equip, equip_line}. A worn piece
        keeps a placeholder with 0 pieces. When the level is too low, the Equip block shows the
        requirement instead of "Equip To".
        """
        panel = self._bank_select(name)
        if not panel:
            return None
        sell = find(panel, "Sell Item", 0.7)
        head = find(panel, "Equip Item", 0.8) or find(panel, "Equip to Toolbelt", 0.8)  # Toolbelt items say the latter
        top = [ln for ln in panel if not head or ln.cy < head.cy]
        need = next(((m.group(1), int(m.group(2))) for m in
                     (re.search(r"(Defence|Attack|Strength|Ranged|Magic) Level\s*(\d+)", ln.text.replace("IO", "10"))
                      for ln in top) if m), None)
        block = [ln for ln in panel if head and head.cy < ln.cy and (not sell or ln.cy < sell.cy - self.px(10))]
        # A Toolbelt slot can be locked: "Purchase Slayer -> Combat Toolbelt" or "Achieve ..." under it.
        equip = next((ln for ln in block if re.search(r"Equip To\W", ln.text, re.I) and not any(
            0 < o.cy - ln.cy < self.px(45) and re.search(r"Purchase|Achieve|Requires|Unlock", o.text)
            for o in block)), None)
        offer = next((m for m in (re.search(r"sell\s+(\d+)\s+for", ln.text, re.I)
                                  for ln in panel if sell and ln.cy > sell.cy) if m), None)
        return {"stack": int(offer.group(1)) if offer else 0, "worn": self.worn_in_panel(panel, name),
                "need": need, "can_equip": equip is not None, "equip_line": equip}

    def equip_gear(self, name: str) -> bool:
        """Bank: put on `name` (Combat loadout) when it is in the bank and the level allows it."""
        if not self._open_bank():
            return False
        try:
            info = self.gear_panel(name)
            if not info or info["worn"]:
                return bool(info and info["worn"])
            if not info["stack"] or not info["can_equip"]:
                return False
            self.click(info["equip_line"], f"equip {name}", action=True)
            time.sleep(0.8)
            again = self.read_union(0.72, 0.06, 1.0, 0.90)
            if self.dry_run or self.worn_in_panel(again, name):
                return True
            # A Toolbelt item without a placeholder leaves the bank when its last piece goes on:
            # the panel then shows something else (or nothing), and that is success too.
            title = next((ln for ln in again if ln.cy < self.px(200) and ln.h >= self.px(15)
                          and similar(self.item_title(ln.text), name) >= 0.9), None)
            return title is None
        finally:
            self._clear_bank_search()

    def smith_one(self, category: str, recipe_name: str) -> bool:
        """Smithing: make exactly one `recipe_name` from `category`, then stop.

        Create keeps going until the bars run out, so the bot presses Stop as soon as the first
        one is done (the recipe takes a few seconds).
        """
        if not self.open_skill("Smithing"):
            return False
        recipes = [r for r in self.category_recipes("Smithing", category) if similar(r.name, recipe_name) >= 0.9]
        if not recipes:
            log.info("smithing: no recipe %s in %s", recipe_name, category)
            return False
        r = recipes[0]
        if r.raw < r.need:
            log.info("smithing: %s needs %d bars, %d there", r.name, r.need, r.raw)
            return False
        if not self.create_recipe("Smithing", r) or self.dry_run:
            return self.dry_run
        time.sleep(4.0)
        stop = find(self.read_union(0.74, 0.70, 1.0, 0.99), "Stop", 0.85)
        if stop:
            self.click(stop, f"stop smithing after one {r.name}", action=True)
        log.info("smithing: made %s", r.name)
        return True

    TOOLBELT_WORDS = ("Utility", "Helmet", "Gloves", "Boots", "Cape", "Amulet", "Ring", "Potion")  # potions sit in a Toolbelt slot

    @staticmethod
    def _potion_base(name: str) -> str:
        """"Woodcutting Potion P1-A (1)" -> "Woodcutting Potion": the tier code and count differ by read."""
        name = re.sub(r"\s*\([\d,.]+[KkMm]?\)\s*$", "", name.strip())  # "(1)", "(1,201)"
        return re.sub(r"\s+P[\dIl]+\s*-?\s*[A-Z]?\s*$|\s+P\s*$", "", name).strip()

    def toolbelt_items(self, equip: bool = True) -> list[dict]:
        """Bank: every Toolbelt item (skill gear with a bonus) and its slots; fills an empty slot.

        Each skill has its own loadout. Toolbelt items are equipped from the bank: the block
        "Equip to Toolbelt" lists "Equip To: <Skill> Utility <n>" with below it "Will Replace: <item>"
        (slot taken), "Achieve <...>" (slot locked) or nothing (slot free). A worn item keeps a
        placeholder with 0 pieces in the bank, so the stack is checked before anything is clicked.
        Entry: {name, skill, bonuses, stack, slots: [{n, state, item}]}; state is free/taken/locked.
        """
        if not self._open_bank():
            return []
        out = []
        try:
            done = set()
            # Toolbelt slots are "<Skill> Utility" but also "<Skill> Helmet" (a Balaclava) and so on;
            # the search has no word for "Toolbelt" itself, so every slot word gets its own search.
            matches = (hit for word in self.TOOLBELT_WORDS for hit in self._bank_matches(word))
            for title, panel in matches:
                head, sell = find(panel, "Equip to Toolbelt", 0.8), find(panel, "Sell Item", 0.7)
                if not head or not sell or title in done:
                    continue
                done.add(title)
                skill = next((m.group(1) for m in (re.search(r"Slot\W+(?:\S{1,2}\s+)?([A-Z][a-z]+)\s+[A-Z][a-z]+", ln.text)
                                                     for ln in panel if ln.cy < head.cy) if m), None)
                if not skill:  # potions name no slot up top: "Equip To: Woodcutting Potion" does
                    skill = next((m.group(1) for m in (re.search(r"Equip To\W+(?:\S{1,2}\s+)?([A-Z][a-z]+)\s+[A-Z][a-z]+", ln.text, re.I)
                                                         for ln in panel if ln.cy > head.cy) if m), None)
                top = sorted((ln for ln in panel if ln.cy < head.cy), key=lambda ln: ln.cy)
                start = next((ln.cy for ln in top if "bonuses" in ln.text.lower()), None)
                bonuses = [ln.text.strip() for ln in top if start is not None and ln.cy > start
                           and re.match(r"[+-]?\d", ln.text.strip())]
                # The Sell block says "Sell 1 for 8,750 GP" with a piece in the bank and "Sell 0 for 0 GP"
                # for a placeholder (a zero often reads as a letter, so no match also means none).
                offer = next((m for m in (re.search(r"sell\s+(\d+)\s+for\D{0,3}[\d,.]+\s*GP", ln.text, re.I)
                                          for ln in panel if ln.cy > sell.cy) if m), None)
                stack = int(offer.group(1)) if offer else 0
                block = sorted((ln for ln in panel if head.cy < ln.cy < sell.cy - self.px(10)), key=lambda ln: ln.cy)
                targets = [ln for ln in block if re.search(r"Equip To\W", ln.text, re.I)]  # "Equip TO:" happens
                slots = []
                for k, ln in enumerate(targets):
                    stop = targets[k + 1].cy if k + 1 < len(targets) else sell.cy
                    below = [b for b in block if ln.cy + self.px(8) < b.cy < stop - self.px(8)]
                    text = " ".join(b.text for b in below)
                    worn = re.sub(r"^.*Will Replace\W*", "", text).strip() if "replace" in text.lower() else ""
                    state = "taken" if worn else "locked" if "achieve" in text.lower() or below else "free"
                    number = re.search(r"(\d)\s*$", ln.text)  # "Fishing Utility 2"; a single slot has no number
                    slots.append({"n": int(number.group(1)) if number else 1, "state": state,
                                  "item": re.sub(r"\s*\(\d+\)\s*$", "", worn), "line": ln})
                entry = {"name": title, "skill": skill, "bonuses": bonuses, "stack": stack,
                         "slots": [{k: v for k, v in s.items() if k != "line"} for s in slots]}
                out.append(entry)
                log.info("gear: %s (%s; %s) x%d, slots %s", title, skill, ", ".join(bonuses) or "no bonus read", stack,
                         ", ".join(f"{s['n']} {s['state']}{' ' + s['item'] if s['item'] else ''}" for s in slots))
                free = [s for s in slots if s["state"] == "free"]
                wearing = any(similar(s["item"], title) >= 0.9 for s in slots)
                # A potion is used up: the same potion in its slot ("Will Replace: Woodcutting Potion
                # P1-A (1)") is topped up with the whole stack. Any other item in a taken slot stays.
                refill = next((s for s in slots if s["state"] == "taken" and "potion" in title.lower()
                               and similar(self._potion_base(s["item"]), self._potion_base(title)) >= 0.9), None)
                if equip and stack >= 1 and free and not wearing:
                    self.click(free[0]["line"], f"equip {title} in {skill} Utility {free[0]['n']}", action=True)
                    entry["equipped"] = not self.dry_run
                elif equip and stack >= 1 and refill:
                    every = [b for b in panel if head.cy < b.cy < refill["line"].cy and re.search(r"\bAll\s*$", b.text)]
                    if every:  # "All but 1 | All": the right end is All
                        ln = min(every, key=lambda b: b.cy)
                        self.click(Line("All", ln.x + ln.w - self.px(26), ln.y, self.px(22), ln.h),
                                   f"all {stack} {title}", settle=0.5)
                        self.click(refill["line"], f"top up {title} in {skill} Potion", action=True)
                        entry["topped_up"] = 0 if self.dry_run else stack
                    else:
                        log.warning("gear: no All button for %s, not topped up", title)
        finally:
            self._clear_bank_search()
        return out

    # Info dialogs with only a Confirm button. They dim the page and swallow every click until
    # closed. Dialogs that spend or sell (shop, bank) are never closed here.
    POPUP_TITLES = ("New Unlock", "Level Up", "Congratulations", "Achievement", "Mastery", "Unlocked",
                    "You can now access", "New Pet Located")
    POPUP_NEVER = ("Buy", "Purchase", "Sell", "Cost", "Delete", "Are you sure")

    def start_screen(self) -> str:
        """While the game starts: "loading", "loaded" (pressed Load on Character Selection), "choose"
        (several characters: the user picks) or "game" (the game itself, or its Welcome Back dialog).
        One quick OCR pass of the middle, so it can be asked every few seconds."""
        img = self.win.capture()
        h, w = img.shape[:2]
        lines = ocr.read(img, (int(w * 0.15), int(h * 0.05), int(w * 0.70), int(h * 0.80)), 1.0)
        if find(lines, "Welcome Back!", 0.85) or any("you were away" in norm(ln.text) for ln in lines):
            return "game"
        loads = [ln for ln in lines if similar(ln.text.strip(), "Load") >= 0.9 and len(ln.text.strip()) <= 5]
        if loads and (find(lines, "Character Selection", 0.8) or find(lines, "Import Save", 0.8)):
            if len(loads) > 1:
                return "choose"
            self.img = img
            self.click(loads[0], "load the character (Character Selection)", settle=1.0)
            return "loaded"
        return "game" if self._measure_scale() else "loading"

    def dismiss_popup(self) -> str | None:
        """Close an info pop-up ("New Unlock! Quests") when one covers the page.

        Returns the dialog's text for the journal, or None when there was none."""
        if self.confirm_item_opened() is not None:
            return "Item Opened"
        # The game menu (Escape): close it with Resume and nothing else; it also holds "Save and Quit".
        menu = self.read_union(0.38, 0.10, 0.62, 0.80)
        # Character Selection, after the game restarted (a Steam update): load the one character.
        # Never with more than one: which one to play is the user's choice.
        loads = [ln for ln in menu if similar(ln.text.strip(), "Load") >= 0.9 and len(ln.text.strip()) <= 5]
        if loads and (find(menu, "Import Save", 0.8) or find(menu, "Character Selection", 0.8)):
            if len(loads) == 1:
                self.click(loads[0], "load the character (Character Selection)", settle=6.0)
                return "Character Selection: loaded the character"
            log.warning("character selection: %d characters - leaving the choice to the user", len(loads))
            return None
        # Right after Load: "Welcome Back! You were away for ..." lists the offline gains and blocks
        # every click until Confirm. Its title sits high, above the band read for other pop-ups.
        away = next((ln for ln in menu if "you were away" in norm(ln.text)), None)
        if away or find(menu, "Welcome Back!", 0.85):
            confirm = find(self.read_union(0.28, 0.55, 0.72, 0.97), "Confirm", 0.85)
            if confirm:
                self.click(confirm, "close Welcome Back (offline gains)", settle=1.5)
                return f"Welcome Back: {away.text.strip()}" if away else "Welcome Back"
        resume = find(menu, "Resume", 0.85)
        if resume and find(menu, "Save and Quit to Desktop", 0.8):
            self.click(resume, "close the game menu (Resume)", settle=0.8)
            return "Game menu"
        lines = self.read_union(0.28, 0.55, 0.72, 0.97)
        confirm = find(lines, "Confirm", 0.85)
        if not confirm:
            return None
        text = " ".join(ln.text for ln in sorted(lines, key=lambda l: l.cy) if ln is not confirm)
        if not any(norm(t) in norm(text) for t in self.POPUP_TITLES):
            return None
        if any(re.search(rf"\b{w}\b", text, re.I) for w in self.POPUP_NEVER):
            log.info("pop-up left open (it spends or sells): %s", text[:80])
            return None
        self.click(confirm, "close the pop-up", settle=0.8)
        return text.strip()

    ITEM_KINDS = (  # (kind, pattern in the item's description), first match wins
        ("container", r"\bContains\b|Open Item"),
        ("compost", r"used as Compost"),
        ("bait", r"used as Bait"),
        ("gear", r"Primary Slot|Equipment|Equip Item|Toolbelt"),
        ("food", r"Heals|\+\s*\d+\s*HP|Consumable: Yes"),
        ("seed", r"\bSeeds?\b"),
    )

    def item_info(self, name: str) -> dict | None:
        """Bank: what an item is, from its panel: {name, kind, text}. None when it is not found."""
        if not self._open_bank():
            return None
        try:
            panel = self._bank_select(name)
            if not panel:
                return None
            lines = sorted((ln for ln in panel if ln.cy < self.img.shape[0] * 0.62), key=lambda ln: (ln.cy // 8, ln.x))
            stop = next((ln.cy for ln in lines if re.search(r"Found in|Stack Value|Sell Item", ln.text)), None)
            body = [ln.text.strip() for ln in lines if stop is None or ln.cy < stop]
            title = next((t for t in body if similar(t, name) >= 0.8), name)
            text = " ".join(t for t in body if t != title)
            text = re.sub(r"Selected Item|Bank Settings", " ", text)  # the panel's tabs
            text = re.sub(r"\s\S{1,2}\s[\d,]+$", "", re.sub(r"\s+", " ", text).strip())  # "[chest] 170": the stack
            text = re.sub(rf"^.{{0,8}}?{re.escape(name)}\s+[\d,]+\s*", "", text).strip()  # "o o Trout 66": icons, title, price
            kind = next((k for k, pattern in self.ITEM_KINDS if re.search(pattern, text, re.I)), None)
            if kind is None and re.search(r"\bSeeds?$", name):
                kind = "seed"
            return {"name": name, "kind": kind or "other", "text": text[:220]}
        finally:
            self._clear_bank_search()

    def confirm_item_opened(self) -> list[tuple[int, str]] | None:
        """Close the "Item Opened" dialog and return what it lists as (pieces, item); None = no dialog.

        The dialog stays until "Confirm" is pressed and dims the page behind it, which made every
        bank action after opening a Bird Nest fail.
        """
        lines = self.read_union(0.30, 0.22, 0.78, 0.78)
        if not find(lines, "Item Opened", 0.8):
            return None
        confirm = find(lines, "Confirm", 0.85)
        items = []
        for ln in lines:
            text = ln.text.strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z' -]{2,}", text) or similar(text, "Item Opened") >= 0.8                     or (confirm and ln is confirm):
                continue
            # The amount stands right above the name, at the same left edge.
            above = [o for o in lines if 0 < ln.cy - o.cy < self.px(30) and abs(o.x - ln.x) < self.px(40)
                     and re.fullmatch(r"[\dOo,]+", o.text.strip())]
            if above:
                digits = re.sub(r"\D", "", max(above, key=lambda o: o.cy).text.replace("O", "0").replace("o", "0"))
                items.append((int(digits or 1), text))
        if confirm:
            self.click(confirm, "confirm the opened items", settle=0.6)
        return items

    def open_items(self, name: str) -> int:
        """Bank: open every `name` (Bird Nest, Bait Box ...). Returns how many were opened."""
        if not self._open_bank():
            return 0
        try:
            panel = self._bank_select(name)
            if not panel:
                return 0
            head, sell = find(panel, "Open Item", 0.8), find(panel, "Sell Item", 0.7)
            if not head or not sell or head.cy >= sell.cy:
                log.info("bank: %s has no Open control (none in the bank?)", name)
                return 0

            def block(lines: list[Line]) -> list[Line]:  # only the Open Item block, never Sell
                return [ln for ln in lines if head.cy + self.px(10) < ln.cy < sell.cy - self.px(20)]

            every = [ln for ln in block(panel) if re.search(r"\bAll\s*$", ln.text)]
            if not every:
                log.warning("bank: no All button in the Open block of %s", name)
                return 0
            ln = min(every, key=lambda ln: ln.cy)  # "All but 1 | All": the right end is All
            self.click(Line("All", ln.x + ln.w - self.px(26), ln.y, self.px(22), ln.h), f"open all {name}", settle=0.5)
            rows = block(self.read_union(0.72, 0.06, 1.0, 0.90))
            hold = [ln for ln in rows if "hold to open" in ln.text.lower()]
            button = next((ln for ln in rows if is_action_button(ln.text, "Open")), None)  # the click-only form
            count = None  # "Open 12 Bird Nests"; OCR reads a zero as "O"
            for ln in rows:
                m = re.search(r"Open\s+([\dOo,]+)\s", ln.text)
                digits = m.group(1).replace("O", "0").replace("o", "0").replace(",", "") if m else ""
                if digits.isdigit():
                    count = int(digits)
            if (not hold and not button) or count == 0:
                log.info("bank: no %s to open", name)
                return 0
            if button and not hold:
                self.hold(button, f"open {name}", 0.3, action=True)
            else:
                ln = hold[0]  # "Open 12 Bird Nests | Hold to Open": the right end is the button
                self.hold(Line("Hold to Open", ln.x + ln.w - self.px(95), ln.y, self.px(80), ln.h), f"open {name}", 2.5,
                          action=True)
            log.info("bank: opened %s %s", count if count is not None else "all", name)
            self.last_opened = []
            if not self.dry_run:  # what came out shows as item notices bottom right, for a few seconds
                time.sleep(1.0)
                # The dialog lists exactly what came out; the notices bottom right also show what
                # the running activity drops, so they are only the fallback.
                shown = self.confirm_item_opened()
                self.last_opened = shown if shown else self.read_notices()
                log.info("bank: %s gave %s", name, ", ".join(f"+{n} {item}" for n, item in self.last_opened) or "nothing read")
            return count or 1
        finally:
            self._clear_bank_search()

    # --- Smithing / smelting -------------------------------------------------------

    def read_smelting(self) -> dict[str, tuple[int, int]]:
        """Open Smithing > Smelting and return {bar name: (have, need)} for unlocked bars."""
        if not self.open_skill("Smithing"):
            return {}
        cat = find(self.read(0.10, 0.10, 0.26, 0.95), "Smelting", 0.85)
        if cat:
            self.click(cat, "smelting category")
        lines = self.read_union(0.25, 0.15, 0.57, 0.95)  # a single pass sometimes drops a bar
        log.debug("smelting lines: %s", [ln.text for ln in lines])
        out = {}
        for name in lines:
            if not re.search(r"bar$", name.text.strip(), re.I):
                continue
            count = self._count_below(lines, name)
            if count:
                out[name.text.strip()] = count
            else:
                log.debug("smelting: no count for %s", name.text)
        return out

    def _count_below(self, lines: list[Line], name: Line) -> tuple[int, int] | None:
        """The "have / need" on a recipe card's item icon, just below the recipe name."""
        count_re = r"([\d,.]+[KkM]?)\s*/\s*(\d+)"  # "2K / 2": thousands shown short
        # First the soft read (see `sentence_read`): the hard-mask reads below turned "93 / 1" into
        # "9331", and the bot then smelted at 93 ore against a rule of 200.
        box = self.img[max(name.y + self.px(40), 0):name.y + self.px(84), max(name.x - self.px(14), 0):name.x + self.px(84)]
        if box.size:
            lo, hi = box.min(axis=2).astype(float), box.max(axis=2).astype(float)
            soft = sentence_read(np.clip((lo - 70) / 150, 0, 1) * (hi - lo < 60)) or ""
            hit = re.search(r"([\d,.]+[KkM]?)\s*[/|]\s*(\d+)", soft.replace("O", "0").replace("o", "0"))
            if hit and parse_amount(hit.group(1)) is not None:
                return parse_amount(hit.group(1)), int(hit.group(2))
        # A cleaned crop of that spot reads small white digits best; the plain OCR line under the
        # name is the fallback.
        # One read is not enough: "509 / 1" came back as "509M" at one scale and right at the
        # next. Take the first read that parses.
        reads = list(self.white_text_reads(name.x - self.px(10), name.y + self.px(48), self.px(90), self.px(30)))
        m = next((hit for hit in (re.search(count_re, text) for text in reads) if hit), None)
        if not m:
            # "35/1" is often read as "35 il" or "35 l1": digits, then junk that stands for "/1".
            for text in reads:
                loose = re.fullmatch(r"\s*([\d,]+)\s*[/|\\]?\s*[iIl1|/!]{1,3}\s*", text)
                if loose:
                    return int(loose.group(1).replace(",", "")), 1
            near = [c for c in lines if re.fullmatch(count_re, c.text.strip())
                    and abs(c.cx - name.cx) < self.px(150) and 0 < c.cy - name.cy < self.px(120)]
            if near:
                m = re.fullmatch(count_re, min(near, key=lambda c: c.cy - name.cy).text.strip())
        return (parse_amount(m.group(1)) or 0, int(m.group(2))) if m else None

    def white_text_reads(self, x: int, y: int, w: int, h: int):
        """Every reading of a small light text: cleaned at two thresholds and three scales, then plain."""
        crop = self.img[max(y, 0):y + h, max(x, 0):x + w]
        if crop.size == 0:
            return
        for threshold in (170, 150):
            white = (crop.min(axis=2) > threshold).astype(np.uint8) * 255
            clean = np.dstack([255 - white] * 3)
            for scale in (2, 4, 3):
                text = " ".join(ln.text for ln in ocr.read(clean, None, self.ocr_scale(scale)))
                if text:
                    yield text
        text = " ".join(ln.text for ln in ocr.read(np.ascontiguousarray(crop), None, self.ocr_scale(3)))
        if text:
            yield text

    def read_white_text(self, x: int, y: int, w: int, h: int) -> str:
        """OCR small white text drawn over artwork: keep near-white pixels, upscale, retry scales."""
        crop = self.img[max(y, 0):y + h, max(x, 0):x + w]
        if crop.size == 0:  # asked for a spot past the edge of the window
            return ""
        white = (crop.min(axis=2) > 170).astype(np.uint8) * 255
        clean = np.dstack([255 - white] * 3)
        for scale in (2, 4, 3):
            text = " ".join(ln.text for ln in ocr.read(clean, None, self.ocr_scale(scale)))
            if text:
                return text
        return ""

    def smelt(self, bar: str) -> bool:
        """Select a bar recipe on the (already open) Smelting tab and press Create."""
        lines = self.read(0.25, 0.15, 0.57, 0.95)
        card = find(lines, bar, 0.85)
        if not card:
            log.warning("smelt: recipe %s not found", bar)
            return False
        self.click(card, f"select {bar}")
        right = self.read(0.57, 0.70, 0.76, 0.98)
        create = find(right, "Create", 0.8)
        if not create:
            log.warning("smelt: Create button not found")
            return False
        self.click(create, f"create {bar}", action=True)
        return True
