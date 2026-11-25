@echo off
setlocal
title KOL 红人邮件系统
cd /d "%~dp0"

if not exist "%~dp0runtime\python\python.exe" goto missing_runtime
"%~dp0runtime\python\python.exe" "%~dp0server\app.py"
if errorlevel 1 pause
goto end

:missing_runtime
echo Missing runtime\python\python.exe. Keep the runtime folder beside this file.
pause

:end
