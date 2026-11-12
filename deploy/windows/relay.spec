# PyInstaller spec for Relay CRM (one-folder build -> dist\RelayCRM\RelayCRM.exe)
# Build:  pyinstaller --noconfirm deploy\windows\relay.spec   (run from the project root)
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

hidden = (collect_submodules("waitress") + collect_submodules("cheroot") + collect_submodules("msal")
          + ["crm.views_auth", "crm.views_crm", "crm.views_admin", "openpyxl.cell._writer"])
datas = [(os.path.join(ROOT, "templates"), "templates"),
         (os.path.join(ROOT, "static"), "static")] + collect_data_files("certifi")

a = Analysis([os.path.join(ROOT, "run.py")], pathex=[ROOT], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "test", "unittest", "pydoc_data"], noarchive=False)
pyz = PYZ(a.pure)
icon = os.path.join(SPECPATH, "relay.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="RelayCRM", console=True,
          icon=icon if os.path.exists(icon) else None,
          version=os.path.join(SPECPATH, "version_info.txt") if os.path.exists(os.path.join(SPECPATH, "version_info.txt")) else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="RelayCRM")
