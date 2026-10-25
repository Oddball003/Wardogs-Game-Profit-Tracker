@echo off
title Remove old Wardogs Profit Tracker
cd /d "%~dp0"

set "EXE=Wardogs Profit Tracker.exe"

echo Closing the app if it's running...
taskkill /f /im "%EXE%" >nul 2>nul
timeout /t 2 >nul

echo Removing old build files...
if exist "dist\%EXE%" del /f /q "dist\%EXE%"
if exist "%EXE%" del /f /q "%EXE%"
rmdir /s /q build >nul 2>nul
del /q "Wardogs Profit Tracker.spec" >nul 2>nul

echo.
set /p WIPE="Also delete saved settings and Steam sign-in? (y/N): "
if /i "%WIPE%"=="y" (
    for %%D in ("." "dist") do (
        if exist "%%~D\settings.json" del /f /q "%%~D\settings.json"
        if exist "%%~D\steam_browser_profile" rmdir /s /q "%%~D\steam_browser_profile"
    )
    echo Settings and sign-in removed.
) else (
    echo Kept your settings and sign-in.
)

echo.
if exist "dist\%EXE%" (
    echo Couldn't delete dist\%EXE% - it may still be open. Close it and run this again.
) else (
    echo Old exe removed. Run build_exe.bat to make a fresh one.
)
pause
