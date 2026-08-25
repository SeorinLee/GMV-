@echo off
setlocal
title TikTok GMV - Stop
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-portable.ps1"
exit /b %ERRORLEVEL%
