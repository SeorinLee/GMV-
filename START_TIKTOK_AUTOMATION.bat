@echo off
setlocal
title TikTok Automation
cd /d "%~dp0"

if exist "%~dp0dist\TikTok-GMV-Portable\runtime\node.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dist\TikTok-GMV-Portable\scripts\launch-portable.ps1"
) else if exist "%~dp0runtime\node.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch-portable.ps1"
) else (
  echo.
  echo [ERROR] The bundled runtime was not found.
  echo Extract the entire ZIP file, then run START_TIKTOK_AUTOMATION.bat again.
  echo Python, Node.js, and npm do not need to be installed.
  echo.
  pause
  exit /b 1
)
exit /b %ERRORLEVEL%
