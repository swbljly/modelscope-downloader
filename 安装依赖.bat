@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

title ModelScope Downloader - Install Dependencies

echo ============================================================
echo   Installing ModelScope Downloader dependencies ...
echo ============================================================
echo.

rem Use the EXACT SAME interpreter selection as the launcher .bat
rem (pythonw first, then python), otherwise the SDK could be installed into a
rem different Python than the one that actually runs the GUI.
set "PYEXE="
for /f "delims=" %%W in ('where pythonw 2^>nul') do (
    if not defined PYEXE (
        if exist "%%~dpWpython.exe" (
            "%%~dpWpython.exe" -c "import tkinter" >nul 2>nul
            if not errorlevel 1 set "PYEXE=%%~dpWpython.exe"
        )
    )
)

if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE (
            "%%P" -c "import tkinter" >nul 2>nul
            if not errorlevel 1 set "PYEXE=%%P"
        )
    )
)

if not defined PYEXE (
    echo [ERROR] No usable Python 3.8+ with tkinter was found in PATH.
    echo Please install Python from https://www.python.org/downloads/
    echo and check "Add Python to PATH" and "tcl/tk and IDLE".
    echo.
    pause
    exit /b 1
)

echo [OK] Using: %PYEXE%
echo.

"%PYEXE%" -m pip install --upgrade pip
"%PYEXE%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed.
    echo If the download is slow, try a mirror, for example:
    echo   "%PYEXE%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo.
echo [OK] Dependencies installed successfully.
echo You can now double-click the launcher .bat file to start the GUI.
echo.
pause
