@echo off
cd /d "%~dp0"
taskkill /f /im RecLight.exe >nul 2>nul
py -3 -m pip install -r requirements.txt
py -3 -m PyInstaller --noconsole --onefile --collect-submodules mido --hidden-import mido.backends.rtmidi --hidden-import rtmidi --hidden-import pystray._win32 --hidden-import gui --hidden-import vmidi --collect-all customtkinter --name RecLight --icon reclight.ico reclight.py
echo.
echo Hotovo: %~dp0dist\RecLight.exe
pause
