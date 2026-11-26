"""What the desktop is doing: is the user away, is the screen locked, is media playing?

Used to decide whether the bot may use the real mouse in the foreground. Everything here only
reads state; nothing is changed. Audio is looked at per application (Core Audio sessions through
plain ctypes COM calls), so the game's own music does not count as "media playing".
"""
import ctypes
import ctypes.wintypes as wt
import time

import win32process

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ole32 = ctypes.windll.ole32


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def last_input_tick() -> int:
    """GetTickCount value of the last mouse or key input anywhere on the system."""
    info = _LastInput()
    info.cbSize = ctypes.sizeof(info)
    user32.GetLastInputInfo(ctypes.byref(info))
    return info.dwTime


def idle_seconds() -> float:
    """Seconds since the last mouse or key input anywhere on the system."""
    return ((kernel32.GetTickCount() - last_input_tick()) & 0xFFFFFFFF) / 1000


def screen_locked() -> bool:
    """True while the lock screen or the screen saver is up: then real input goes nowhere."""
    running = wt.BOOL()
    user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0)  # SPI_GETSCREENSAVERRUNNING
    if running.value:
        return True
    desk = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not desk:
        return True  # no access to the input desktop: the secure (lock) desktop has it
    try:
        name = ctypes.create_unicode_buffer(64)
        user32.GetUserObjectInformationW(desk, 2, name, ctypes.sizeof(name), None)  # UOI_NAME
        return name.value.lower() != "default"
    finally:
        user32.CloseDesktop(desk)


# --- Core Audio, through the COM vtables -------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "GUID":
        g = cls()
        ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(g))
        return g


CLSID_MMDeviceEnumerator = GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioSessionManager2 = GUID.of("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAudioSessionControl2 = GUID.of("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_IAudioMeterInformation = GUID.of("C02216F6-8C67-4B5B-9D00-D008E73E0064")
CLSCTX_ALL = 0x17


def _call(obj: ctypes.c_void_p, index: int, restype, *args):
    """Call method `index` of a COM object's vtable."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    # byref(...) has no ctypes type of its own: it travels as a plain pointer.
    kinds = [ctypes.c_void_p if type(a).__name__ == "CArgObject" else type(a) for a in args]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *kinds)
    return proto(vtable[index])(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        _call(obj, 2, ctypes.c_ulong)


def _sessions() -> list[tuple[int, float]]:
    """(process id, current peak level 0..1) of every audio session on the default output."""
    out = []
    ole32.CoInitialize(None)
    enum = ctypes.c_void_p()
    if ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                              ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum)):
        return out
    device = manager = sessions = ctypes.c_void_p()
    try:
        device = ctypes.c_void_p()
        if _call(enum, 4, ctypes.c_long, ctypes.c_int(0), ctypes.c_int(1), ctypes.byref(device)):  # eRender, eMultimedia
            return out
        manager = ctypes.c_void_p()
        if _call(device, 3, ctypes.c_long, ctypes.byref(IID_IAudioSessionManager2), ctypes.c_ulong(CLSCTX_ALL),
                 ctypes.c_void_p(), ctypes.byref(manager)):
            return out
        sessions = ctypes.c_void_p()
        if _call(manager, 5, ctypes.c_long, ctypes.byref(sessions)):  # GetSessionEnumerator
            return out
        count = ctypes.c_int()
        _call(sessions, 3, ctypes.c_long, ctypes.byref(count))
        for i in range(count.value):
            control = ctypes.c_void_p()
            if _call(sessions, 4, ctypes.c_long, ctypes.c_int(i), ctypes.byref(control)):
                continue
            control2, meter = ctypes.c_void_p(), ctypes.c_void_p()
            try:
                pid = ctypes.c_ulong()
                if not _call(control, 0, ctypes.c_long, ctypes.byref(IID_IAudioSessionControl2), ctypes.byref(control2)):
                    _call(control2, 14, ctypes.c_long, ctypes.byref(pid))  # GetProcessId
                peak = ctypes.c_float()
                if not _call(control, 0, ctypes.c_long, ctypes.byref(IID_IAudioMeterInformation), ctypes.byref(meter)):
                    _call(meter, 3, ctypes.c_long, ctypes.byref(peak))  # GetPeakValue
                out.append((pid.value, peak.value))
            finally:
                _release(meter)
                _release(control2)
                _release(control)
    finally:
        _release(sessions)
        _release(manager)
        _release(device)
        _release(enum)
    return out


def media_playing(ignore_pids: set[int], samples: int = 6, gap: float = 0.25) -> bool:
    """Is any application other than `ignore_pids` making sound? Sampled for about a second and a half.

    Process id 0 is the system sounds session; a beep there is not media.
    """
    for _ in range(samples):
        try:
            if any(pid not in ignore_pids and pid != 0 and peak > 0.01 for pid, peak in _sessions()):
                return True
        except OSError:
            return True  # cannot tell: assume someone is listening
        time.sleep(gap)
    return False


def window_pid(hwnd: int) -> int:
    return win32process.GetWindowThreadProcessId(hwnd)[1]


# --- the bot's own real input ----------------------------------------------------------
# Real (SendInput) clicks reset the system's last-input time just like the user's own. The tick
# of the bot's last real input is kept, so that input does not look like the user coming back.
real_active = False            # a RealMouse session is running right now
_own_tick: int | None = None   # last-input tick right after the bot's own real input
_user_tick: int | None = None  # the user's last input tick from before that


def note_own_input(user_tick_before: int) -> None:
    """Call right after the bot used the real mouse. `user_tick_before` = user_last_tick() from
    just before it started."""
    global _own_tick, _user_tick
    _own_tick = last_input_tick()
    _user_tick = user_tick_before


def user_last_tick() -> int:
    """Tick of the user's last real input, leaving out the bot's own real clicks."""
    tick = last_input_tick()
    if _own_tick is not None and _user_tick is not None and tick == _own_tick:
        return _user_tick
    return tick


def user_idle_seconds() -> float:
    """Seconds since the user's own last mouse or key input anywhere on the system."""
    return ((kernel32.GetTickCount() - user_last_tick()) & 0xFFFFFFFF) / 1000


def may_use_real_mouse(game_pid: int, idle_needed: float) -> tuple[bool, str]:
    """May the bot take the real mouse now? Only while the user is away and nothing is playing."""
    idle = user_idle_seconds()
    if idle < idle_needed:
        return False, f"user active {idle:.0f}s ago"
    if screen_locked():
        return False, "screen locked"
    if media_playing({game_pid}):
        return False, "media playing"
    return True, f"user away {idle / 60:.0f} min"
