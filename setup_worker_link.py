"""Lets the dashboard open the plan worker on this laptop.

Registers a "training-worker://" link type for your Windows user only (no admin
needed), pointing at start_worker.bat in this folder. When you choose "My laptop" on
the dashboard, the browser asks to open it, and the worker reads that one plan and closes.
Nothing runs in the background.

  python setup_worker_link.py           set it up (run again if you move this folder)
  python setup_worker_link.py --remove  undo it
"""
import sys
import winreg
from pathlib import Path

KEY = r"Software\Classes\training-worker"
BAT = Path(__file__).resolve().parent / "start_worker.bat"


def remove():
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", r"\DefaultIcon", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY + sub)
        except FileNotFoundError:
            pass
    print("Removed the training-worker:// link.")


def setup():
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:Training plan worker")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY + r"\shell\open\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f'cmd.exe /c ""{BAT}" "%1""')
    print(f"Set up: training-worker:// now opens {BAT}")


if __name__ == "__main__":
    remove() if "--remove" in sys.argv else setup()
