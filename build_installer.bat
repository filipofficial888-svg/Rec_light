@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === RecLight: sestaveni instalatoru ===
echo.

where winget >nul 2>nul
if errorlevel 1 goto nowinget

echo [1/4] Python
py -3 --version >nul 2>nul
if not errorlevel 1 goto havepython
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
echo.
echo Python byl prave nainstalovan. Zavrete toto okno a spustte build_installer.bat znovu.
pause
exit /b 0

:havepython
echo Python je nainstalovany.
echo.
echo [2/4] Knihovny a RecLight.exe
taskkill /f /im RecLight.exe >nul 2>nul
py -3 -m pip install -r requirements.txt
if errorlevel 1 goto fail
py -3 make_icons.py
if errorlevel 1 goto fail
py -3 -m PyInstaller --noconsole --onefile --collect-submodules mido --hidden-import mido.backends.rtmidi --hidden-import rtmidi --hidden-import pystray._win32 --hidden-import gui --hidden-import vmidi --collect-all customtkinter --name RecLight --icon reclight.ico reclight.py
if errorlevel 1 goto fail
if not exist dist\RecLight.exe goto fail

echo.
echo [3/4] Inno Setup
set "PF86=%ProgramFiles(x86)%"
call :findiscc
if defined ISCC goto haveiscc
winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
call :findiscc
if not defined ISCC goto noinno

:haveiscc
echo.
echo [4/4] Instalator
"%ISCC%" installer.iss
if errorlevel 1 goto fail
echo.
echo Hotovo: %~dp0Output\RecLight-Setup.exe
explorer "%~dp0Output"
pause
exit /b 0

:findiscc
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%PF86%\Inno Setup 6\ISCC.exe" set "ISCC=%PF86%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
exit /b 0

:nowinget
echo Chybi winget. Aktualizujte "App Installer" z Microsoft Store a spustte skript znovu.
pause
exit /b 1

:noinno
echo Inno Setup se nepodarilo nainstalovat ani najit. Stahnete ho z https://jrsoftware.org/isdl.php
echo a spustte skript znovu.
pause
exit /b 1

:fail
echo.
echo Sestaveni selhalo, viz zpravy vyse.
pause
exit /b 1
