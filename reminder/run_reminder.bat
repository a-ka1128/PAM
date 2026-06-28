@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title Reminder Bot
cd /d "D:\Study\DIscordBot\AutoLinkBot\reminder"
echo ============================================
echo   Reminder Bot starting...
echo   (Close this window to stop)
echo ============================================
echo.
"D:\Study\DIscordBot\venv\Scripts\python.exe" reminder_bot.py
echo.
echo === Bot stopped. Press any key to close. ===
pause >nul
