@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

title ModelScope Downloader Launcher

echo ============================================================
echo   ModelScope Model Downloader - starting ...
echo ============================================================
echo.

set "PYWEXE="
set "PYEXE="

rem ---- 1) Preferred: "where pythonw" -> pythonw gui_app.py (no console) ----
rem A portable/embedded Python may ship pythonw.exe without tkinter; validate
rem each candidate by importing tkinter with its sibling python.exe first.
for /f "delims=" %%W in ('where pythonw 2^>nul') do (
    if not defined PYWEXE (
        if exist "%%~dpWpython.exe" (
            "%%~dpWpython.exe" -c "import tkinter" >nul 2>nul
            if not errorlevel 1 (
                set "PYWEXE=%%W"
                set "PYEXE=%%~dpWpython.exe"
            )
        )
    )
)

if defined PYWEXE (
    echo [OK] Using pythonw: %PYWEXE%
    echo.
    start "" "%PYWEXE%" "gui_app.py"
    goto :EOF
)

echo [WARN] No usable pythonw.exe with tkinter found; falling back to python.exe.
echo.

rem ---- 2) Fallback: "where python" -> python gui_app.py (console window) ----
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE (
        "%%P" -c "import tkinter" >nul 2>nul
        if not errorlevel 1 set "PYEXE=%%P"
    )
)

if not defined PYEXE (
    echo [ERROR] No usable Python 3.8+ with tkinter was found in PATH.
    echo.
    echo Please install Python 3.8 or newer from:
    echo     https://www.python.org/downloads/
    echo During installation check BOTH "Add Python to PATH" and
    echo "tcl/tk and IDLE". Portable / embedded builds usually lack tkinter.
    echo.
    pause
    exit /b 1
)

echo [OK] Using python: %PYEXE%
echo.
"%PYEXE%" "gui_app.py"
if errorlevel 1 (
    echo.
    echo [ERROR] The application exited with an error.
    echo Try installing dependencies first:  pip install -r requirements.txt
    echo Or double-click the dependency installer .bat file in this folder.
    echo A crash log may have been created in this folder.
    pause
)
