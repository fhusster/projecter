"""
Registers the Projecter extension's launcher with Brave (and Chrome / Edge), so the
extension can start Projecter by itself. Run once:  python install_extension.py
Undo with:  python install_extension.py --uninstall
"""
import json
import os
import sys
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.join(HERE, "native")
NAME = "com.projecter.launcher"
BROWSERS = [r"Software\BraveSoftware\Brave-Browser", r"Software\Google\Chrome", r"Software\Microsoft\Edge"]


def main():
    manifest_path = os.path.join(NATIVE, NAME + ".json")
    if "--uninstall" in sys.argv:
        for b in BROWSERS:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{b}\NativeMessagingHosts\{NAME}")
            except OSError:
                pass
        print("Launcher unregistered.")
        return
    ext_id = open(os.path.join(NATIVE, "extension_id.txt")).read().strip()
    bat = os.path.join(NATIVE, "projecter_host.bat")
    with open(bat, "w") as f:
        f.write(f'@echo off\r\n"{sys.executable}" -u "%~dp0projecter_host.py" %*\r\n')
    with open(manifest_path, "w") as f:
        json.dump({"name": NAME, "description": "Starts Projecter for the browser extension",
                   "path": bat, "type": "stdio",
                   "allowed_origins": [f"chrome-extension://{ext_id}/"]}, f, indent=2)
    for b in BROWSERS:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{b}\NativeMessagingHosts\{NAME}")
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, manifest_path)
        winreg.CloseKey(key)
    print(f"Launcher registered for extension {ext_id}.")


if __name__ == "__main__":
    main()
