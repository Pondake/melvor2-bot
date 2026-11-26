"""Start the bot when the user signs in to Windows: a shortcut in the user's Startup folder.

The shortcut runs the GUI with --start, so the bot begins at once and starts the game through
Steam when it is not running. Off unless the user ticks it in the window (Settings > General).
"""
import os
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

from .paths import FROZEN, ROOT

STARTUP = pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LINK = STARTUP / "Melvor Bot.lnk"
ICON = ROOT / "logs" / "melvor-bot.ico"
GREEN = "#1e874b"  # the window's green (gui.GREEN)


def enabled() -> bool:
    return LINK.exists()


def icon_image(size: int = 256) -> Image.Image:
    """The bot's "M" icon (window, taskbar and shortcut)."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((size // 32, size // 32, size - 1 - size // 32, size - 1 - size // 32),
                        radius=size * 7 // 32, fill=GREEN)
    try:
        font = ImageFont.truetype("seguisb.ttf", size * 5 // 8)
    except OSError:
        font = ImageFont.load_default()
    d.text((size // 2, size * 15 // 32), "M", fill="#ffffff", font=font, anchor="mm")
    return im


def write_icon() -> pathlib.Path:
    ICON.parent.mkdir(exist_ok=True)
    icon_image().save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return ICON


def set_enabled(on: bool) -> None:
    if not on:
        LINK.unlink(missing_ok=True)
        return
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    link = shell.CreateShortCut(str(LINK))
    if FROZEN:  # release build: MelvorBot.exe carries its own icon
        link.TargetPath = sys.executable
        link.Arguments = "--start"
        link.IconLocation = f"{sys.executable},0"
    else:
        link.TargetPath = str(ROOT / ".venv" / "Scripts" / "pythonw.exe")
        link.Arguments = "-m bot.gui --start"
        link.IconLocation = str(write_icon())
    link.WorkingDirectory = str(ROOT)
    link.Description = "Melvor Idle 2 helper bot"
    link.save()
