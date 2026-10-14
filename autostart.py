"""Make Holdco start by itself (so your companies keep running after a reboot).

    python autostart.py install     # Windows: starts hidden at login. Linux: systemd user service.
    python autostart.py uninstall
    python autostart.py status
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = HERE / "run.py"


# ---------------- Windows: a hidden launcher in the Startup folder ----------------

def _win_startup() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Holdco.vbs"


def win_install() -> None:
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else py   # window style 0 hides the console either way
    vbs = _win_startup()
    vbs.write_text(
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{HERE}"\r\n'
        f'sh.Run """{exe}"" ""{RUN}"" --no-browser", 0, False\r\n',
        encoding="utf-8",
    )
    subprocess.Popen(["wscript", str(vbs)])
    print(f"Done. Holdco now starts in the background whenever you log in ({vbs}).")
    print("It's starting now too: open http://127.0.0.1:8420 in a few seconds.")
    print("Tip: set Windows Settings > System > Power > Sleep to 'Never' when plugged in, "
          "so agents keep working overnight.")


def win_uninstall() -> None:
    vbs = _win_startup()
    if vbs.exists():
        vbs.unlink()
        print("Removed. Holdco won't start at login anymore. (If it's running now, stop it from Task Manager: "
              "the python process running run.py.)")
    else:
        print("Autostart wasn't installed.")


# ---------------- Linux: systemd user service ----------------

UNIT = Path.home() / ".config" / "systemd" / "user" / "holdco.service"


def _systemctl(*args: str) -> int:
    return subprocess.call(["systemctl", "--user", *args])


def linux_install() -> None:
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(f"""[Unit]
Description=Holdco - AI-run companies on your GPU
After=network-online.target

[Service]
WorkingDirectory={HERE}
ExecStart={sys.executable} {RUN} --no-browser
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
""", encoding="utf-8")
    _systemctl("daemon-reload")
    code = _systemctl("enable", "--now", "holdco.service")
    if code != 0:
        sys.exit("systemctl failed. Is systemd running for your user session?")
    print(f"Done. Holdco runs as a user service ({UNIT}).")
    print("Open http://127.0.0.1:8420   Logs: journalctl --user -u holdco -f")
    print(f"To keep it running when you're logged out, run once:  sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")


def linux_uninstall() -> None:
    _systemctl("disable", "--now", "holdco.service")
    if UNIT.exists():
        UNIT.unlink()
    _systemctl("daemon-reload")
    print("Removed the Holdco service.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    windows = os.name == "nt"
    if cmd == "install":
        win_install() if windows else linux_install()
    elif cmd == "uninstall":
        win_uninstall() if windows else linux_uninstall()
    elif cmd == "status":
        if windows:
            print("Installed." if _win_startup().exists() else "Not installed.")
        else:
            _systemctl("status", "holdco.service", "--no-pager")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
