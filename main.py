"""Start VideoExpress AutoCreator: local server + an app-style window (Edge/Chrome --app), falling back to the default browser."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("SCRIPTPILOT_PORT", "8790"))


def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _app_window(url: str):
    candidates = []
    if sys.platform == "win32":
        pf = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
    for name in ("msedge", "google-chrome", "chrome", "chromium"):
        w = shutil.which(name)
        if w:
            candidates.append(Path(w))
    profile = Path.home() / "Documents" / "VideoExpress AutoCreator" / "window-profile"
    for exe in candidates:
        if exe and exe.exists():
            try:
                subprocess.Popen([str(exe), f"--app={url}", f"--user-data-dir={profile}", "--window-size=1280,900"])
                return
            except OSError:
                continue
    webbrowser.open(url)


def _running_version(port: int) -> str:
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=3) as r:
            return json.loads(r.read().decode()).get("version", "")
    except Exception:
        return ""


def main():
    from . import __version__
    from .project import KEYS_FILE, ensure_keys_file, load_settings
    url = f"http://127.0.0.1:{PORT}/"
    open_window = os.environ.get("SCRIPTPILOT_NO_WINDOW") != "1"
    if _port_in_use(PORT):
        if _running_version(PORT) == __version__:
            if open_window:
                print("VideoExpress AutoCreator is already running - opening its window.")
                _app_window(url)
            else:
                print("VideoExpress AutoCreator is already running.")
            return
        print(f"An older VideoExpress AutoCreator is still running on port {PORT}. Close its black window (or restart the PC) and try again.")
        input("Press Enter to exit...")
        return
    ensure_keys_file()
    s = load_settings()
    key_ok = bool(s.get({"groq": "groq_key", "gemini": "gemini_key", "claude": "anthropic_key"}.get(s["provider"], "x"), "ok"))
    print(f"VideoExpress AutoCreator v{__version__}  |  AI: {s['provider']}  |  keys file: {KEYS_FILE}")
    if not key_ok:
        print(f"  ! No key found for {s['provider']}. Add it to the keys file (or in Settings).")
    from .server import app
    if open_window:
        threading.Thread(target=lambda: (time.sleep(1.2), _app_window(url)), daemon=True).start()
    print(f"Running at {url}  (keep this window open; close it to quit)")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
