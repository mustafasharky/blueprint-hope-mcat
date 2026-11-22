"""User-directed login in ordinary Windows Chrome; no debugger or automation during authentication."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path


def browser_executable(channel):
    import winreg

    name = "chrome.exe" if channel == "chrome" else "msedge.exe"
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as handle:
                candidate = Path(winreg.QueryValueEx(handle, None)[0])
                if candidate.is_file():
                    return candidate
        except OSError:
            pass
    relative = "Google/Chrome/Application/chrome.exe" if channel == "chrome" else "Microsoft/Edge/Application/msedge.exe"
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root and (candidate := Path(root) / relative).is_file():
            return candidate
    raise ValueError("Installed browser not found. Install Chrome or choose --channel msedge.")


class OwnedWindows:
    """Read only top-level titles belonging to the process this run created."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        self.ctypes, self.w = ctypes, w
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.api.EnumWindows.argtypes = [self.callback, w.LPARAM]
        self.api.EnumWindows.restype = w.BOOL
        self.api.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.api.GetWindowThreadProcessId.restype = w.DWORD
        self.api.IsWindowVisible.argtypes = [w.HWND]
        self.api.IsWindowVisible.restype = w.BOOL
        self.api.GetWindowTextLengthW.argtypes = [w.HWND]
        self.api.GetWindowTextLengthW.restype = ctypes.c_int
        self.api.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.api.GetWindowTextW.restype = ctypes.c_int
        self.api.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self.api.PostMessageW.restype = w.BOOL

    def belongs_to(self, hwnd, pid):
        owner = self.w.DWORD()
        self.api.GetWindowThreadProcessId(hwnd, self.ctypes.byref(owner))
        return owner.value == pid

    def titles(self, pid):
        found = []

        @self.callback
        def visit(hwnd, _):
            if self.belongs_to(hwnd, pid) and self.api.IsWindowVisible(hwnd):
                size = self.api.GetWindowTextLengthW(hwnd)
                text = self.ctypes.create_unicode_buffer(size + 1)
                self.api.GetWindowTextW(hwnd, text, size + 1)
                found.append((hwnd, text.value))
            return True

        self.api.EnumWindows(visit, 0)
        return found

    def close_report_window(self, hwnd, pid):
        if not self.belongs_to(hwnd, pid):
            raise ValueError("The login window changed ownership; close the dedicated browser manually.")
        if not self.api.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE: normal graceful close, never force-kill.
            raise ValueError("Could not close the login window; close the dedicated browser manually.")


def is_portal_title(title):
    # Observed account/dashboard/report titles. This triggers handoff, not authorization;
    # the API response is independently checked after Playwright starts.
    return (title.startswith("My Account - ") or
            (title.startswith("Blueprint MCAT") and
             (" - Score Report - " in title or " - Dashboard - " in title)))


async def wait_for_handoff(process, windows, timeout, progress, poll=0.5, stable_polls=6):
    deadline = asyncio.get_running_loop().time() + timeout
    candidate, matches, saw_window, closing = None, 0, False, False
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            if not saw_window:
                raise ValueError("The dedicated browser is already open or did not start. Close its other window and retry.")
            return  # User closure also triggers a session check; it never proves successful authentication.
        owned = windows.titles(process.pid)
        saw_window = saw_window or bool(owned)
        current = next((hwnd for hwnd, title in owned if is_portal_title(title)), None)
        if current is not None and not closing:
            matches = matches + 1 if candidate == current else 1
            candidate = current
            if matches >= stable_polls:
                progress.update("login_handoff", "Blueprint sign-in completed. Saving the browser session and starting capture...")
                windows.close_report_window(current, process.pid)
                closing = True
        elif current is None:
            candidate, matches = None, 0
        await asyncio.sleep(poll)
    raise ValueError("Login handoff timed out. The normal browser was left open. Close it when finished, then rerun.")


async def normal_login(profile, channel, timeout, progress):
    if os.name != "nt":
        raise ValueError("The normal-browser login handoff currently requires Windows.")
    executable = browser_executable(channel)
    profile.mkdir(parents=True, exist_ok=True)
    command = [str(executable), f"--user-data-dir={profile.resolve()}", "--new-window",
               "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
               "https://blueprintprep.com/lms/access/redirect/test/mcat/"]
    progress.update("waiting_for_login", "Opening a normal browser for sign-in. Complete login yourself; capture starts when Blueprint opens. If it stays open, close this dedicated window after signing in.")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # No remote-debugging, webdriver, automation-hiding, or security-disabling flags.
    await wait_for_handoff(process, OwnedWindows(), timeout, progress)
