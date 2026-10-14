@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Nobitex Portfolio
echo.
echo  ========================================
echo   Nobitex Portfolio - local server
echo  ========================================
echo.

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 main.py
  goto END
)
where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python main.py
  goto END
)

echo ERROR: Python 3 was not found.
echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
echo.

:END
if errorlevel 1 (
  echo.
  echo Server stopped with an error.
  pause
)
endlocal
