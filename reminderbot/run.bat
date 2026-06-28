@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title LooKP Reminder Bot
cd /d D:\Study\DIscordBot\AutoLinkBot\reminderbot
:loop
echo === LooKP Reminder Bot starting (close this window to stop) ===
"D:\Study\DIscordBot\venv\Scripts\python.exe" reminder_bot.py
echo.
echo [watchdog] reminder bot exited - restarting in 5s
timeout /t 5 /nobreak >nul
goto loop
