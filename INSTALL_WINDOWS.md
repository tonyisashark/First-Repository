# Windows GUI — install & build

The bot ships with a desktop GUI (`Dashboard` + `Settings` tabs) and a build
pipeline that produces a Windows installer. The installer drops a **Start Menu
entry** (so the app shows up in Windows Search) and an optional **Desktop
shortcut**, and registers an uninstaller under *Apps & features*.

## Easiest: download a prebuilt installer (GitHub Actions)

You don't need to build anything yourself — a GitHub Actions workflow builds the
installer on Windows for you:

1. Open the repository on GitHub → **Actions** tab.
2. Click the most recent **"Build Windows installer"** run (or press **Run
   workflow** to start one).
3. Wait for it to finish (green check), then scroll to **Artifacts** and download
   **`KalshiTempBot-Windows`**.
4. Unzip it and run **`KalshiTempBotSetup.exe`**.

(The zip also contains `KalshiTempBot.exe`, the standalone app, if you'd rather
skip the installer.)

## For end users (installed app)

1. Run **`KalshiTempBotSetup.exe`**.
2. Leave *Create a desktop icon* checked if you want one, and finish the wizard.
3. Launch from the **Desktop shortcut** or by pressing **⊞ Win** and typing
   *"Kalshi"* — the Start Menu entry makes it searchable.
4. In the app's **Settings** tab, choose your environment, (optionally) enter
   your API Key ID + private-key path, then **Save settings**.
   - It starts in **Paper (dry run)** mode — no real orders — so you can watch
     the strategy first.
   - To trade for real: uncheck *Dry run*, pick `prod`, provide credentials, and
     confirm the warning prompt.
5. On the **Dashboard** tab press **Start**.

Settings are stored per-user at `%APPDATA%\KalshiTempBot\.env` (the app never
writes secrets into its Program Files folder).

No separate Python install is required — the executable is self-contained.

## For builders (produce the installer)

**Prerequisites**
- Python 3.9+ from [python.org](https://www.python.org/downloads/windows/)
  (its installer bundles Tkinter). Tick *"Add python.exe to PATH"*.
- [Inno Setup 6](https://jrsoftware.org/isdl.php) (only needed for the installer
  step; the standalone `.exe` builds without it).

**Build** — from the repository root in a Command Prompt:

```bat
build_windows.bat
```

This will:
1. install runtime + build dependencies,
2. generate `assets\icon.ico`,
3. build the standalone GUI with PyInstaller → `dist\KalshiTempBot.exe`,
4. compile the installer with Inno Setup → `installer\Output\KalshiTempBotSetup.exe`.

If Inno Setup isn't installed, step 4 is skipped and you can still distribute
`dist\KalshiTempBot.exe` directly.

### Manual steps (equivalent to the script)

```bat
python -m pip install -r requirements.txt -r requirements-build.txt
python assets\make_icon.py
python -m PyInstaller --noconfirm --clean kalshi_temp_bot.spec
"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" installer\kalshi_temp_bot.iss
```

## Run the GUI from source (no build)

Useful during development on any OS with Tkinter:

```bash
python -m kalshi_temp_bot gui
```

## What makes it "searchable in Windows"

The installer creates a Start Menu shortcut (`[Icons]` →
`{autoprograms}\Kalshi Temperature Bot`). Windows Search indexes Start Menu
shortcuts, so typing the app name in the Start menu finds it. The registered
uninstaller also makes it appear in *Settings → Apps*.

## Files involved

```
gui_app.py                     PyInstaller entry point (launches the GUI)
kalshi_temp_bot/gui.py         the Tkinter application
kalshi_temp_bot.spec           PyInstaller build spec (windowed, bundles icon)
installer/kalshi_temp_bot.iss  Inno Setup installer script (shortcuts + uninstall)
assets/make_icon.py            generates assets/icon.ico (no dependencies)
build_windows.bat              one-shot build: exe + installer
requirements-build.txt         build-time deps (PyInstaller)
```
