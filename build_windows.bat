@echo off
rem Build KalshiBot.exe locally on Windows.
rem Requires Python 3.10+ from python.org (includes tkinter).

setlocal
cd /d "%~dp0"

echo === installing build dependencies ===
python -m pip install --upgrade pip || goto :error
python -m pip install . pyinstaller || goto :error

echo === generating icon ===
python assets\make_icon.py || goto :error

echo === running tests ===
python -m pip install pytest || goto :error
python -m pytest -q || goto :error

echo === building exe ===
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name KalshiBot --icon assets\icon.ico ^
  --collect-submodules kalshi_bot --hidden-import dotenv ^
  gui_app.py || goto :error

echo.
echo Done: dist\KalshiBot.exe
exit /b 0

:error
echo BUILD FAILED
exit /b 1
