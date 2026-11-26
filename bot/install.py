"""MelvorBot.exe installs itself: no separate installer.

Started from anywhere else (a download), it offers to install for this Windows user: the exe goes
to %LOCALAPPDATA%\\Programs\\MelvorBot, a Start menu shortcut and an entry under Windows' installed
apps are added (both per user, no admin). Uninstalling (from Windows' app list, which runs
`MelvorBot.exe --uninstall`) removes all of that and the autostart shortcut, and keeps the data
folder with config, logs and state.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tkinter as tk
import winreg
from tkinter import messagebox

from . import autostart
from .paths import INSTALL_DIR, INSTALLED_EXE, ROOT
from .version import VERSION

NAME = "Melvor Bot"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MelvorBot"
START_MENU = pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
MENU_LINK = START_MENU / f"{NAME}.lnk"
DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def running_installed() -> bool:
    try:
        return pathlib.Path(sys.executable).resolve() == INSTALLED_EXE.resolve()
    except OSError:
        return False


def _shortcut(path: pathlib.Path, target: pathlib.Path, args: str = "") -> None:
    import win32com.client

    link = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(path))
    link.TargetPath = str(target)
    link.Arguments = args
    link.WorkingDirectory = str(target.parent)
    link.IconLocation = f"{target},0"
    link.Description = "Melvor Idle 2 helper bot"
    link.save()


def _register(exe: pathlib.Path) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in {
            "DisplayName": NAME, "DisplayVersion": VERSION, "Publisher": NAME,
            "DisplayIcon": f"{exe},0", "InstallLocation": str(exe.parent),
            "UninstallString": f'"{exe}" --uninstall',
        }.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, exe.stat().st_size // 1024)


def register_version() -> None:
    """Keep the version in Windows' app list right after a self-update."""
    if running_installed():
        try:
            _register(INSTALLED_EXE)
        except OSError:
            pass


def install() -> None:
    """Copy this exe into place, add the shortcuts and the app entry, and start the installed copy."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if INSTALLED_EXE.exists():
        # A running copy keeps its file open: move it aside (allowed for a running exe), the
        # next start removes it.
        old = INSTALLED_EXE.with_suffix(".exe.old")
        old.unlink(missing_ok=True)
        INSTALLED_EXE.rename(old)
    shutil.copy2(sys.executable, INSTALLED_EXE)
    START_MENU.mkdir(parents=True, exist_ok=True)
    _shortcut(MENU_LINK, INSTALLED_EXE)
    _register(INSTALLED_EXE)
    if autostart.enabled():
        autostart.set_enabled(True)  # point an existing autostart shortcut at the installed exe
    from .update import spawn_independent

    spawn_independent([str(INSTALLED_EXE), "--start"], INSTALL_DIR)


def uninstall() -> None:
    autostart.set_enabled(False)
    MENU_LINK.unlink(missing_ok=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    # The running exe cannot delete itself: a detached shell removes the folder once it has exited.
    subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{INSTALL_DIR}"',
                     creationflags=DETACHED | subprocess.CREATE_NO_WINDOW, close_fds=True)


def handle_start(argv: list[str]) -> bool:
    """Run the install or uninstall step this start asks for. True: the program should exit now."""
    if "--uninstall" in argv:
        root = tk.Tk()
        root.withdraw()
        if messagebox.askyesno(NAME, f"Uninstall {NAME}?\n\nYour settings and logs stay in\n{ROOT}", parent=root):
            uninstall()
            messagebox.showinfo(NAME, f"{NAME} is uninstalled.", parent=root)
        root.destroy()
        return True
    if running_installed() or "--portable" in argv:
        old = INSTALLED_EXE.with_suffix(".exe.old")
        try:
            old.unlink(missing_ok=True)  # left by an update or a reinstall
        except OSError:
            pass
        return False
    if "--install" in argv:  # quiet install (scripts): no question
        install()
        return True
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # the question must not hide behind other windows
    if INSTALLED_EXE.exists():
        question = f"Update the installed {NAME} to {VERSION}?\n\nClose the running {NAME} first."
    else:
        question = (f"Install {NAME} {VERSION} for this Windows user?\n\nProgram: {INSTALL_DIR}\n"
                    f"Settings and logs: {ROOT}\n\nIt adds a Start menu entry and updates itself "
                    "from GitHub releases.")
    answer = messagebox.askyesnocancel(NAME, question + "\n\nNo = run this copy without installing.", parent=root)
    root.destroy()
    if answer is None:
        return True
    if answer:
        try:
            install()
        except OSError as e:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(NAME, f"Install failed: {e}", parent=root)
            root.destroy()
        return True
    return False
