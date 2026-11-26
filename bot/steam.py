"""Steam: which build of the game is installed, whether an update waits, and the latest patch notes.

Steam downloads updates in the background but cannot apply them while the game runs, and the game
shows no notice. The bot checks the app manifest now and then and writes what it finds to the journal.
"""
import csv
import html
import json
import logging
import os
import pathlib
import re
import subprocess
import urllib.request
import winreg

log = logging.getLogger("melvor2")

APP_ID = 3218350
GAME_EXE = "Melvor2.exe"
NEWS_URL = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
            f"?appid={APP_ID}&count=5&maxlength=0&format=json")
STATE_UPDATE_REQUIRED = 2


def _steam_root() -> pathlib.Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return pathlib.Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        default = pathlib.Path(r"C:\Program Files (x86)\Steam")
        return default if default.exists() else None


def pids(exe: str = GAME_EXE) -> list[int]:
    """Process ids of a running program (by image name), without extra packages."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(row[1]) for row in csv.reader(out.splitlines())
            if len(row) > 1 and row[0].lower() == exe.lower() and row[1].isdigit()]


def launch_game() -> None:
    """Start the game the way the Steam library does; this also starts Steam when it is not running
    (Steam signs in with the remembered account, then opens the game)."""
    os.startfile(f"steam://rungameid/{APP_ID}")


def kill_game() -> None:
    for pid in pids():
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)


def _vdf_pairs(text: str) -> dict[str, str]:
    """Flat "key" "value" pairs of a Valve KeyValues file (the first of each key wins)."""
    out: dict[str, str] = {}
    for key, value in re.findall(r'"([^"]+)"\s+"([^"]*)"', text):
        out.setdefault(key, value)
    return out


def manifest_path() -> pathlib.Path | None:
    root = _steam_root()
    if not root:
        return None
    libraries = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        libraries += [pathlib.Path(p.replace("\\\\", "\\"))
                      for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace"))]
    except OSError:
        pass
    for lib in libraries:
        path = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
        if path.exists():
            return path
    return None


def build_info() -> dict | None:
    """{"build", "target", "updated" (unix time the files changed), "pending" (an update waits)}."""
    path = manifest_path()
    if not path:
        return None
    try:
        kv = _vdf_pairs(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    build = kv.get("buildid", "")
    target = kv.get("TargetBuildID") or build
    flags = int(kv.get("StateFlags", "0") or 0)
    return {"build": build, "target": target, "updated": int(kv.get("lastupdated", "0") or 0),
            "pending": bool(target and target != build) or bool(flags & STATE_UPDATE_REQUIRED)}


def _plain(text: str) -> str:
    """News bodies come as HTML or BBCode: make readable text with one change per line."""
    text = re.sub(r"\[/?(?:p|h\d|list|olist|\*)\]|<\s*/?\s*(?:p|br|li|h\d|ul|ol)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"\[[^\]]+\]|<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def latest_update_notes(timeout: float = 8.0) -> dict | None:
    """The newest "Game Update" post: {"title", "date", "url", "text"}; None when offline."""
    try:
        req = urllib.request.Request(NEWS_URL, headers={"User-Agent": "melvor2-bot"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            items = json.load(resp).get("appnews", {}).get("newsitems", [])
    except Exception as e:  # noqa: BLE001 - no network is not an error for the bot
        log.info("update check: news not fetched (%s)", e)
        return None
    for item in items:
        if re.search(r"update|patch", item.get("title", ""), re.I):
            return {"title": item["title"], "date": item.get("date", 0), "url": item.get("url", ""),
                    "text": _plain(item.get("contents", ""))}
    return None
