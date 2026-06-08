@echo off
REM ===================================================================
REM  Build the Kalshi Temperature Bot Windows executable + installer.
REM  Run from the repository root in a Command Prompt:  build_windows.bat
REM
REM  Prerequisites:
REM    * Python 3.9+ (python.org build, which includes Tkinter) on PATH
REM    * Inno Setup 6 (https://jrsoftware.org/isdl.php) for the installer step
REM ===================================================================
setlocal enabledelayedexpansion
echo === Kalshi Temperature Bot : Windows build ===

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install it from https://python.org
    exit /b 1
)

echo [1/4] Installing dependencies...
python -m pip install --upgrade pip || exit /b 1
python -m pip install -r requirements.txt -r requirements-build.txt || exit /b 1

echo [2/4] Generating application icon...
python assets\make_icon.py

echo [3/4] Building the executable with PyInstaller...
python -m PyInstaller --noconfirm --clean kalshi_temp_bot.spec || exit /b 1
echo      Standalone executable: dist\KalshiTempBot.exe

echo [4/4] Building the installer with Inno Setup...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo [WARN] Inno Setup 6 not found.
    echo        Install it from https://jrsoftware.org/isdl.php to build the installer,
    echo        or just distribute the standalone exe at dist\KalshiTempBot.exe
    exit /b 0
)
"%ISCC%" installer\kalshi_temp_bot.iss || exit /b 1
echo.
echo === Done. Installer written to installer\Output\KalshiTempBotSetup.exe ===
endlocal
