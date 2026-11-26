"""Control window for the bot: status light, live info, settings and log.

Run:  .venv\\Scripts\\pythonw -m bot.gui   (or double-click start_gui.cmd)
UI text is English; code and log messages are English too.
"""
import ctypes
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageDraw, ImageFont, ImageTk

from . import autostart, install, update
from .game import Game
from .main import (ROOT, STATE_PATH, SUPPORTED_SKILLS, Bot, Controls, Hotkeys, load_config, minutes_range,
                   save_config)
from .paths import FROZEN, use_root
from .version import VERSION
from .window import GameWindow

log = logging.getLogger("bot")

BG, SURFACE, BORDER = "#f4f4f1", "#ffffff", "#d3d3cc"
TEXT, MUTED = "#1d2125", "#61666e"
GREEN, GREEN_HOVER, GREEN_PRESS = "#1e874b", "#197a43", "#146437"
AMBER, RED, GREY, FOCUS = "#c88a00", "#bf3a2b", "#9a9fa6", "#2f6fde"
HOVER, PRESS = "#ecece6", "#e0e0d8"
FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI Semibold", 10)
FONT_TITLE = ("Segoe UI Semibold", 16)
FONT_SECTION = ("Segoe UI Semibold", 11)
FONT_NOW = ("Segoe UI Semibold", 17)
WARN_TEXT = "#8a5a00"  # amber dark enough to read as text on white

STATES = {  # state -> (light colour, label)
    "stopped": (GREY, "Stopped"),
    "starting": (GREEN, "Starting…"),
    "running": (GREEN, "Running"),
    "paused": (AMBER, "Paused"),
    "yielding": (AMBER, "You are playing"),
    "waiting": (RED, "Waiting for the game"),
    "error": (RED, "Error"),
    "stopping": (GREY, "Stopping after this step…"),
}
PROBLEMS = {  # GameWindowError.code -> explanation
    "not_found": "Melvor Idle 2 not found. The bot starts it through Steam and checks again every 10 s.",
    "minimized": "The game window is minimized. Restore it; behind other windows is fine.",
    "capture_failed": "Could not read the game window. The bot tries again in 10 s and restarts "
                      "the game after 20 min.",
}
LEFT_W = 440  # width of the column with the controls and the status box
KINDS = {  # journal: event kind -> label
    "activity": "Activity", "sold": "Sold", "bought": "Bought", "loot": "Received", "farm": "Farming",
    "open": "Opened", "level": "Level", "smelt": "Smelting", "gear": "Gear", "skip": "Skipped",
    "warn": "Warning", "slayer": "Slayer", "gold": "Gold", "game": "Game", "learn": "Learned",
}
JOURNAL_FILTERS = {  # choice in the journal -> kinds shown (None = all)
    "All": None,
    "Sold, bought and gold": ("sold", "bought", "gold"),
    "Received and opened": ("loot", "open", "farm", "learn"),
    "Activity and levels": ("activity", "level", "smelt", "gear", "slayer", "game"),
    "Skipped and warnings": ("skip", "warn"),
}
NO_TARGET = ("Fishing", "Cooking", "Firemaking", "Thieving", "Combat", "Fletching", "Runecrafting", "Crafting", "Herblore",
             "Hunting")
KNOWN_BARS = ["Copper Bar", "Bronze Bar", "Iron Bar", "Silver Bar", "Steel Bar", "Gold Bar"]


def fmt_secs(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait((record.levelno, self.format(record)))
        except Exception:
            self.handleError(record)


def setup_logging(q: queue.Queue) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    file_handler = logging.FileHandler(ROOT / "logs" / "bot.log", encoding="utf-8")
    queue_handler = QueueLogHandler(q)
    for h in (file_handler, queue_handler):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


# --- clickable widgets: pointer cursor, "no" cursor when disabled --------------------

def button(parent, text: str, command, style: str = "TButton", **kw) -> ttk.Button:
    return ttk.Button(parent, text=text, command=command, style=style, cursor="hand2", **kw)


def set_enabled(widget, enabled: bool, readonly: bool = False) -> None:
    if enabled:
        widget.state(["!disabled"] + (["readonly"] if readonly else []))
    else:
        widget.state(["disabled"])
    if not isinstance(widget, (ttk.Entry, ttk.Spinbox)) or isinstance(widget, ttk.Combobox):
        widget.configure(cursor="hand2" if enabled else "no")


class ScrollFrame(ttk.Frame):
    """Vertically scrollable area; put content in `.inner`."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0, borderwidth=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, cursor="hand2")
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._fit)

    def _fit(self, event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)
        self._sync()

    def _sync(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.inner.winfo_reqheight() > self.canvas.winfo_height() + 2:
            self.bar.grid()
        else:  # everything fits: no bar, and the view back at the top
            self.bar.grid_remove()
            self.canvas.yview_moveto(0)

    def contains(self, widget) -> bool:
        return str(widget).startswith(str(self))

    def wheel(self, event) -> None:
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.logq: queue.Queue = queue.Queue()
        setup_logging(self.logq)
        self.ctl = Controls()
        Hotkeys(self.ctl).start()
        self.bot: Bot | None = None
        self.thread: threading.Thread | None = None
        self.cfg = load_config()
        self.updated_from = ""  # set by main() after a self-update; noted in the journal at start
        self.pending_update: tuple[str, object, bool] | None = None  # (version, exe, restart the bot)
        self.updater = update.Updater(lambda: self.cfg)
        self.updater.start()
        self.dry_run = tk.BooleanVar(value=False)
        self.saved_note_until = 0.0
        self.scrolls: list[ScrollFrame] = []
        try:  # the journal of earlier runs, shown until the bot is started
            self.saved_events = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("events", [])
        except (OSError, ValueError):
            self.saved_events = []

        self._style()
        self._build()
        self._load_settings(self.cfg)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()

    # --- look ------------------------------------------------------------------------

    def _style(self) -> None:
        self.root.configure(background=BG)
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=FONT, bordercolor=BORDER,
                    focuscolor=FOCUS, troughcolor=BG, lightcolor=SURFACE, darkcolor=BORDER)
        s.configure("Card.TFrame", background=SURFACE, relief="solid", borderwidth=1)
        s.configure("Card.TLabel", background=SURFACE)
        s.configure("Key.TLabel", background=SURFACE, foreground=MUTED)
        s.configure("CardNote.TLabel", background=SURFACE, foreground=MUTED, font=FONT_SMALL)
        s.configure("Value.TLabel", background=SURFACE, font=FONT_BOLD)
        s.configure("Muted.TLabel", foreground=MUTED, font=FONT_SMALL)
        s.configure("Title.TLabel", font=FONT_TITLE)
        s.configure("Section.TLabel", font=FONT_SECTION)
        s.configure("Panel.TFrame", background=SURFACE)
        s.configure("Now.TLabel", background=SURFACE, font=FONT_NOW)
        s.configure("TSeparator", background=BORDER)
        s.configure("Now.Horizontal.TProgressbar", troughcolor="#e6e6df", background=GREEN, bordercolor="#e6e6df",
                    lightcolor=GREEN, darkcolor=GREEN, thickness=10)

        # Buttons: always a visible border or fill (no ghost buttons), hover + focus ring.
        s.configure("TButton", padding=(14, 8), background=SURFACE, bordercolor=BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE, focusthickness=2, focuscolor=FOCUS)
        s.map("TButton",
              background=[("disabled", BG), ("pressed", PRESS), ("active", HOVER)],
              lightcolor=[("disabled", BG), ("pressed", PRESS), ("active", HOVER)],
              darkcolor=[("disabled", BG), ("pressed", PRESS), ("active", HOVER)],
              foreground=[("disabled", GREY)],
              bordercolor=[("disabled", "#e2e2dc"), ("focus", FOCUS), ("active", "#b9b9b1")])
        s.configure("Big.TButton", padding=(18, 11), font=FONT_BOLD)
        s.configure("Primary.TButton", padding=(18, 11), font=FONT_BOLD, background=GREEN,
                    foreground="#ffffff", bordercolor=GREEN, lightcolor=GREEN, darkcolor=GREEN)
        s.map("Primary.TButton",
              background=[("disabled", "#b9d3c3"), ("pressed", GREEN_PRESS), ("active", GREEN_HOVER)],
              lightcolor=[("disabled", "#b9d3c3"), ("pressed", GREEN_PRESS), ("active", GREEN_HOVER)],
              darkcolor=[("disabled", "#b9d3c3"), ("pressed", GREEN_PRESS), ("active", GREEN_HOVER)],
              foreground=[("disabled", "#f4f4f1")],
              bordercolor=[("disabled", "#b9d3c3"), ("focus", FOCUS)])
        s.configure("Danger.TButton", padding=(18, 11), font=FONT_BOLD, foreground=RED, bordercolor=RED)
        s.map("Danger.TButton",
              background=[("disabled", BG), ("pressed", "#f6dcd8"), ("active", "#fbeae7")],
              lightcolor=[("disabled", BG), ("pressed", "#f6dcd8"), ("active", "#fbeae7")],
              darkcolor=[("disabled", BG), ("pressed", "#f6dcd8"), ("active", "#fbeae7")],
              foreground=[("disabled", GREY)],
              bordercolor=[("disabled", "#e2e2dc"), ("focus", FOCUS)])
        s.configure("Small.TButton", padding=(10, 5))
        s.configure("Icon.TButton", padding=(9, 8))

        self._images = self._draw_images()  # kept on self: Tk drops an image nobody refers to
        img = self._images
        s.element_create("Check.box", "image", img["box"],
                         ("disabled", "selected", img["box_on_off"]), ("disabled", img["box_off"]),
                         ("selected", img["box_on"]), ("active", img["box_hover"]), sticky="w")
        s.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Check.box", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        s.configure("TCheckbutton", background=BG, focuscolor=FOCUS, padding=(0, 3))
        s.map("TCheckbutton", background=[("active", BG)], foreground=[("disabled", GREY)])
        s.configure("Card.TCheckbutton", background=SURFACE)
        s.map("Card.TCheckbutton", background=[("active", SURFACE)])
        # Tabs: flat, the selected one carries a green line. Same font for every state, so the
        # row never shifts when another tab is picked.
        s.element_create("Flat.tab", "image", img["tab"], ("selected", img["tab_on"]), ("active", img["tab_hover"]),
                         border=(2, 2, 2, 4), sticky="nswe")
        s.layout("TNotebook.Tab", [("Flat.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.focus", {"side": "top", "sticky": "nswe", "children": [
                    ("Notebook.label", {"side": "top", "sticky": ""})]})]})]})])
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", padding=(13, 9, 13, 10), background=BG, foreground=MUTED, focuscolor=FOCUS,
                    font=FONT_BOLD)
        s.map("TNotebook.Tab", foreground=[("selected", TEXT), ("active", TEXT)], expand=[("selected", (0, 0, 0, 0))])
        s.configure("TCombobox", padding=4, arrowsize=14)
        s.map("TCombobox", fieldbackground=[("readonly", SURFACE)], background=[("active", HOVER)],
              bordercolor=[("focus", FOCUS), ("active", "#b9b9b1")])
        s.configure("TSpinbox", padding=4, arrowsize=12)
        s.map("TSpinbox", bordercolor=[("focus", FOCUS)], background=[("active", HOVER)])
        s.configure("TEntry", padding=4)
        s.map("TEntry", bordercolor=[("focus", FOCUS)])
        s.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, rowheight=28, borderwidth=0)
        s.configure("Treeview.Heading", font=FONT_BOLD, background=SURFACE, foreground=MUTED, relief="flat",
                    padding=(6, 6))
        s.map("Treeview.Heading", background=[("active", SURFACE)])

    @staticmethod
    def _draw_images() -> dict[str, ImageTk.PhotoImage]:
        """Checkbox and tab pictures in the window's own colours, drawn large and scaled down."""
        def box(fill: str, edge: str, tick: str | None) -> ImageTk.PhotoImage:
            size, k = 18, 4
            im = Image.new("RGBA", ((size + 8) * k, size * k), (0, 0, 0, 0))  # 8 px of air before the label
            d = ImageDraw.Draw(im)
            d.rounded_rectangle((k, k, (size - 1) * k, (size - 1) * k), radius=4 * k, fill=fill, outline=edge, width=int(1.5 * k))
            if tick:
                d.line([(4.6 * k, 9.4 * k), (7.9 * k, 12.6 * k), (13.6 * k, 5.8 * k)], fill=tick, width=int(2.2 * k), joint="curve")
            return ImageTk.PhotoImage(im.resize((size + 8, size), Image.LANCZOS))

        def tab(fill: str, line: str, thick: int) -> ImageTk.PhotoImage:
            im = Image.new("RGB", (12, 40), fill)
            ImageDraw.Draw(im).rectangle((0, 40 - thick, 12, 40), fill=line)
            return ImageTk.PhotoImage(im)

        def arrow(up: bool, colour: str) -> ImageTk.PhotoImage:
            k = 4
            im = Image.new("RGBA", (14 * k, 14 * k), (0, 0, 0, 0))
            tip, base = (4.5, 9.5) if up else (9.5, 4.5)
            ImageDraw.Draw(im).line([(3 * k, base * k), (7 * k, tip * k), (11 * k, base * k)], fill=colour,
                                    width=int(1.8 * k), joint="curve")
            return ImageTk.PhotoImage(im.resize((14, 14), Image.LANCZOS))

        return {
            "up": arrow(True, TEXT), "up_off": arrow(True, "#c4c6c1"),
            "down": arrow(False, TEXT), "down_off": arrow(False, "#c4c6c1"),
            "box": box(SURFACE, "#a9aca6", None), "box_hover": box(SURFACE, GREEN, None),
            "box_on": box(GREEN, GREEN, "#ffffff"), "box_off": box(BG, "#d9d9d3", None),
            "box_on_off": box("#b9d3c3", "#b9d3c3", "#ffffff"),
            "tab": tab(BG, BORDER, 1), "tab_hover": tab(HOVER, "#b9b9b1", 1), "tab_on": tab(BG, GREEN, 3),
        }

    def _icon(self) -> None:
        """Own window and taskbar icon instead of the Tk feather / Python logo."""
        self._icon_image = ImageTk.PhotoImage(autostart.icon_image(64))
        self.root.iconphoto(True, self._icon_image)

    # --- layout ----------------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.title("Melvor Bot")
        self._icon()
        root.minsize(1280, 780)  # narrower than this and the settings tabs and fields would fall off the right edge
        width = min(1340, root.winfo_screenwidth() - 80)
        height = min(900, root.winfo_screenheight() - 120)
        root.geometry(f"{width}x{height}")

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)
        # Left: what is going on and the controls. Right: everything to read or set, at full height.
        left = ttk.Frame(outer, width=LEFT_W)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 16))
        left.pack_propagate(False)
        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")

        # Status header: light + state + one-line detail.
        head = ttk.Frame(left)
        head.pack(fill="x")
        self.light = tk.Canvas(head, width=22, height=22, highlightthickness=0, background=BG)
        self.light_dot = self.light.create_oval(2, 2, 20, 20, fill=GREY, outline="")
        self.light.grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky="n", pady=(6, 0))
        self.state_label = ttk.Label(head, text="Stopped", style="Title.TLabel")
        self.state_label.grid(row=0, column=1, sticky="w")
        self.detail_label = ttk.Label(head, text="Press Start.", style="Muted.TLabel", wraplength=LEFT_W - 50,
                                      justify="left")
        self.detail_label.grid(row=1, column=1, sticky="w")
        head.columnconfigure(1, weight=1)

        # Controls.
        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(14, 4))
        self.btn_start = button(bar, "Start", self.start, "Primary.TButton")
        self.btn_pause = button(bar, "Pause", self.pause, "Big.TButton")
        self.btn_stop = button(bar, "Stop", self.stop, "Danger.TButton")
        for i, b in enumerate((self.btn_start, self.btn_pause, self.btn_stop)):
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            bar.columnconfigure(i, weight=1, uniform="btn")
        self.chk_dry = ttk.Checkbutton(left, text="Dry run: look and navigate, start no actions",
                                       variable=self.dry_run, cursor="hand2")
        self.chk_dry.pack(anchor="w", pady=(6, 0))
        ttk.Label(left, text="Hotkeys: Ctrl+Alt+P pause, Ctrl+Alt+Q stop", style="Muted.TLabel").pack(
            anchor="w", pady=(2, 10))

        self._build_status(left)

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self._hand_on_tabs(self.tabs)
        self._build_journal()
        self._build_shop()
        self._build_overview()
        self._build_settings()
        self._build_log()

    def _hand_on_tabs(self, book: ttk.Notebook) -> None:
        """Pointer cursor over the tabs of `book` (a notebook has no cursor option per tab)."""
        def move(event) -> None:
            try:
                over = book.identify(event.x, event.y) != "" and book.index(f"@{event.x},{event.y}") is not None
            except tk.TclError:
                over = False
            book.configure(cursor="hand2" if over else "")

        book.bind("<Motion>", move)
        book.bind("<Leave>", lambda e: book.configure(cursor=""))



    def _wheel(self, event) -> None:
        for area in self.scrolls:
            if area.contains(event.widget):
                area.wheel(event)
                return

    def _card(self, parent) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=14)
        card.pack(fill="x", pady=(0, 10))
        return card

    def _build_status(self, parent) -> None:
        """The box that says what is going on right now. Always visible, under the controls."""
        # A coloured frame around a white panel: the border takes the colour of the state.
        self.status_box = tk.Frame(parent, background=GREY, padx=2, pady=2)
        self.status_box.pack(fill="x")
        box = ttk.Frame(self.status_box, style="Panel.TFrame", padding=(18, 14))
        box.pack(fill="both", expand=True)
        wrap = LEFT_W - 46

        top = ttk.Frame(box, style="Panel.TFrame")
        top.pack(fill="x")
        head = ttk.Frame(top, style="Panel.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="Now doing", style="Key.TLabel").pack(side="left", anchor="s")
        self.btn_skip = button(head, "Next activity", self.skip, "Small.TButton")
        self.btn_skip.pack(side="right")
        self.now_label = ttk.Label(top, text="Not started yet", style="Now.TLabel", wraplength=wrap, justify="left")
        self.now_label.pack(anchor="w", pady=(0, 6))
        self.progress = ttk.Progressbar(top, style="Now.Horizontal.TProgressbar", maximum=1000, mode="determinate")
        self.progress.pack(fill="x")
        self.next_label = ttk.Label(top, text="", style="Card.TLabel", wraplength=wrap, justify="left")
        self.next_label.pack(anchor="w", pady=(6, 0))
        self.last_line = ttk.Label(top, text="", style="Key.TLabel", wraplength=wrap, justify="left")
        self.last_line.pack(anchor="w", pady=(2, 0))

        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=10)

        facts = ttk.Frame(box, style="Panel.TFrame")
        facts.pack(fill="x")
        self.info = {}
        rows = [("activity", "Rotation"), ("bank", "Bank"), ("smelt", "Smelting"), ("farm", "Farming"),
                ("health", "HP"), ("equipped", "Food equipped"),  # of the last page read: Thieving or Combat
                ("food", "Food stock"), ("cooking", "Waiting on Cooking"), ("fire", "Firemaking"),
                ("ranch", "Ranching"), ("gear", "Gear"), ("slayer", "Slayer task"), ("gold", "Gold")]
        for r, (key, label) in enumerate(rows):
            ttk.Label(facts, text=label, style="Key.TLabel").grid(row=r, column=0, sticky="nw", pady=3, padx=(0, 12))
            val = ttk.Label(facts, text="—", style="Value.TLabel", wraplength=wrap - 134, justify="left")
            val.grid(row=r, column=1, sticky="nw", pady=3)
            self.info[key] = val
        facts.columnconfigure(0, minsize=118)
        facts.columnconfigure(1, weight=1)

    def _build_journal(self) -> None:
        """What the bot did, sold, bought and received, newest first."""
        tab = ttk.Frame(self.tabs, padding=(0, 14, 0, 0))
        self.tabs.add(tab, text="Journal")
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(2, 2))
        ttk.Label(top, text="What the bot did", style="Section.TLabel").pack(side="left")
        self.v_journal = tk.StringVar(value="All")
        pick = ttk.Combobox(top, textvariable=self.v_journal, values=list(JOURNAL_FILTERS), state="readonly",
                            width=24, cursor="hand2")
        pick.pack(side="right")
        pick.bind("<<ComboboxSelected>>", lambda e: self._render_journal())
        ttk.Label(top, text="Show", style="Muted.TLabel").pack(side="right", padx=(0, 8))
        self.journal_sum = ttk.Label(tab, text="", style="Muted.TLabel", justify="left")
        self.journal_sum.pack(anchor="w", pady=(0, 8))
        box = ttk.Frame(tab, style="Card.TFrame", padding=1)
        box.pack(fill="both", expand=True)
        # A text widget instead of a table: long lines wrap under their own text, and only the
        # kind of a line carries colour.
        self.journal = tk.Text(box, wrap="word", font=FONT, relief="flat", borderwidth=0, highlightthickness=0,
                               background=SURFACE, foreground=TEXT, state="disabled", cursor="arrow",
                               padx=16, pady=10, spacing1=4, spacing3=4, tabs=(62, 176))
        self.journal.tag_configure("row", lmargin2=176)
        self.journal.tag_configure("time", foreground=MUTED)
        self.journal.tag_configure("kind", font=FONT_BOLD)
        self.journal.tag_configure("good", foreground=GREEN)
        self.journal.tag_configure("warn", foreground=WARN_TEXT)
        self.journal.tag_configure("quiet", foreground=MUTED)
        self.journal.tag_configure("day", font=FONT_BOLD, foreground=MUTED, spacing1=12, spacing3=6)
        bar = ttk.Scrollbar(box, orient="vertical", command=self.journal.yview, cursor="hand2")
        self.journal.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.journal.pack(fill="both", expand=True)
        self._journal_key = None

    def _render_journal(self) -> None:
        events = self.bot.events if self.bot else self.saved_events
        key = (len(events), events[-1]["t"] if events else 0, self.v_journal.get(), time.strftime("%d"))
        if key == self._journal_key:
            return
        self._journal_key = key
        kinds = JOURNAL_FILTERS.get(self.v_journal.get())
        days = {time.strftime("%d-%m"): "Today", time.strftime("%d-%m", time.localtime(time.time() - 86400)): "Yesterday"}
        view = self.journal.yview()[0]
        self.journal.configure(state="normal")
        self.journal.delete("1.0", "end")
        shown_day = None
        for e in reversed(events):
            if kinds and e["kind"] not in kinds:
                continue
            when = time.localtime(e["t"])
            day = time.strftime("%d-%m", when)
            if day != shown_day:
                shown_day = day
                self.journal.insert("end", days.get(day, day) + "\n", ("day",))
            colour = ("good" if e["kind"] in ("sold", "bought", "loot", "level", "gold") else
                      "warn" if e["kind"] == "warn" else "quiet" if e["kind"] == "skip" else "")
            self.journal.insert("end", time.strftime("%H:%M", when), ("row", "time"))
            self.journal.insert("end", "\t" + KINDS.get(e["kind"], e["kind"]), ("row", "kind") + ((colour,) if colour else ()))
            self.journal.insert("end", "\t" + e["text"] + "\n", ("row", "quiet") if colour == "quiet" else ("row",))
        self.journal.configure(state="disabled")
        if view > 0:  # the reader had scrolled down: stay there
            self.journal.yview_moveto(view)
        recent = [e for e in events if e["t"] >= time.time() - 86400]
        sold = sum(e.get("gp", 0) for e in recent if e["kind"] == "sold")
        bought = [e for e in recent if e["kind"] == "bought"]
        levels = sum(1 for e in recent if e["kind"] == "level")
        parts = [f"sold for {sold:,} GP",
                 f"{len(bought)} bought for {sum(e.get('gp', 0) for e in bought):,} GP",
                 f"{levels} levels gained"]
        self.journal_sum.configure(text="Last 24 hours: " + ", ".join(parts) if events else
                                   "Nothing yet. Once the bot runs, what it does, sells, buys and receives shows up here.")

    def _build_overview(self) -> None:
        tab = ttk.Frame(self.tabs, padding=(0, 12, 0, 0))
        self.tabs.add(tab, text="Ores")
        ttk.Label(tab, text="Ores for bars (last read)", style="Section.TLabel").pack(anchor="w", pady=(4, 6))
        box = ttk.Frame(tab, style="Card.TFrame", padding=1)
        box.pack(fill="both", expand=True)
        self.ore_tree = ttk.Treeview(box, columns=("bar", "ore", "rule"), show="headings", height=4, selectmode="none")
        for col, text, width in (("bar", "Bar", 170), ("ore", "Ore in bank", 120), ("rule", "Smelt from", 120)):
            self.ore_tree.heading(col, text=text, anchor="w")
            self.ore_tree.column(col, width=width, anchor="w")
        self.ore_tree.tag_configure("ready", foreground=GREEN)
        self.ore_tree.pack(fill="both", expand=True)

    def _build_shop(self) -> None:
        tab = ttk.Frame(self.tabs, padding=(0, 12, 0, 0))
        self.tabs.add(tab, text="Shop")
        ttk.Label(tab, text="Upgrades in the Shop", style="Section.TLabel").pack(anchor="w", pady=(4, 2))
        ttk.Label(tab, text="Green is within reach. Set what the bot buys itself under Settings, in Bank and buying.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        box = ttk.Frame(tab, style="Card.TFrame", padding=1)
        box.pack(fill="both", expand=True)
        self.shop_tree = ttk.Treeview(box, columns=("cat", "name", "need"), show="headings", height=8, selectmode="none")
        for col, text, width in (("cat", "Category", 110), ("name", "Upgrade", 190), ("need", "Still missing (estimate)", 330)):
            self.shop_tree.heading(col, text=text, anchor="w")
            self.shop_tree.column(col, width=width, anchor="w")
        self.shop_tree.tag_configure("ready", foreground=GREEN)
        bar = ttk.Scrollbar(box, orient="vertical", command=self.shop_tree.yview, cursor="hand2")
        self.shop_tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.shop_tree.pack(fill="both", expand=True)

    def _build_settings(self) -> None:
        outer = ttk.Frame(self.tabs, padding=(0, 12, 0, 0))
        self.tabs.add(outer, text="Settings")
        foot = ttk.Frame(outer)
        foot.pack(fill="x", side="bottom", pady=(8, 0))
        button(foot, "Save", self.save_settings, "Primary.TButton").pack(side="left")
        button(foot, "Reset", lambda: self._load_settings(load_config()), "Big.TButton").pack(side="left", padx=(8, 0))
        self.saved_label = ttk.Label(foot, text="", style="Muted.TLabel")
        self.saved_label.pack(side="left", padx=(12, 0))
        # One sub-tab per subject, so a page fits without scrolling on a normal window.
        self.set_tabs = ttk.Notebook(outer)
        self.set_tabs.pack(fill="both", expand=True)
        self._hand_on_tabs(self.set_tabs)
        # Wheel scrolls the page; it must not silently change spinbox/combobox values.
        for cls in ("TSpinbox", "TCombobox"):
            self.root.unbind_class(cls, "<MouseWheel>")
        self.root.bind_all("<MouseWheel>", self._wheel)

        def page(title: str) -> ttk.Frame:
            area = ScrollFrame(self.set_tabs)
            self.scrolls.append(area)
            self.set_tabs.add(area, text=title)
            area.inner.configure(padding=(2, 12, 2, 4))
            return area.inner

        def group(parent, title: str, note: str = "") -> ttk.Frame:
            ttk.Label(parent, text=title, style="Section.TLabel").pack(anchor="w", pady=(2, 2 if note else 4))
            if note:
                ttk.Label(parent, text=note, style="Muted.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=(0, 4))
            card = self._card(parent)
            card.columnconfigure(0, minsize=400)
            return card

        def spin(card, row: int, label: str, var, lo: int, hi: int, inc: int = 1) -> None:
            ttk.Label(card, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=3, padx=(0, 12))
            ttk.Spinbox(card, from_=lo, to=hi, increment=inc, textvariable=var, width=9).grid(
                row=row, column=1, sticky="w", pady=3)

        def text(card, row: int, label: str, var, width: int = 30) -> None:
            ttk.Label(card, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=3, padx=(0, 12))
            ttk.Entry(card, textvariable=var, width=width).grid(row=row, column=1, sticky="w", pady=3)

        def check(card, row: int, label: str, var) -> None:
            ttk.Checkbutton(card, text=label, variable=var, cursor="hand2", style="Card.TCheckbutton").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=3)

        def note(card, row: int, label: str) -> None:
            ttk.Label(card, text=label, style="CardNote.TLabel", wraplength=660, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(0, 6), padx=(24, 0))

        # --- Rotation ---
        tab = page("Rotation")
        ttk.Label(tab, text="Rotation", style="Section.TLabel").pack(anchor="w", pady=(2, 2))
        ttk.Label(tab, text="Untick to skip a step. Target: 'best' = highest unlocked, 'best Tree' or 'best Bush' per "
                            "kind, 'least' = least explored, 'random', 'smeltable' (Mining: the ore for a bar Smithing can make, least in stock first), a name, or 'level 5' (Fletching etc.: train "
                            "until that level). Each time a random duration between the two numbers; 0 = stay.",
                  style="Muted.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=(0, 8))
        self.rot_frame = ttk.Frame(tab)
        self.rot_frame.pack(fill="x")
        button(tab, "+ Activity", self._add_rotation, "Small.TButton").pack(anchor="w", pady=(6, 10))

        # --- General ---
        tab = page("General")
        gen = group(tab, "General")
        self.v_tick = tk.StringVar()
        self.v_reselect = tk.StringVar()
        self.v_start_current = tk.BooleanVar(value=True)
        self.v_restore = tk.BooleanVar(value=True)
        self.v_yield = tk.BooleanVar(value=True)
        self.v_yield_secs = tk.StringVar()
        spin(gen, 0, "Look every (seconds)", self.v_tick, 2, 60)
        spin(gen, 1, "Look for new unlocks every (min)", self.v_reselect, 1, 240)
        check(gen, 2, "On a first start, begin with what the game is already doing if it is in the rotation", self.v_start_current)
        note(gen, 3, "After a restart the bot carries on at the step it was at, with the time that was left.")
        check(gen, 4, "Put a minimized game window back, behind your other windows", self.v_restore)
        note(gen, 5, "A minimized window cannot be read. During a fight the bot always puts it back.")
        self.v_blur_secs = tk.StringVar()
        check(gen, 6, "Stand back while I use the game myself (during a fight the bot keeps eating)", self.v_yield)
        spin(gen, 7, "In the game: carry on after this many seconds without a click or key", self.v_yield_secs, 3, 600)
        spin(gen, 8, "After I switch to another window: carry on after (seconds)", self.v_blur_secs, 0, 120)
        self.v_scale = tk.BooleanVar(value=True)
        self.v_min_turn = tk.StringVar()
        check(gen, 9, "Spend a bit more time on low skills and a bit less on high ones", self.v_scale)
        note(gen, 10, "Turns of a low skill last up to 1.6 times as long, those of a high skill down to 0.6 times, "
                      "compared with the middle level of the rotation.")
        spin(gen, 11, "Shortest turn (min)", self.v_min_turn, 1, 60)
        self.v_real = tk.BooleanVar(value=True)
        self.v_real_idle = tk.StringVar()
        check(gen, 12, "Use the real mouse while I am away", self.v_real)
        note(gen, 13, "Only when the PC has had no input for a while, the screen is not locked and nothing is "
                      "playing sound. Needed for menus that ignore the bot's normal clicks (crop choice).")
        spin(gen, 14, "Away for at least (min)", self.v_real_idle, 1, 120)
        self.v_autostart = tk.BooleanVar(value=False)
        check(gen, 15, "Start the bot when I sign in to Windows", self.v_autostart)
        note(gen, 16, "A shortcut in your Startup folder. The bot then starts the game through Steam and loads "
                      "your character when there is only one.")
        self.v_self_update = tk.BooleanVar(value=True)
        check(gen, 17, "Update automatically from GitHub releases", self.v_self_update)
        self.update_note = ttk.Label(gen, text="", style="CardNote.TLabel", wraplength=660, justify="left")
        self.update_note.grid(row=18, column=0, columnspan=2, sticky="w", pady=(0, 6), padx=(24, 0))

        # --- Fishing and cooking ---
        tab = page("Fishing and cooking")
        fish = group(tab, "Fishing")
        self.v_auto_spot = tk.BooleanVar(value=True)
        self.v_use_bait = tk.BooleanVar(value=False)
        self.v_bait_spot = tk.StringVar()
        self.v_variety = tk.StringVar()
        self.v_backlog = tk.StringVar()
        check(fish, 0, "Choose the spot automatically", self.v_auto_spot)
        note(fish, 1, "Mostly the highest xp, less often where all rares are already collected.")
        check(fish, 2, "Watch the bait", self.v_use_bait)
        note(fish, 3, "If the chosen bait runs out, first fish bait boxes at the spot below.")
        text(fish, 4, "Fish bait boxes at spot", self.v_bait_spot, 20)
        spin(fish, 5, "Variety: chance of another spot (%)", self.v_variety, 0, 100, 5)
        spin(fish, 6, "Fish Cooking cannot cook yet: enough from (pieces)", self.v_backlog, 0, 100000, 50)
        cook = group(tab, "Cooking and Firemaking")
        self.v_min_raw = tk.StringVar()
        self.v_fire_keep = tk.StringVar()
        spin(cook, 0, "Cooking: cook a recipe from this much raw", self.v_min_raw, 1, 1000)
        spin(cook, 1, "Firemaking: keep of each log type (pieces)", self.v_fire_keep, 0, 100000, 50)

        # --- Combat and thieving ---
        tab = page("Combat and thieving")
        fight = group(tab, "Combat", "Slayer task: fight the monster of the current task and move on when a new "
                                     "task comes. Fixed monster: always the area and monster below, which are also "
                                     "the fallback when no task can be read.")
        self.v_area = tk.StringVar()
        self.v_monster = tk.StringVar()
        self.v_stance = tk.StringVar()
        self.v_mode = tk.StringVar()
        self.v_fight_food = tk.StringVar()
        self.v_fight_eat = tk.StringVar()
        text(fight, 0, "Area", self.v_area, 26)
        text(fight, 1, "Monster", self.v_monster, 26)
        ttk.Label(fight, text="Stance (balance = train the lowest of Attack, Strength, Defence; keep = leave it)",
                  style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=3, padx=(0, 12))
        ttk.Combobox(fight, textvariable=self.v_stance, width=12, cursor="hand2", state="readonly",
                     values=["balance", "keep", "Stab", "Slash", "Block", "Crush"]).grid(row=2, column=1, sticky="w", pady=3)
        spin(fight, 3, "Only with at least this much food equipped", self.v_fight_food, 1, 1000)
        spin(fight, 4, "Eat when HP below (%)", self.v_fight_eat, 10, 95)
        ttk.Label(fight, text="What to fight", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=3, padx=(0, 12))
        ttk.Combobox(fight, textvariable=self.v_mode, width=16, cursor="hand2", state="readonly",
                     values=["Slayer task", "Fixed monster"]).grid(row=5, column=1, sticky="w", pady=3)
        note(fight, 6, "During a fight the bot stays on the Combat page: farm, bank and smelting checks wait. It eats "
                       "earlier against a monster that hits hard and runs when HP stays under a quarter after eating.")
        self.v_armour = tk.BooleanVar(value=True)
        check(fight, 7, "Keep the best normal armour set on", self.v_armour)
        note(fight, 8, "When Defence reaches a new tier, and every 6 hours: put on the best normal set (Stone, Copper, "
                       "Iron ...), smith a missing piece once, and sell older normal sets. Armour with bonuses from "
                       "other sources is never sold.")
        thief = group(tab, "Thieving")
        self.v_min_food = tk.StringVar()
        self.v_eat_below = tk.StringVar()
        self.v_min_success = tk.StringVar()
        spin(thief, 0, "Only with at least this much food equipped", self.v_min_food, 1, 1000)
        spin(thief, 1, "Eat when HP below (%)", self.v_eat_below, 10, 95)
        spin(thief, 2, "Pick a target with success chance from (%)", self.v_min_success, 30, 100)

        # --- Bank and buying ---
        tab = page("Bank and buying")
        bank = group(tab, "Bank")
        self.v_bank_free = tk.StringVar()
        self.v_block_free = tk.StringVar()
        self.v_open_names = tk.StringVar()
        self.v_open_every = tk.StringVar()
        self.v_gear = tk.BooleanVar(value=True)
        spin(bank, 0, "Warn at this many free bank slots or fewer", self.v_bank_free, 0, 50)
        spin(bank, 1, "Fetch nothing new at this many free slots or fewer", self.v_block_free, 0, 50)
        text(bank, 2, "Open in the bank (names, comma separated)", self.v_open_names)
        spin(bank, 3, "Open every (min)", self.v_open_every, 5, 1440, 15)
        check(bank, 4, "Equip skill gear with a boost automatically", self.v_gear)
        note(bank, 5, "Only Toolbelt items from the bank, and only in an empty slot of that skill.")
        buy = group(tab, "Buying in the Shop")
        self.v_buy_bank = tk.BooleanVar(value=True)
        self.v_buy_upgrades = tk.BooleanVar(value=True)
        self.v_keep_gold = tk.StringVar()
        check(buy, 0, "Buy bank space automatically once level and gold allow", self.v_buy_bank)
        check(buy, 1, "Buy other upgrades automatically (after bank space, cheapest first)", self.v_buy_upgrades)
        spin(buy, 2, "Always keep at least this much gold", self.v_keep_gold, 0, 10000000, 1000)

        # --- Selling ---
        tab = page("Selling")
        sell = group(tab, "Selling")
        self.v_sell = tk.BooleanVar(value=True)
        self.v_keep_logs = tk.StringVar()
        self.v_keep_ores = tk.StringVar()
        self.v_keep_bars = tk.StringVar()
        self.v_keep_food = tk.StringVar()
        self.v_sell_gap = tk.StringVar()
        self.v_dupes = tk.StringVar()
        check(sell, 0, "Sell surplus when only gold is missing for an upgrade", self.v_sell)
        note(sell, 1, "Only your own logs, ores, bars and cheap food. The best food, what you carry and loot stay "
                      "put. At most one selling round per Shop check.")
        spin(sell, 2, "Keep of each log type", self.v_keep_logs, 0, 1000000, 50)
        spin(sell, 3, "Keep of each ore", self.v_keep_ores, 0, 1000000, 50)
        spin(sell, 4, "Keep of each bar", self.v_keep_bars, 0, 1000000, 50)
        spin(sell, 5, "Keep of each food", self.v_keep_food, 0, 1000000, 50)
        spin(sell, 6, "Sell only for a shortfall of up to (gold)", self.v_sell_gap, 0, 1000000, 1000)
        text(sell, 7, "Duplicate loot where 1 is enough (names, comma separated)", self.v_dupes)
        note(sell, 8, "One piece stays: the one you wear, or else one in the bank. The rest is sold about once an "
                      "hour. Exact item names.")

        # --- Smelting and farming ---
        tab = page("Smelting and farming")
        smelt = group(tab, "Smelting", "Smelt a bar as soon as there is this much ore.")
        self.v_smelt_every = tk.StringVar()
        spin(smelt, 0, "Check smelting every (min)", self.v_smelt_every, 1, 240)
        self.rule_frame = ttk.Frame(tab)
        self.rule_frame.pack(fill="x")
        button(tab, "+ Rule", self._add_rule, "Small.TButton").pack(anchor="w", pady=(6, 12))
        farm = group(tab, "Farming")
        self.v_farm = tk.BooleanVar(value=True)
        self.v_farm_every = tk.StringVar()
        check(farm, 0, "Harvest, replant and water; skip plots without seeds", self.v_farm)
        spin(farm, 1, "Also look every (min), for watering and plot prices", self.v_farm_every, 1, 480)
        note(farm, 2, "Harvesting starts as soon as the red ! shows at Farming in the sidebar.")

        self.rot_rows: list[dict] = []
        self.rule_rows: list[dict] = []

    def _build_log(self) -> None:
        tab = ttk.Frame(self.tabs, padding=(0, 12, 0, 0))
        self.tabs.add(tab, text="Technical log")
        self.log_text = ScrolledText(tab, height=10, wrap="word", font=("Consolas", 9), relief="solid",
                                     borderwidth=1, background=SURFACE, foreground=TEXT, state="disabled")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("warn", foreground="#9a6700")
        self.log_text.tag_configure("err", foreground=RED)
        foot = ttk.Frame(tab)
        foot.pack(fill="x", pady=(8, 0))
        button(foot, "Open log file", self.open_log, "Small.TButton").pack(side="left")
        button(foot, "Clear window", self.clear_log, "Small.TButton").pack(side="left", padx=(8, 0))

    # --- settings rows ---------------------------------------------------------------

    def _load_settings(self, cfg: dict) -> None:
        self.v_tick.set(cfg.get("tick_seconds", 5))
        self.v_autostart.set(autostart.enabled())
        self.v_reselect.set(cfg.get("reselect_minutes", 10))
        self.v_smelt_every.set(cfg.get("smelting", {}).get("check_minutes", 10))
        self.v_bank_free.set(cfg.get("bank_warn_free", 2))
        self.v_start_current.set(cfg.get("start_with_current", True))
        self.v_restore.set(cfg.get("restore_window", True))
        self.v_yield.set(cfg.get("yield_to_user", True))
        self.v_yield_secs.set(cfg.get("yield_seconds", 20))
        self.v_blur_secs.set(cfg.get("yield_after_leaving", 3))
        self.v_scale.set(cfg.get("level_scaling", True))
        self.v_min_turn.set(cfg.get("min_turn_minutes", 8))
        self.v_real.set(cfg.get("real_mouse", {}).get("enabled", True))
        self.v_self_update.set(cfg.get("self_update", {}).get("enabled", True))
        self.v_real_idle.set(cfg.get("real_mouse", {}).get("idle_minutes", 10))
        fcfg = cfg.get("fishing", {})
        self.v_auto_spot.set(fcfg.get("auto_spot", True))
        self.v_use_bait.set(fcfg.get("use_bait", False))
        self.v_bait_spot.set(fcfg.get("bait_spot", "Dock Waters"))
        self.v_variety.set(int(round(fcfg.get("variety", 0.25) * 100)))
        self.v_backlog.set(fcfg.get("backlog_limit", 200))
        self.v_farm.set(cfg.get("farming", {}).get("enabled", True))
        self.v_farm_every.set(cfg.get("farming", {}).get("check_minutes", 60))
        ccfg = cfg.get("combat", {})
        self.v_area.set(ccfg.get("area", "Corrupted Farmlands"))
        self.v_monster.set(ccfg.get("monster", "Evil Plant"))
        self.v_stance.set("keep" if ccfg.get("stance", "keep") in ("auto", "keep") else ccfg["stance"])
        self.v_mode.set("Fixed monster" if ccfg.get("mode", "slayer") == "fixed" else "Slayer task")
        self.v_armour.set(cfg.get("armour", {}).get("enabled", True))
        self.v_fight_food.set(ccfg.get("min_food", 20))
        self.v_fight_eat.set(ccfg.get("eat_below", 50))
        sell = cfg.get("sell", {})
        self.v_sell.set(sell.get("enabled", True))
        self.v_keep_logs.set(sell.get("keep_logs", 200))
        self.v_keep_ores.set(sell.get("keep_ores", 200))
        self.v_keep_bars.set(sell.get("keep_bars", 100))
        self.v_keep_food.set(sell.get("keep_food", 100))
        self.v_sell_gap.set(sell.get("max_gap", 20000))
        self.v_dupes.set(", ".join(sell.get("duplicates", [])))
        self.v_buy_bank.set(cfg.get("shop", {}).get("buy_bank", True))
        self.v_buy_upgrades.set(cfg.get("shop", {}).get("buy_upgrades", True))
        self.v_keep_gold.set(cfg.get("shop", {}).get("keep_gold", 0))
        self.v_gear.set(cfg.get("gear", {}).get("equip", True))
        self.v_block_free.set(cfg.get("bank", {}).get("block_free", 2))
        self.v_fire_keep.set(cfg.get("firemaking", {}).get("keep", 200))
        self.v_open_names.set(", ".join(cfg.get("open_items", {}).get("names", [])))
        self.v_open_every.set(cfg.get("open_items", {}).get("every_minutes", 60))
        self.v_min_raw.set(cfg.get("cooking", {}).get("min_raw", 10))
        self.v_min_food.set(cfg.get("thieving", {}).get("min_food", 20))
        self.v_eat_below.set(cfg.get("thieving", {}).get("eat_below", 60))
        self.v_min_success.set(cfg.get("thieving", {}).get("min_success", 50))
        self.rot_rows = []
        for a in cfg.get("rotation", []):
            lo, hi = minutes_range(a)
            self.rot_rows.append({"skill": tk.StringVar(value=a["skill"]),
                                  "target": tk.StringVar(value=a.get("target", "" if a["skill"] in NO_TARGET else "best")),
                                  "lo": tk.StringVar(value=int(lo)), "hi": tk.StringVar(value=int(hi)),
                                  "on": tk.BooleanVar(value=a.get("enabled", True)),
                                  # Keys the form has no field for (only_while_below, categories ...) ride along.
                                  "extra": {k: v for k, v in a.items()
                                            if k not in ("skill", "target", "minutes", "enabled")}})
        self.rule_rows = [{"bar": tk.StringVar(value=r["bar"]), "min_ore": tk.StringVar(value=r["min_ore"])}
                          for r in cfg.get("smelting", {}).get("rules", [])]
        self._render_rotation()
        self._render_rules()

    def _render_rotation(self) -> None:
        for w in self.rot_frame.winfo_children():
            w.destroy()
        n = len(self.rot_rows)
        for i, row in enumerate(self.rot_rows):
            f = ttk.Frame(self.rot_frame)
            f.pack(fill="x", pady=2)
            ttk.Checkbutton(f, text=f"{i + 1}.", variable=row["on"], cursor="hand2", width=3).pack(side="left")
            skill = ttk.Combobox(f, textvariable=row["skill"], values=SUPPORTED_SKILLS, state="readonly",
                                 width=12, cursor="hand2")
            skill.pack(side="left")
            skill.bind("<<ComboboxSelected>>", lambda e: self._render_rotation())
            target = ttk.Entry(f, textvariable=row["target"], width=18)
            target.pack(side="left", padx=(6, 0))
            if row["skill"].get() in ("Fishing", "Cooking", "Firemaking", "Thieving", "Combat"):  # these choose by their own rules
                row["target"].set("")
                target.state(["disabled"])
            ttk.Spinbox(f, from_=0, to=600, increment=5, textvariable=row["lo"], width=4).pack(side="left", padx=(6, 0))
            ttk.Label(f, text="to").pack(side="left", padx=4)
            ttk.Spinbox(f, from_=0, to=600, increment=5, textvariable=row["hi"], width=4).pack(side="left")
            ttk.Label(f, text="min").pack(side="left", padx=(4, 8))
            # Drawn arrows: the arrow characters come out as coloured emoji on some systems.
            up = button(f, "", lambda i=i: self._move_rotation(i, -1), "Icon.TButton",
                        image=(self._images["up"], "disabled", self._images["up_off"]))
            down = button(f, "", lambda i=i: self._move_rotation(i, 1), "Icon.TButton",
                          image=(self._images["down"], "disabled", self._images["down_off"]))
            rm = button(f, "Remove", lambda i=i: self._remove(self.rot_rows, i, self._render_rotation), "Small.TButton")
            up.pack(side="left")
            down.pack(side="left", padx=(4, 0))
            rm.pack(side="left", padx=(4, 0))
            set_enabled(up, i > 0)
            set_enabled(down, i < n - 1)
            set_enabled(rm, n > 1)

    def _render_rules(self) -> None:
        for w in self.rule_frame.winfo_children():
            w.destroy()
        bars = sorted(set(KNOWN_BARS) | set(self.bot.smelt_counts if self.bot else {}))
        for i, row in enumerate(self.rule_rows):
            f = ttk.Frame(self.rule_frame)
            f.pack(fill="x", pady=2)
            ttk.Combobox(f, textvariable=row["bar"], values=bars, width=16, cursor="hand2").pack(side="left")
            ttk.Label(f, text="from").pack(side="left", padx=(8, 6))
            ttk.Spinbox(f, from_=1, to=100000, increment=50, textvariable=row["min_ore"], width=7).pack(side="left")
            ttk.Label(f, text="ore").pack(side="left", padx=(4, 8))
            button(f, "Remove", lambda i=i: self._remove(self.rule_rows, i, self._render_rules), "Small.TButton").pack(side="left")

    def _add_rotation(self) -> None:
        self.rot_rows.append({"skill": tk.StringVar(value="Mining"), "target": tk.StringVar(value="best"),
                              "lo": tk.StringVar(value=20), "hi": tk.StringVar(value=40),
                              "on": tk.BooleanVar(value=True), "extra": {}})
        self._render_rotation()

    def _add_rule(self) -> None:
        self.rule_rows.append({"bar": tk.StringVar(value="Copper Bar"), "min_ore": tk.StringVar(value=100)})
        self._render_rules()

    def _move_rotation(self, i: int, step: int) -> None:
        j = i + step
        self.rot_rows[i], self.rot_rows[j] = self.rot_rows[j], self.rot_rows[i]
        self._render_rotation()

    @staticmethod
    def _remove(rows: list, i: int, render) -> None:
        rows.pop(i)
        render()

    def save_settings(self) -> None:
        def num(var: tk.StringVar, label: str, lo: int) -> int:
            try:
                value = int(str(var.get()).strip())
            except ValueError:
                raise ValueError(f"{label}: enter a whole number.") from None
            if value < lo:
                raise ValueError(f"{label}: minimum {lo}.")
            return value

        try:
            rotation = []
            for i, row in enumerate(self.rot_rows, 1):
                act = {**row.get("extra", {}), "skill": row["skill"].get()}
                target = row["target"].get().strip()
                if act["skill"] not in NO_TARGET:
                    act["target"] = target or "best"
                elif target and target != "best":
                    act["target"] = target  # e.g. "level 5" for a recipe skill
                lo = num(row["lo"], f"Rotation {i}, minutes from", 0)
                hi = num(row["hi"], f"Rotation {i}, minutes to", 0)
                if hi < lo:
                    raise ValueError(f"Rotation {i}: 'to' must be at least 'from'.")
                if hi:
                    act["minutes"] = lo if lo == hi else [lo, hi]
                if not row["on"].get():
                    act["enabled"] = False
                rotation.append(act)
            if not rotation:
                raise ValueError("Rotation: at least one activity.")
            rules = []
            for i, row in enumerate(self.rule_rows, 1):
                bar = row["bar"].get().strip()
                if not bar:
                    raise ValueError(f"Smelt rule {i}: pick a bar.")
                rules.append({"bar": bar, "min_ore": num(row["min_ore"], f"Smelt rule {i}", 1)})
            cfg = {
                **self.cfg,  # keys the window has no field for (game_launch, update_check, ...) stay
                "tick_seconds": num(self.v_tick, "Look every", 2),
                "reselect_minutes": num(self.v_reselect, "Look for new unlocks", 1),
                "bank_warn_free": num(self.v_bank_free, "Bank warning", 0),
                "start_with_current": bool(self.v_start_current.get()),
                "restore_window": bool(self.v_restore.get()),
                "yield_to_user": bool(self.v_yield.get()),
                "yield_seconds": num(self.v_yield_secs, "Seconds without input", 3),
                "yield_after_leaving": num(self.v_blur_secs, "After switching window", 0),
                "level_scaling": bool(self.v_scale.get()),
                "min_turn_minutes": num(self.v_min_turn, "Shortest turn", 1),
                "self_update": {**self.cfg.get("self_update", {}), "enabled": bool(self.v_self_update.get())},
                "real_mouse": {**self.cfg.get("real_mouse", {}), "enabled": bool(self.v_real.get()),
                               "idle_minutes": num(self.v_real_idle, "Away for at least", 1)},
                "fishing": {
                    **self.cfg.get("fishing", {}),
                    "auto_spot": bool(self.v_auto_spot.get()),
                    "use_bait": bool(self.v_use_bait.get()),
                    "bait_spot": self.v_bait_spot.get().strip(),
                    "variety": min(num(self.v_variety, "Variety", 0), 100) / 100,
                    "backlog_limit": num(self.v_backlog, "Fishing, pile", 0),
                },
                "farming": {"enabled": bool(self.v_farm.get()), "check_minutes": num(self.v_farm_every, "Farming", 1)},
                "firemaking": {**self.cfg.get("firemaking", {"min_logs": 10}),
                               "keep": num(self.v_fire_keep, "Firemaking, keep", 0)},
                "bank": {"block_free": num(self.v_block_free, "Bank, free slots", 0)},
                "shop": {**self.cfg.get("shop", {}), "buy_bank": bool(self.v_buy_bank.get()),
                         "buy_upgrades": bool(self.v_buy_upgrades.get()),
                         "keep_gold": num(self.v_keep_gold, "Buying, gold to keep", 0)},
                "ranching": self.cfg.get("ranching", {"enabled": True, "warn_below": 60}),
                "gear": {**self.cfg.get("gear", {"enabled": True, "every_minutes": 120}), "equip": bool(self.v_gear.get())},
                "sell": {**self.cfg.get("sell", {}), "enabled": bool(self.v_sell.get()),
                         "keep_logs": num(self.v_keep_logs, "Selling, logs", 0),
                         "keep_ores": num(self.v_keep_ores, "Selling, ores", 0),
                         "keep_bars": num(self.v_keep_bars, "Selling, bars", 0),
                         "keep_food": num(self.v_keep_food, "Selling, food", 0),
                         "max_gap": num(self.v_sell_gap, "Selling, shortfall", 0),
                         "duplicates": [n.strip() for n in self.v_dupes.get().split(",") if n.strip()]},
                "armour": {**self.cfg.get("armour", {}), "enabled": bool(self.v_armour.get())},
                "combat": {"area": self.v_area.get().strip(), "monster": self.v_monster.get().strip(),
                           "stance": self.v_stance.get() or "keep",
                           "mode": "fixed" if self.v_mode.get() == "Fixed monster" else "slayer",
                           "min_food": num(self.v_fight_food, "Combat, food", 1),
                           "eat_below": min(num(self.v_fight_eat, "Combat, HP", 10), 95)},
                "open_items": {"names": [n.strip() for n in self.v_open_names.get().split(",") if n.strip()],
                               "every_minutes": num(self.v_open_every, "Open", 5)},
                "cooking": {**self.cfg.get("cooking", {"categories": ["Basic Food"]}),
                            "min_raw": num(self.v_min_raw, "Cooking, raw", 1)},
                "thieving": {"min_food": num(self.v_min_food, "Thieving, food", 1),
                             "eat_below": min(num(self.v_eat_below, "Thieving, HP", 10), 95),
                             "min_success": min(num(self.v_min_success, "Thieving, success chance", 30), 100)},
                "rotation": rotation,
                "smelting": {"check_minutes": num(self.v_smelt_every, "Check smelting", 1), "rules": rules},
            }
        except ValueError as e:
            messagebox.showerror("Not saved", str(e), parent=self.root)
            return
        save_config(cfg)
        self.cfg = cfg
        if bool(self.v_autostart.get()) != autostart.enabled():
            try:
                autostart.set_enabled(bool(self.v_autostart.get()))
            except Exception as e:  # noqa: BLE001 - settings are saved; only the shortcut failed
                messagebox.showerror("Autostart", f"Could not change the Startup shortcut: {e}", parent=self.root)
        if self.bot and self.running:
            self.bot.update_config(cfg)
        self.saved_label.configure(text="Saved" + (" and applied" if self.running else ""))
        self.saved_note_until = time.time() + 4

    # --- bot control -----------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.ctl.stop.clear()
        self.ctl.paused.clear()
        self.bot = Bot(Game(GameWindow(), dry_run=self.dry_run.get()), self.cfg, self.ctl)
        if self.updated_from:
            self.bot.event("game", f"Melvor Bot updated to {VERSION} (was {self.updated_from})")
            self.updated_from = ""
        self.thread = threading.Thread(target=self._run_bot, args=(self.bot,), daemon=True)
        self.thread.start()

    def _update_safe_now(self) -> bool:
        """A moment the bot can be stopped for an update: no fight or Thieving (HP), nobody playing."""
        if not self.running or not self.bot:
            return True
        st = self.bot.status
        return not (st and st.skill in ("Combat", "Thieving")) and self.bot.state not in ("yielding", "starting")

    def _tend_update(self) -> None:
        """Called from poll: install a downloaded update once it is safe, then hand over and exit."""
        if self.pending_update:
            if self.running:
                return  # the bot is still finishing its tick
            version, exe, restart = self.pending_update
            try:
                update.apply(exe, restart)
            except OSError as e:
                log.warning("update to %s failed: %s", version, e)
                self.pending_update = None
                self.updater.ready = None
                if restart:
                    self.start()
                return
            log.info("update: handing over to %s", version)
            self.root.destroy()
            os._exit(0)
        ready = self.updater.ready
        if ready and self.v_self_update.get() and self._update_safe_now():
            log.info("update: installing %s", ready[0])
            self.pending_update = (ready[0], ready[1], self.running)
            self.stop()

    def _run_bot(self, bot: Bot) -> None:
        log.info("bot started%s", " (DRY-RUN)" if bot.g.dry_run else "")
        try:
            bot.run()
        except Exception:
            log.exception("bot crashed")
        log.info("bot stopped")

    def pause(self) -> None:
        self.ctl.toggle_pause()

    def skip(self) -> None:
        """End the current activity's turn and go to the next one in the rotation."""
        if self.running:
            log.info("next activity requested")
            self.ctl.skip.set()

    def stop(self) -> None:
        if self.running:
            log.info("stop requested")
            self.ctl.stop.set()

    def on_close(self) -> None:
        self.ctl.stop.set()
        self.root.destroy()

    def open_log(self) -> None:
        path = ROOT / "logs" / "bot.log"
        if path.exists():
            os.startfile(path)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # --- refresh loop ----------------------------------------------------------------

    def current_state(self) -> str:
        if not self.running:
            return "stopped"
        if self.ctl.stop.is_set():
            return "stopping"
        if self.ctl.paused.is_set():
            return "paused"
        return self.bot.state if self.bot else "stopped"

    def poll(self) -> None:
        self._drain_log()
        state = self.current_state()
        colour, label = STATES.get(state, (GREY, state))
        self.light.itemconfigure(self.light_dot, fill=colour)
        self.state_label.configure(text=label)
        self.root.title(f"Melvor Bot {VERSION} ({label})")
        self.detail_label.configure(text=self._detail(state))
        self.update_note.configure(text=f"Version {VERSION}. " + (self.updater.status or "Looking for updates soon."))
        self._tend_update()

        running = self.running
        set_enabled(self.btn_start, not running)
        set_enabled(self.btn_pause, running and state != "stopping")
        set_enabled(self.btn_stop, running and state != "stopping")
        set_enabled(self.chk_dry, not running)
        set_enabled(self.btn_skip, running and state not in ("stopping", "paused"))
        self.btn_pause.configure(text="Resume" if self.ctl.paused.is_set() else "Pause")
        self._update_overview()
        if self.saved_note_until and time.time() > self.saved_note_until:
            self.saved_label.configure(text="")
            self.saved_note_until = 0.0
        self.root.after(250, self.poll)

    def _detail(self, state: str) -> str:
        b = self.bot
        if state == "stopped":
            if b and b.status and b.status.skill == "Thieving":
                return "Warning: Thieving keeps running and the bot is not watching your HP. Start the bot or stop Thieving in the game."
            return "Press Start." if not b else "Stopped. Press Start to continue."
        if state == "paused":
            return "Click what you want in the game. Resume with the button or Ctrl+Alt+P."
        if state == "yielding":
            return (f"The bot stands back while you use the game. It carries on "
                    f"{self.cfg.get('yield_seconds', 20)} s after your last click or key in it, or "
                    f"{self.cfg.get('yield_after_leaving', 3)} s after you switch to another window.")
        if state == "waiting" and b:
            return PROBLEMS.get(b.problem_code, b.problem)
        if state == "error" and b:
            return f"Failed {b.errors} times in a row: {b.problem}. See the Technical log."
        if b and b.smelting:
            return "Smelting bars, then back to the rotation."
        if b:
            act = b.current
            step = f"Rotation step {b.idx + 1}: {act['skill']}" + (f" ({b.target_name})" if b.target_name else "")
            if b.task:
                return f"{step}. Now {b.task}…" + (" (dry run)" if b.g.dry_run else "")
            return step + (" (dry run)" if b.g.dry_run else "")
        return ""

    def _update_overview(self) -> None:
        b = self.bot
        st = b.status if b else None
        running = self.running
        state = self.current_state()
        self.status_box.configure(background=STATES.get(state, (GREY, ""))[0])

        # Headline: what the character does right now, as the game reports it.
        if not b:
            now = "Not started yet"
        elif b.smelting:
            now = "Smelting bars"
        elif st and st.skill:
            now = st.skill + (f": {st.detail}" if st.detail else "") + (f" (level {st.level})" if st.level else "")
        else:
            now = "Nothing (the bot starts the next activity shortly)" if running else "Nothing"
        self.now_label.configure(text=now)

        # Progress of the current activity and what comes after it.
        left = b.seconds_to_switch() if b and running else None
        if b and running and left is not None and b.duration:
            self.progress.configure(value=int(1000 * (1 - left / b.duration)))
            nxt = b.rotation[(b.idx + 1) % len(b.rotation)]
            target = nxt.get("target", "")
            then = nxt["skill"] + (f" ({target})" if target else "")
            game = st.skill if st else None
            if left <= 0:
                text = f"{b.current['skill']}'s turn is over: switching to {then}"
            elif game and game != b.current["skill"] and not b.smelting:
                text = f"Starting {b.current['skill']} (the game still shows {game}); then {then}"
            else:
                text = f"{fmt_secs(left)} left, then {then}"
            self.next_label.configure(text=text)
        else:
            self.progress.configure(value=0)
            self.next_label.configure(text=("Stays on this until it finishes, then the next in the rotation"
                                            if b and running else ""))

        if b:
            act = b.current
            target = b.target_name or act.get("target", "")
            self.info["activity"].configure(
                text=f"{b.idx + 1} of {len(b.rotation)}: {act['skill']}" + (f" ({target})" if target and act["skill"] != "Fishing" else ""))
            if b.smelting:
                self.info["smelt"].configure(text="running")
            else:
                wait = b.seconds_to_smelt_check() if running else None
                self.info["smelt"].configure(text="no rules" if wait is None and running else
                                             (f"check in {fmt_secs(wait)}" if wait is not None else "—"))
            wait = b.seconds_to_farm_check() if running else None
            self.info["farm"].configure(text="off" if wait is None and running else
                                        (f"check in {fmt_secs(wait)}" if wait is not None else "—"))
        else:
            for key in ("activity", "smelt", "farm"):
                self.info[key].configure(text="—")
        if st and st.bank_used is not None and st.bank_max:
            free = st.bank_max - st.bank_used
            full = bool(b and b.bank_full())
            self.info["bank"].configure(
                text=f"{st.bank_used}/{st.bank_max} ({free} free)" + (". Full: known items only" if full else ""),
                foreground=RED if full or free <= self.cfg.get("bank_warn_free", 2) else TEXT)
        else:
            self.info["bank"].configure(text="—", foreground=TEXT)
        v = b.vitals if b else None
        self.info["health"].configure(text=f"{v.hp}/{v.hp_max}" if v and v.hp is not None else "—")
        if v and v.food_heal:
            count = v.food_count if v.food_count is not None else "?"
            self.info["equipped"].configure(text=f"{count} pieces, +{v.food_heal} HP each",
                                            foreground=RED if v.food_count == 0 else TEXT)
        else:
            self.info["equipped"].configure(text="—", foreground=TEXT)
        if b and b.food:
            stock = sorted(b.food.items(), key=lambda kv: -kv[1][1])
            self.info["food"].configure(text=", ".join(f"{n} {s if s is not None else '?'} (+{h})" for n, (s, h) in stock))
        else:
            self.info["food"].configure(text="—")
        waiting = b.waiting_fish() if b else {}
        if b and (b.cooking_level or waiting):
            level = f"Cooking {b.cooking_level or '?'}" + (f", next recipe at {b.cooking_next}" if b.cooking_next else "")
            piles = ", ".join(f"{n} {c}" for n, c in sorted(waiting.items(), key=lambda kv: -kv[1]))
            rest = (f"Not cookable yet: {piles}" if piles else "Everything in stock is cookable" if b.fish_table
                    else "Fish per spot not read yet; that happens at the first fishing session")
            self.info["cooking"].configure(text=f"{level}. {rest}")
        else:
            self.info["cooking"].configure(text="—")
        if b and (b.fire_logs or b.fire_next):
            logs = ", ".join(f"{n} {c}" for n, c in b.fire_logs.items()) or "no logs"
            self.info["fire"].configure(text=f"To burn: {logs}" + (f". Next log at level {b.fire_next}" if b.fire_next else ""))
        else:
            self.info["fire"].configure(text="—")
        if b and b.ranch:
            names = {"happiness": "happy", "hunger": "food", "comfort": "comfort", "satisfaction": "satisfied"}
            low = any(b.ranch.get(k, 100) < self.cfg.get("ranching", {}).get("warn_below", 60) for k in ("hunger", "comfort"))
            self.info["ranch"].configure(text=", ".join(f"{names.get(k, k)} {v}%" for k, v in b.ranch.items()),
                                         foreground=RED if low else TEXT)
        else:
            self.info["ranch"].configure(text="—", foreground=TEXT)
        if b and b.gear:
            self.info["gear"].configure(text="; ".join(f"{skill}: {', '.join(items)}" for skill, items in b.gear.items()))
        else:
            self.info["gear"].configure(text="—")
        if b and b.slayer:
            self.info["slayer"].configure(text=f"{b.slayer[0]}, {b.slayer[1]} of {b.slayer[2]}")
        else:
            self.info["slayer"].configure(text="—")
        gold = b.gold_seen if b and b.gold_seen is not None else (b.gold if b else None)
        self.info["gold"].configure(text=f"{gold:,} GP" if gold is not None else "—")

        self._render_journal()

        shop_rows = []
        for e in (b.shop if b else []):
            text, ready = b.upgrade_status(e)
            shop_rows.append((e.get("category", ""), e.get("name", ""), text, ready))
        shop_rows.sort(key=lambda r: (not r[3], r[0], r[1]))
        if shop_rows != getattr(self, "_shop_rows", None):
            self._shop_rows = shop_rows
            self.shop_tree.delete(*self.shop_tree.get_children())
            for cat, name, text, ready in shop_rows:
                self.shop_tree.insert("", "end", values=(cat, name, text), tags=("ready",) if ready else ())

        rules = {r["bar"]: r["min_ore"] for r in self.cfg.get("smelting", {}).get("rules", [])}
        counts = b.smelt_counts if b else {}
        rows = []
        for bar in sorted(set(rules) | set(counts)):
            have = counts.get(bar)
            ready = have is not None and bar in rules and have[0] >= rules[bar]
            rows.append((bar, f"{have[0]} (needs {have[1]})" if have else "—", rules.get(bar, "—"), ready))
        if rows != getattr(self, "_ore_rows", None):
            self._ore_rows = rows
            self.ore_tree.delete(*self.ore_tree.get_children())
            for bar, ore, rule, ready in rows:
                self.ore_tree.insert("", "end", values=(bar, ore, rule), tags=("ready",) if ready else ())

    def _drain_log(self) -> None:
        lines = []
        try:
            while True:
                lines.append(self.logq.get_nowait())
        except queue.Empty:
            pass
        if not lines:
            return
        self.log_text.configure(state="normal")
        for level, text in lines:
            tag = "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
            self.log_text.insert("end", text + "\n", tag)
        excess = int(self.log_text.index("end-1c").split(".")[0]) - 1000
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.last_line.configure(text="Last step: " + re.sub(r"\s{2,}", " ", re.sub(r" (INFO|WARNING|ERROR)\s+", " ", lines[-1][1], count=1)))


def main() -> None:
    use_root()
    # An own app id: the taskbar then shows the bot's icon and groups it apart from other Python
    # programs, instead of the pythonw logo.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Melvor2Bot.Gui")
    except (AttributeError, OSError):
        pass
    argv = sys.argv[1:]
    if FROZEN:
        if install.handle_start(argv):  # offered or ran the install / uninstall: this copy is done
            return
        install.register_version()
        update.cleanup()
    root = tk.Tk()
    app = App(root)
    if "--updated-from" in argv:
        i = argv.index("--updated-from")
        app.updated_from = argv[i + 1] if i + 1 < len(argv) else "?"
        log.info("updated to %s (was %s)", VERSION, app.updated_from)
    if "--start" in argv:  # start the bot right away (e.g. from a shortcut)
        root.after(800, app.start)
    root.mainloop()


if __name__ == "__main__":
    main()
