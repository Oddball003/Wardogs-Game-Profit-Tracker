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
echo This removes the app, its settings, its Steam sign-in and Start with
echo Windows (if it was on) from:
echo   %~dp0
echo   %~dp0dist
echo   %LOCALAPPDATA%\%APP%
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

rem the text files the app writes (separate files + leaderboard), read from
rem settings.json before it's deleted, one per line
set "OUTLIST=%TEMP%\wardogs_outfiles.txt"
if exist "%OUTLIST%" del /f /q "%OUTLIST%"
for %%D in ("%~dp0." "%~dp0dist" "%LOCALAPPDATA%\%APP%") do (
    if exist "%%~D\settings.json" (
        powershell -NoProfile -Command "try { $s = Get-Content -Raw '%%~D\settings.json' | ConvertFrom-Json; $f = @($s.people | Where-Object { $_.own_file -and $_.out_file } | ForEach-Object { $_.out_file }); if ($s.board_on -and $s.board_file) { $f += $s.board_file }; if ($s.out_file) { $f += $s.out_file }; $f | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique | Add-Content -LiteralPath '%OUTLIST%' } catch {}"
    )
    if exist "%%~D\%EXE%"                   del /f /q "%%~D\%EXE%"
    if exist "%%~D\settings.json"           del /f /q "%%~D\settings.json"
    if exist "%%~D\settings.json.tmp"       del /f /q "%%~D\settings.json.tmp"
    if exist "%%~D\settings.json.broken"    del /f /q "%%~D\settings.json.broken"
    if exist "%%~D\activity_log.txt"        del /f /q "%%~D\activity_log.txt"
    if exist "%%~D\steam_browser_profile"   rmdir /s /q "%%~D\steam_browser_profile"
    if exist "%%~D\.wardogs.lock"           del /f /q "%%~D\.wardogs.lock"
)
rmdir /s /q "%~dp0build" >nul 2>nul
del /q "%~dp0%APP%.spec" >nul 2>nul
rmdir "%~dp0dist" >nul 2>nul
rmdir "%LOCALAPPDATA%\%APP%" >nul 2>nul
rem Start with Windows (Advanced): the sign-in entry
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "%APP%" /f >nul 2>nul
echo Removed the app, settings, Steam sign-in and Start with Windows.

if exist "%OUTLIST%" (
    echo.
    echo The app was writing to these text files:
    for /f "usebackq delims=" %%P in ("%OUTLIST%") do echo   %%P
    set /p DELTXT="Delete them too? (y/N): "
    if /i "!DELTXT!"=="y" (
        for /f "usebackq delims=" %%P in ("%OUTLIST%") do (
            del /f /q "%%P" >nul 2>nul
            if exist "%%P.tmp" del /f /q "%%P.tmp" >nul 2>nul
        )
        echo Deleted.
    )
    del /f /q "%OUTLIST%" >nul 2>nul
)

echo.
set /p PIP="Also remove the Python packages used to build it (playwright, obsws-python, pillow, pystray, pyinstaller)? (y/N): "
if /i "%PIP%"=="y" (
    set "PY=python"
    where py >nul 2>nul && set "PY=py"
    !PY! -m pip uninstall -y playwright obsws-python pillow pystray pyinstaller pyinstaller-hooks-contrib
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
