"""Self-update from the GitHub releases of this project (public: no token).

A thread looks for a newer release a minute after the start and then every `self_update.every_minutes`,
downloads its MelvorBot.exe and checks it against SHA256SUMS.txt in the same release. The window then
waits for a safe moment (no fight or Thieving, nobody at the game), stops the bot, moves the running
exe aside (allowed on Windows), puts the new one in its place and starts it with --start.
Only in the installed release build; running from source never updates itself.
"""
import hashlib
import json
import logging
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.request

from .paths import FROZEN, INSTALLED_EXE, ROOT
from .version import VERSION

log = logging.getLogger("bot")

REPO = "Pondake/melvor2-bot"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
EXE_ASSET = "MelvorBot.exe"
SUMS_ASSET = "SHA256SUMS.txt"
MAX_BYTES = 400 * 1024 * 1024
UPDATE_DIR = ROOT / "update"
DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def _get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "melvor-bot-updater",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release() -> dict | None:
    """{"version", "exe", "sums", "notes"} of the newest release, or None when it has no exe."""
    with _get(LATEST) as resp:
        data = json.load(resp)
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    if EXE_ASSET not in assets or SUMS_ASSET not in assets:
        return None
    return {"version": data.get("tag_name", "").lstrip("v"), "exe": assets[EXE_ASSET],
            "sums": assets[SUMS_ASSET], "notes": data.get("body") or ""}


def download(release: dict) -> pathlib.Path:
    """Fetch the release's exe into the data folder and check its SHA-256; raises on a mismatch."""
    with _get(release["sums"]) as resp:
        sums = resp.read().decode("utf-8", "replace")
    want = next((m.group(1).lower() for m in re.finditer(r"([0-9a-fA-F]{64})\s+\*?(\S+)", sums)
                 if m.group(2) == EXE_ASSET), None)
    if not want:
        raise ValueError(f"no SHA-256 for {EXE_ASSET} in {SUMS_ASSET}")
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATE_DIR / f"MelvorBot-{release['version']}.exe"
    part = target.with_suffix(".part")
    digest, size = hashlib.sha256(), 0
    with _get(release["exe"], timeout=60) as resp, part.open("wb") as out:
        while chunk := resp.read(1 << 20):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("download too large")
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != want:
        part.unlink(missing_ok=True)
        raise ValueError("download does not match its SHA-256")
    part.replace(target)
    return target


def apply(new_exe: pathlib.Path, start_bot: bool) -> None:
    """Swap the running exe for the new one and start it. The caller exits right after."""
    current = pathlib.Path(sys.executable)
    old = current.with_suffix(".exe.old")
    old.unlink(missing_ok=True)
    current.rename(old)
    try:
        new_exe.replace(current)
    except OSError:
        old.rename(current)  # put the working version back
        raise
    args = [str(current), "--updated-from", VERSION] + (["--start"] if start_bot else [])
    subprocess.Popen(args, cwd=str(current.parent), creationflags=DETACHED, close_fds=True)


def cleanup() -> None:
    for path in UPDATE_DIR.glob("MelvorBot-*") if UPDATE_DIR.exists() else []:
        try:
            path.unlink()
        except OSError:
            pass


class Updater:
    """Background check and download; `ready` holds (version, path) once a verified exe waits."""

    def __init__(self, cfg_getter):
        self.cfg = cfg_getter
        self.ready: tuple[str, pathlib.Path] | None = None
        self.status = "" if self.enabled_here() else "updates only in the installed version"
        self.check_now = threading.Event()

    @staticmethod
    def enabled_here() -> bool:
        if not FROZEN:
            return False
        try:
            return pathlib.Path(sys.executable).resolve() == INSTALLED_EXE.resolve()
        except OSError:
            return False

    def start(self) -> None:
        if self.enabled_here():
            threading.Thread(target=self._loop, daemon=True, name="updater").start()

    def _loop(self) -> None:
        self.check_now.wait(60)  # let the start settle
        while True:
            ucfg = self.cfg().get("self_update", {})
            if ucfg.get("enabled", True) and not self.ready:
                self._check()
            self.check_now.clear()
            self.check_now.wait(max(10, ucfg.get("every_minutes", 60)) * 60)

    def _check(self) -> None:
        try:
            release = latest_release()
            if not release or parse_version(release["version"]) <= parse_version(VERSION):
                self.status = f"up to date ({VERSION})"
                return
            self.status = f"downloading {release['version']}"
            log.info("update: %s found, downloading", release["version"])
            path = download(release)
            self.ready = (release["version"], path)
            self.status = f"{release['version']} ready, installs at a quiet moment"
            log.info("update: %s downloaded and verified", release["version"])
        except Exception as e:  # noqa: BLE001 - no network or a bad release must never stop the bot
            self.status = f"update check failed: {e}"
            log.warning("update check failed: %s", e)
        self.checked_at = time.time()
