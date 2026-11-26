"""
Installs Projecter's own channel (the roku/ folder) on a Roku TV. With it, subtitles are
drawn by the Roku, so changing them in Projecter shows up instantly instead of restarting.

One-time Roku setup (Developer Mode):
  1. On the Roku remote press: Home x3, Up x2, Right, Left, Right, Left, Right.
  2. Choose "Enable installer and restart", accept, and pick a password. The Roku restarts.
Then run:  python install_roku_channel.py <roku-ip> <password>
(The IP and password are remembered in config.json, so later updates are just
 python install_roku_channel.py)
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "roku")
CONFIG_PATH = os.path.join(HERE, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def package():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(SRC):
            for name in files:
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, SRC).replace(os.sep, "/"))
    path = os.path.join(tempfile.gettempdir(), "projecter-roku.zip")
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return path


def main():
    cfg = load_config()
    saved = cfg.get("roku_dev") or {}
    ip = sys.argv[1] if len(sys.argv) > 1 else saved.get("ip")
    password = sys.argv[2] if len(sys.argv) > 2 else saved.get("password")
    if not ip or not password:
        sys.exit(__doc__)
    zip_path = package()
    # The Roku's installer uses HTTP digest login (user "rokudev"); curl ships with Windows.
    out = subprocess.run(["curl", "-sS", "--digest", "-u", f"rokudev:{password}", "--max-time", "60",
                          "-F", "mysubmit=Install", "-F", f"archive=@{zip_path}",
                          f"http://{ip}/plugin_install"], capture_output=True, text=True)
    page = out.stdout + out.stderr
    if out.returncode or re.search(r"401|Unauthorized", page):
        sys.exit("Couldn't reach the Roku installer. Is Developer Mode on, and the password right?\n"
                 + page[-300:])
    msgs = re.findall(r'"text":"([^"]+)"', page) or re.findall(r"<font[^>]*>([^<]+)</font>", page)
    if not any(re.search(r"Install Success|Identical to previous", m) for m in msgs):
        sys.exit("The Roku didn't accept the channel:\n" + ("\n".join(msgs) or page[-500:]))
    cfg["roku_dev"] = {"ip": ip, "password": password}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print("Projecter channel installed on the Roku. " + "; ".join(msgs))


if __name__ == "__main__":
    main()
