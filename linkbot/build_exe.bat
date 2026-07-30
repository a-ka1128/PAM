@echo off
REM build_exe.bat — tray_app.py를 콘솔 없는 단일 exe로 빌드 (PyInstaller)
REM   결과: 이 폴더에 "LooKP LinkBot.exe" 생성. 빌드 잔여물(build/dist/spec)은 gitignore됨.
REM   필요: 공유 venv에 pyinstaller, pystray, Pillow 설치돼 있어야 함.
setlocal
set PY=D:\Study\DIscordBot\venv\Scripts\python.exe
cd /d "%~dp0"

"%PY%" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name "LooKP LinkBot" --icon linkbot.ico ^
  --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder ^
  tray_app.py

if exist "dist\LooKP LinkBot.exe" (
  copy /Y "dist\LooKP LinkBot.exe" "LooKP LinkBot.exe" >nul
  echo.
  echo [OK] "LooKP LinkBot.exe" 생성 완료 (이 폴더).
) else (
  echo [FAIL] 빌드 실패 — 위 로그 확인.
)
endlocal
