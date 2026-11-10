# Building the Windows installer with GitHub (no Windows PC or Python needed)

GitHub builds `RelayCRM-Setup.exe` on a Windows machine in its cloud for free, every time code is pushed.

## One-time setup (about 5 minutes)

1. Sign in at https://github.com and click **+ → New repository**.
   * **Name:** `relay-crm`
   * **Visibility:** **Private**
   * Click **Create repository**.
2. Upload the code. Choose one of these:
   * **Easiest:** on the empty repository page, click **uploading an existing file**. Drag in *everything inside* the unzipped `relay-crm` folder, including the hidden `.github` folder. Then click **Commit changes**.
     * Windows hides folders that start with a dot. In File Explorer, turn on **View → Show → Hidden items** so you can see `.github`.
   * **Or** connect GitHub to Claude and let Claude push it for you.
3. Click the **Actions** tab. If GitHub asks, click **"I understand my workflows, go ahead and enable them"**.

## Getting the installer

1. In **Actions**, open the newest **Build Windows desktop app** run. It takes about 6 to 8 minutes and needs a green ✓.
2. Scroll to **Artifacts** and download **RelayCRM-Setup-1.0.0**. You get a zip that contains the `.exe`.
3. Run the `.exe` on any Windows 10 or 11 PC.

To rebuild by hand, go to **Actions → Build Windows desktop app → Run workflow**.

## Sharing a permanent download link (optional)

1. Go to **Releases → Draft a new release**.
2. Create the tag `v1.0.0` and click **Publish**.
3. The workflow attaches the installer and the portable zip to that release. Anyone with access to the repository can download them from there.

## Building locally instead

On a Windows PC with Python 3.12 and Inno Setup 6, double-click `build_installer.bat`.
