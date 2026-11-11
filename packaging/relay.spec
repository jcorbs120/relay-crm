# PyInstaller build recipe for the Windows desktop app.
# Run from the repository root:  pyinstaller --noconfirm packaging/relay.spec
import os
from PyInstaller.utils.hooks import collect_all

root = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [
    (os.path.join(root, "relay", "templates"), "relay/templates"),
    (os.path.join(root, "relay", "static"), "relay/static"),
]
binaries, hiddenimports = [], ["relay.app", "msal", "openpyxl", "clr"]
for pkg in ("webview",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis([os.path.join(root, "desktop.py")], pathex=[root], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=["tkinter", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="RelayCRM", console=False,
          icon=os.path.join(root, "packaging", "icon.ico"), upx=False,
          version=os.path.join(root, "packaging", "version_info.txt"))
coll = COLLECT(exe, a.binaries, a.datas, name="RelayCRM", upx=False)
