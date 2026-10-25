@echo off
title Build Wardogs Profit Tracker
cd /d "%~dp0"

if not exist "wardogs_profit_tracker.py" (
    echo wardogs_profit_tracker.py not found next to this .bat
    pause
    exit /b 1
)

set "PY=python"
where py >nul 2>nul && set "PY=py"

rem close an old copy so it can be replaced
taskkill /f /im "Wardogs Profit Tracker.exe" >nul 2>nul

echo [1/2] Installing build tools...
%PY% -m pip install --upgrade playwright obsws-python pyinstaller
if errorlevel 1 goto fail

echo.
echo [2/2] Building the exe (takes a minute)...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "Wardogs Profit Tracker" ^
    --hidden-import obsws_python ^
    --collect-all playwright ^
    wardogs_profit_tracker.py
if errorlevel 1 goto fail

rmdir /s /q build >nul 2>nul
del /q "Wardogs Profit Tracker.spec" >nul 2>nul

echo.
echo Done!  Your app is here:
echo   %~dp0dist\Wardogs Profit Tracker.exe
echo.
echo You can move that .exe into any folder (e.g. your OBS Elements folder).
echo It saves its settings and Steam sign-in next to itself.
explorer "%~dp0dist"
pause
exit /b 0

:fail
echo.
echo Build failed - scroll up for the error.
pause
exit /b 1
