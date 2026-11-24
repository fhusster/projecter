"""
Native-messaging helper for the Projecter browser extension.

The browser runs this when the extension asks; it starts Projecter in the background
(no window) if it isn't already running, then replies. Registered by install_extension.py.
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
APP = os.path.join(APP_DIR, "projecter.py")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    return json.loads(sys.stdin.buffer.read(n) or b"{}")


def send(obj):
    data = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


def running():
    try:
        socket.create_connection(("127.0.0.1", 8765), 0.5).close()
        return True
    except OSError:
        return False


def start():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    cmd = [pythonw, APP, "--no-browser"]
    try:
        # The browser puts this helper in a job that's killed when it exits; break away from it
        # so Projecter keeps running.
        subprocess.Popen(cmd, cwd=APP_DIR, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        # Breaking away isn't allowed: have Windows (WMI) start it instead, outside the job.
        line = subprocess.list2cmdline(cmd).replace("'", "''")
        ps = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
              f"@{{CommandLine='{line}'; CurrentDirectory='{APP_DIR}'}}")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)


def main():
    msg = read_message() or {}
    if msg.get("cmd") == "start":
        if not running():
            start()
            for _ in range(40):
                if running():
                    break
                time.sleep(0.25)
        send({"ok": running()})
    else:
        send({"ok": True, "running": running()})


if __name__ == "__main__":
    main()
