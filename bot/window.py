"""Find the Melvor Idle 2 window, capture it and send input without moving the real mouse.

Capture uses PrintWindow, so the game may be covered by other windows. It must not be
minimized: a minimized window has no client area to render.
Input uses PostMessage, so the user's cursor and keyboard stay free.
"""
import ctypes
import time

import numpy as np
from PIL import Image
import win32api
import win32con
import win32gui
import win32ui

WINDOW_TITLE = "Melvor2"
PW_CLIENTONLY_RENDERFULL = 3  # PW_CLIENTONLY | PW_RENDERFULLCONTENT

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass


class GameWindowError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code  # not_found | minimized | capture_failed


class GameWindow:
    def __init__(self, title: str = WINDOW_TITLE):
        self.title = title
        self.hwnd = 0  # found lazily on first capture, so the bot can start before the game

    def refresh(self) -> None:
        hwnd = win32gui.FindWindow(None, self.title)
        if not hwnd:
            raise GameWindowError(f"Window '{self.title}' not found. Is the game running?", "not_found")
        self.hwnd = hwnd

    @property
    def minimized(self) -> bool:
        return bool(win32gui.IsIconic(self.hwnd))

    @property
    def size(self) -> tuple[int, int]:
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.refresh()
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        return right - left, bottom - top

    def capture(self) -> np.ndarray:
        """Return the client area as an RGB numpy array (h, w, 3)."""
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.refresh()
        if self.minimized:
            raise GameWindowError("Game window is minimized; restore it (it may stay behind other windows).", "minimized")
        w, h = self.size
        hdc = win32gui.GetWindowDC(self.hwnd)
        mfc = win32ui.CreateDCFromHandle(hdc)
        mem = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        try:
            bmp.CreateCompatibleBitmap(mfc, w, h)
            mem.SelectObject(bmp)
            if not ctypes.windll.user32.PrintWindow(self.hwnd, mem.GetSafeHdc(), PW_CLIENTONLY_RENDERFULL):
                raise GameWindowError("PrintWindow failed", "capture_failed")
            raw = bmp.GetBitmapBits(True)
            img = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
            return img[:, :, 2::-1].copy()  # BGRA -> RGB
        finally:
            win32gui.DeleteObject(bmp.GetHandle())
            mem.DeleteDC()
            mfc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hdc)

    # --- input -----------------------------------------------------------------

    def _post(self, msg: int, wparam: int, x: int, y: int) -> None:
        win32gui.PostMessage(self.hwnd, msg, wparam, win32api.MAKELONG(x & 0xFFFF, y & 0xFFFF))

    def move(self, x: int, y: int) -> None:
        self._post(win32con.WM_MOUSEMOVE, 0, x, y)

    def click(self, x: int, y: int, settle: float = 0.05) -> None:
        """Left-click at client coordinates."""
        x, y = int(x), int(y)
        self.move(x, y)
        time.sleep(settle)
        self._post(win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, x, y)
        time.sleep(settle)
        self._post(win32con.WM_LBUTTONUP, 0, x, y)

    def scroll(self, x: int, y: int, notches: int) -> None:
        """Scroll at client coordinates. Negative notches scroll down."""
        x, y = int(x), int(y)
        self.move(x, y)
        time.sleep(0.03)
        sx, sy = win32gui.ClientToScreen(self.hwnd, (x, y))  # WM_MOUSEWHEEL uses screen coordinates
        step = 1 if notches > 0 else -1
        for _ in range(abs(notches)):
            delta = (step * win32con.WHEEL_DELTA) & 0xFFFF
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEWHEEL, delta << 16, win32api.MAKELONG(sx & 0xFFFF, sy & 0xFFFF))
            time.sleep(0.04)

    def type_text(self, text: str) -> None:
        """Type into the focused field of the game with posted characters (no real keyboard)."""
        for ch in text:
            win32gui.PostMessage(self.hwnd, win32con.WM_CHAR, ord(ch), 1)
            time.sleep(0.07)
    def hold(self, x: float, y: float, seconds: float) -> None:
        """Press the left button at client coordinates, keep it down for `seconds`, release."""
        x, y = int(x), int(y)
        self.move(x, y)
        time.sleep(0.08)
        self._post(win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, x, y)
        end = time.time() + seconds
        while time.time() < end:  # keep the pointer "alive" on the button while holding
            time.sleep(0.25)
            self._post(win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, x, y)
        self._post(win32con.WM_LBUTTONUP, 0, x, y)

    def _key(self, vk: int, scan: int) -> None:
        win32gui.PostMessage(self.hwnd, win32con.WM_KEYDOWN, vk, 1 | (scan << 16))
        win32gui.PostMessage(self.hwnd, win32con.WM_KEYUP, vk, 1 | (scan << 16) | (0xC0 << 24))
        time.sleep(0.05)

    def clear_field(self, presses: int = 8) -> None:
        """Empty the focused text field with posted Backspace keys."""
        for _ in range(presses):
            self._key(win32con.VK_BACK, 0x0E)

    def press_enter(self) -> None:
        self._key(win32con.VK_RETURN, 0x1C)

    def press_escape(self) -> None:
        self._key(win32con.VK_ESCAPE, 0x01)

    def restore(self) -> None:
        """Bring a minimized game window back without the focus, behind every other window.

        It only has to be drawn for the bot to read it; it should not land on top of what the
        user is doing."""
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)

    def close(self) -> None:
        """Ask the game to quit (as the window's close button does), so it can save first."""
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        self.hwnd = 0

    def user_idle_seconds(self) -> float | None:
        """Seconds since the user's last real mouse or key input while the game is the window in front.

        None when another window is in front: then the user is not playing. The bot's own posted
        messages are not input to Windows, and its own real clicks (RealMouse) are left out.
        """
        from . import desktop

        front = win32gui.GetForegroundWindow()
        if not front or not self.hwnd:
            return None
        if front != self.hwnd and win32gui.GetAncestor(front, 2) != self.hwnd:  # 2 = GA_ROOT
            return None
        return desktop.user_idle_seconds()

    def real_mouse(self) -> RealMouse:
        return RealMouse(self)


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MouseInput), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_WHEEL = 0x0002, 0x0004, 0x0800


def _send_mouse(flags: int, data: int = 0) -> None:
    inp = _Input(type=0)  # INPUT_MOUSE
    inp.mi = _MouseInput(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


class RealMouse:
    """A short stretch of real mouse input on the game window (with RealMouse(win) as m: m.click(x, y)).

    For menus that ignore posted messages (crop picker, bait list). The game is lifted above
    every window while it lasts; afterwards the cursor, the window in front and the game's place
    at the bottom are put back, and the input is noted as the bot's own. Only to be used while
    the user is away (desktop.may_use_real_mouse).
    """

    def __init__(self, win: "GameWindow"):
        self.win = win

    def __enter__(self) -> "RealMouse":
        from . import desktop
        desktop.real_active = True
        self.user_tick = desktop.user_last_tick()
        self.cursor = win32api.GetCursorPos()
        self.front = win32gui.GetForegroundWindow()
        if not self.win.hwnd or not win32gui.IsWindow(self.win.hwnd):
            self.win.refresh()
        if self.win.minimized:
            self.win.restore()
        win32gui.SetWindowPos(self.win.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        time.sleep(0.3)
        return self

    def _to_screen(self, x: float, y: float) -> tuple[int, int]:
        return win32gui.ClientToScreen(self.win.hwnd, (int(round(x)), int(round(y))))

    def hover(self, x: float, y: float, settle: float = 0.6) -> None:
        sx, sy = self._to_screen(x, y)
        win32api.SetCursorPos((sx - 3, sy))  # two steps: the page reacts to movement, not to a jump
        time.sleep(0.05)
        win32api.SetCursorPos((sx, sy))
        time.sleep(settle)

    def click(self, x: float, y: float, settle: float = 0.5) -> None:
        self.hover(x, y, settle=0.15)
        _send_mouse(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.06)
        _send_mouse(MOUSEEVENTF_LEFTUP)
        time.sleep(settle)

    def scroll(self, x: float, y: float, notches: int) -> None:
        self.hover(x, y, settle=0.1)
        _send_mouse(MOUSEEVENTF_WHEEL, 120 * notches)
        time.sleep(0.3)

    def __exit__(self, *exc) -> None:
        from . import desktop
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        win32gui.SetWindowPos(self.win.hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        win32gui.SetWindowPos(self.win.hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0, flags)
        win32api.SetCursorPos(self.cursor)
        if self.front and self.front != self.win.hwnd and win32gui.IsWindow(self.front):
            try:
                win32gui.SetForegroundWindow(self.front)
            except win32gui.error:
                pass  # Windows refused the switch; the game simply stays the active window
        desktop.note_own_input(self.user_tick)
        desktop.real_active = False


class ScaledWindow:
    """The game window as the bot wants to see it: every frame resized by `factor`.

    The user can set the game's UI scale to anything. Every text height, offset and OCR setting
    in `game.py` was tuned and tested at one scale, so instead of trusting all of them at every
    scale, the frame is brought to that scale and the click positions are mapped back.
    """

    def __init__(self, win: GameWindow):
        self.raw = win
        self.factor = 1.0

    def __getattr__(self, name):  # hwnd, minimized, typing, restore, user_idle_seconds ...
        return getattr(self.raw, name)

    @property
    def size(self) -> tuple[int, int]:
        width, height = self.raw.size
        return round(width * self.factor), round(height * self.factor)

    def capture(self) -> np.ndarray:
        img = self.raw.capture()
        if abs(self.factor - 1.0) < 0.01:
            return img
        h, w = img.shape[:2]
        size = (round(w * self.factor), round(h * self.factor))
        return np.asarray(Image.fromarray(img).resize(size, Image.BICUBIC))

    def click(self, x: float, y: float, settle: float = 0.05) -> None:
        self.raw.click(round(x / self.factor), round(y / self.factor), settle=settle)

    def move(self, x: float, y: float) -> None:
        self.raw.move(round(x / self.factor), round(y / self.factor))

    def scroll(self, x: float, y: float, notches: int) -> None:
        self.raw.scroll(round(x / self.factor), round(y / self.factor), notches)

    def hold(self, x: float, y: float, seconds: float) -> None:
        self.raw.hold(round(x / self.factor), round(y / self.factor), seconds)

    def real_mouse(self) -> "_ScaledRealMouse":
        return _ScaledRealMouse(RealMouse(self.raw), self.factor)


class _ScaledRealMouse:
    def __init__(self, real: RealMouse, factor: float):
        self.real, self.factor = real, factor

    def __enter__(self) -> "_ScaledRealMouse":
        self.real.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.real.__exit__(*exc)

    def hover(self, x: float, y: float, settle: float = 0.6) -> None:
        self.real.hover(x / self.factor, y / self.factor, settle)

    def click(self, x: float, y: float, settle: float = 0.5) -> None:
        self.real.click(x / self.factor, y / self.factor, settle)

    def scroll(self, x: float, y: float, notches: int) -> None:
        self.real.scroll(x / self.factor, y / self.factor, notches)
