@echo off
setlocal EnableDelayedExpansion
title Uninstall Wardogs Profit Tracker
cd /d "%~dp0"

set "APP=Wardogs Profit Tracker"
set "EXE=%APP%.exe"

echo ============================================
echo   Uninstall %APP%
echo ============================================
echo.
echo This removes the app, its settings and its Steam sign-in from:
echo   %~dp0
echo   %~dp0dist
echo.
set /p OK="Continue? (y/N): "
if /i not "%OK%"=="y" (
    echo Cancelled. Nothing was removed.
    pause
    exit /b 0
)

echo.
echo Closing the app if it's open...
taskkill /f /im "%EXE%" >nul 2>nul
timeout /t 2 >nul

for %%D in ("%~dp0." "%~dp0dist") do (
    if exist "%%~D\settings.json" (
        rem read the output txt location before settings.json is deleted
        for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "try { (Get-Content -Raw '%%~D\settings.json' | ConvertFrom-Json).out_file } catch {}"`) do set "OUTFILE=%%P"
    )
    if exist "%%~D\%EXE%"              del /f /q "%%~D\%EXE%"
    if exist "%%~D\settings.json"      del /f /q "%%~D\settings.json"
    if exist "%%~D\steam_browser_profile" rmdir /s /q "%%~D\steam_browser_profile"
    if exist "%%~D\.wardogs.lock"      del /f /q "%%~D\.wardogs.lock"
)
rmdir /s /q "%~dp0build" >nul 2>nul
del /q "%~dp0%APP%.spec" >nul 2>nul
rmdir "%~dp0dist" >nul 2>nul
echo Removed the app, settings and Steam sign-in.

if defined OUTFILE if exist "!OUTFILE!" (
    echo.
    echo The app was writing to:  !OUTFILE!
    set /p DELTXT="Delete that text file too? (y/N): "
    if /i "!DELTXT!"=="y" (
        del /f /q "!OUTFILE!"
        if exist "!OUTFILE!.tmp" del /f /q "!OUTFILE!.tmp"
        echo Deleted.
    )
)

echo.
set /p PIP="Also remove the Python packages used to build it (playwright, obsws-python, pyinstaller)? (y/N): "
if /i "%PIP%"=="y" (
    set "PY=python"
    where py >nul 2>nul && set "PY=py"
    !PY! -m pip uninstall -y playwright obsws-python pyinstaller pyinstaller-hooks-contrib
)

if exist "%LOCALAPPDATA%\ms-playwright" (
    echo.
    echo Found a Playwright browser download from the old script, about 150 MB:
    echo   %LOCALAPPDATA%\ms-playwright
    set /p PWB="Delete it? (y/N): "
    if /i "!PWB!"=="y" (
        rmdir /s /q "%LOCALAPPDATA%\ms-playwright"
        echo Deleted.
    )
)

echo.
if exist "%~dp0dist\%EXE%" (
    echo Couldn't delete the exe - it may still be open. Close it and run this again.
) else if exist "%~dp0%EXE%" (
    echo Couldn't delete the exe - it may still be open. Close it and run this again.
) else (
    echo Done. %APP% has been uninstalled.
    echo You can delete this folder's .py and .bat files yourself if you don't need them.
)
echo.
pause
