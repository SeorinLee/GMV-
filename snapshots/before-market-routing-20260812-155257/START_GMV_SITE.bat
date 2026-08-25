@echo off
setlocal
title TikTok GMV
cd /d "%~dp0"

if exist "%~dp0dist\TikTok-GMV-Portable\runtime\node.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dist\TikTok-GMV-Portable\scripts\launch-portable.ps1"
) else if exist "%~dp0runtime\node.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch-portable.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch-portable.ps1" -SourceMode
)
exit /b %ERRORLEVEL%
