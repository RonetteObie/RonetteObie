# time_logger.py — BVI Time Logger
# Logs the active window every few seconds to a daily CSV, for building your own timesheet.
# Runs in the system tray. Settings are saved per user in %APPDATA%\BVI Time Logger.

import csv
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime

APP_NAME = "BVI Time Logger"
APP_VERSION = "1.0.0"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
ERROR_LOG_PATH = os.path.join(SETTINGS_DIR, "errors.txt")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_NAME = "Local\\BVI_TimeLogger_SingleInstance"

DEFAULT_SETTINGS = {
    "log_folder": "",
    "interval_seconds": 15,
    "idle_minutes": 5,
    "start_with_windows": True,
}

CSV_HEADER = ["Timestamp", "Date", "Seconds", "Status", "Project", "App", "Window Title", "PC Name", "User"]

# BVI project number: 5-digit client number, dot, 2-digit sequence. E.g. 35515.00, 33236.19
PROJECT_PATTERN = re.compile(r"(?<![\d.])(\d{5}\.\d{2})(?![\d])")

PC_NAME = os.environ.get("COMPUTERNAME", "UNKNOWN_PC")
USER_NAME = os.environ.get("USERNAME", "UNKNOWN_USER")


# === HELPERS ===

def log_error(message):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} — {message}\n")
    except OSError:
        pass


def find_project(window_title):
    match = PROJECT_PATTERN.search(window_title or "")
    return match.group(1) if match else ""


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        return None
    if not settings.get("log_folder"):
        return None
    return settings


def save_settings(settings):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def launch_command():
    # When packaged as .exe, sys.executable is the exe. When run as a script, use pythonw so no console shows.
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def set_start_with_windows(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# === WINDOWS CALLS ===

def setup_win_api():
    # Set handle return types so 64-bit handles aren't cut short.
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetTickCount.restype = wintypes.DWORD


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    millis = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return millis / 1000.0


def screen_locked():
    # OpenInputDesktop fails while the lock screen (or a UAC prompt) is showing.
    user32 = ctypes.windll.user32
    desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desktop:
        return True
    try:
        return not user32.SwitchDesktop(desktop)
    finally:
        user32.CloseDesktop(desktop)


def active_window():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""

    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    title = buffer.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                app = os.path.basename(path.value)
        finally:
            kernel32.CloseHandle(handle)
    return app, title


def already_running():
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


# === LOGGER ===

class Logger:
    def __init__(self, settings):
        self.settings = settings
        self.paused = False
        self.stop_event = threading.Event()
        self.pending_rows = []  # kept here if the CSV can't be written (e.g. open in Excel, drive offline)
        self.last_status = "Starting"

    def daily_path(self, day):
        return os.path.join(self.settings["log_folder"], f"TimeLog_{PC_NAME}_{day}.csv")

    def take_sample(self):
        now = datetime.now()
        interval = int(self.settings["interval_seconds"])
        app, title, project = "", "", ""

        try:
            if screen_locked():
                status = "Locked"
            elif idle_seconds() >= float(self.settings["idle_minutes"]) * 60:
                status = "Idle"
            else:
                status = "Active"
                app, title = active_window()
                project = find_project(title)
        except Exception as e:
            status = "Error"
            title = str(e)

        return now.strftime("%Y-%m-%d"), [
            now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"), interval,
            status, project, app, title, PC_NAME, USER_NAME,
        ]

    def write_pending(self):
        still_pending = []
        for day, row in self.pending_rows:
            path = self.daily_path(day)
            try:
                new_file = not os.path.exists(path)
                with open(path, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
                    writer = csv.writer(f)
                    if new_file:
                        writer.writerow(CSV_HEADER)
                    writer.writerow(row)
            except OSError as e:
                still_pending.append((day, row))
                if len(still_pending) == 1:
                    log_error(f"Could not write to {path}: {e}")
        self.pending_rows = still_pending

    def run(self):
        os.makedirs(self.settings["log_folder"], exist_ok=True)
        while not self.stop_event.is_set():
            if not self.paused:
                day, row = self.take_sample()
                self.pending_rows.append((day, row))
                self.write_pending()
                self.last_status = f"{row[3]} {row[4]}".strip() + f" — {row[0][11:]}"
                if self.pending_rows:
                    self.last_status += f" ({len(self.pending_rows)} waiting to save)"
            self.stop_event.wait(int(self.settings["interval_seconds"]))
        self.write_pending()


# === SETTINGS WINDOW ===

def settings_window(root, current, first_run):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    result = {}
    win = tk.Toplevel(root)
    win.title(f"{APP_NAME} — Settings")
    win.resizable(False, False)
    win.attributes("-topmost", True)

    folder_var = tk.StringVar(value=current.get("log_folder", ""))
    interval_var = tk.StringVar(value=str(current.get("interval_seconds", 15)))
    idle_var = tk.StringVar(value=str(current.get("idle_minutes", 5)))
    startup_var = tk.BooleanVar(value=current.get("start_with_windows", True))

    frame = ttk.Frame(win, padding=16)
    frame.grid()

    intro = "Welcome! Pick where your time logs should be saved." if first_run else "Change your settings."
    ttk.Label(frame, text=intro).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    ttk.Label(frame, text="Save logs in:").grid(row=1, column=0, sticky="w")
    ttk.Entry(frame, textvariable=folder_var, width=50).grid(row=1, column=1, padx=6)

    def browse():
        folder = filedialog.askdirectory(parent=win, initialdir=folder_var.get() or os.path.expanduser("~"))
        if folder:
            folder_var.set(os.path.normpath(folder))

    ttk.Button(frame, text="Browse...", command=browse).grid(row=1, column=2)

    ttk.Label(frame, text="Log every (seconds):").grid(row=2, column=0, sticky="w", pady=6)
    ttk.Combobox(frame, textvariable=interval_var, values=["15", "30", "60"], width=8).grid(
        row=2, column=1, sticky="w", padx=6)

    ttk.Label(frame, text="Idle after (minutes):").grid(row=3, column=0, sticky="w")
    ttk.Combobox(frame, textvariable=idle_var, values=["2", "5", "10", "15"], width=8).grid(
        row=3, column=1, sticky="w", padx=6)

    ttk.Checkbutton(frame, text="Start automatically when I log in to Windows", variable=startup_var).grid(
        row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def save():
        folder = folder_var.get().strip()
        try:
            interval = int(interval_var.get())
            idle = float(idle_var.get())
            if interval < 5 or idle <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Interval must be 5 seconds or more, and idle time more than 0.", parent=win)
            return
        if not folder:
            messagebox.showerror(APP_NAME, "Please pick a folder to save the logs in.", parent=win)
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Can't use that folder:\n{e}", parent=win)
            return
        result.update(log_folder=folder, interval_seconds=interval, idle_minutes=idle,
                      start_with_windows=startup_var.get())
        win.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(16, 0))
    ttk.Button(buttons, text="Cancel", command=win.destroy).grid(row=0, column=0, padx=6)
    ttk.Button(buttons, text="Save", command=save).grid(row=0, column=1)

    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.focus_force()
    root.wait_window(win)
    return result or None


def apply_settings(settings):
    save_settings(settings)
    try:
        set_start_with_windows(settings["start_with_windows"])
    except OSError as e:
        log_error(f"Could not update start with Windows: {e}")


# === TRAY ICON ===

def make_icon_image():
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(0, 84, 147), outline=(255, 255, 255), width=3)
    draw.line((32, 32, 32, 14), fill=(255, 255, 255), width=5)
    draw.line((32, 32, 46, 38), fill=(255, 255, 255), width=5)
    return image


def main():
    import tkinter as tk
    from tkinter import messagebox
    import pystray

    setup_win_api()
    root = tk.Tk()
    root.withdraw()

    if already_running():
        messagebox.showinfo(APP_NAME, f"{APP_NAME} is already running.\nLook for the clock icon next to the Windows clock.")
        return

    settings = load_settings()
    if settings is None:
        settings = settings_window(root, DEFAULT_SETTINGS, first_run=True)
        if settings is None:
            return
        apply_settings(settings)

    logger = Logger(settings)
    worker = threading.Thread(target=logger.run, daemon=True)
    worker.start()

    # Tray clicks come in on the tray thread; hand them to the Tk thread through this queue.
    actions = queue.Queue()

    def status_text(item):
        return "Paused" if logger.paused else logger.last_status

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open log folder", lambda: actions.put("open")),
        pystray.MenuItem(lambda item: "Resume" if logger.paused else "Pause", lambda: actions.put("pause")),
        pystray.MenuItem("Settings...", lambda: actions.put("settings")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda: actions.put("exit")),
    )
    icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, menu)
    icon.run_detached()

    def handle_actions():
        nonlocal logger, worker
        try:
            while True:
                action = actions.get_nowait()
                if action == "open":
                    os.startfile(logger.settings["log_folder"])
                elif action == "pause":
                    logger.paused = not logger.paused
                    icon.update_menu()
                elif action == "settings":
                    new_settings = settings_window(root, logger.settings, first_run=False)
                    if new_settings:
                        apply_settings(new_settings)
                        logger.stop_event.set()
                        worker.join(timeout=5)
                        logger = Logger(new_settings)
                        worker = threading.Thread(target=logger.run, daemon=True)
                        worker.start()
                elif action == "exit":
                    logger.stop_event.set()
                    worker.join(timeout=5)
                    icon.stop()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        tooltip = f"{APP_NAME} — {status_text(None)}"[:120]
        if icon.title != tooltip:
            icon.title = tooltip
        root.after(250, handle_actions)

    root.after(250, handle_actions)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(f"Crashed: {e!r}")
        raise
