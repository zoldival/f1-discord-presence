@echo off
REM Builds dist\F1RichPresence.exe
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --onefile --noconsole ^
  --name F1RichPresence ^
  --icon assets\icon.ico ^
  --add-data "assets\icon.ico;assets" ^
  --hidden-import pystray._win32 ^
  f1_presence.py || exit /b 1
echo.
echo Done: dist\F1RichPresence.exe
