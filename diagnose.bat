@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 reclight.py --list > ports.txt 2>&1
type ports.txt
echo.
echo Vysledek je ulozen v ports.txt
pause
