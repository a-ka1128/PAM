@echo off
REM setup_named_exe.bat — 작업관리자에 이름이 보이는 실행파일 만들기 (1회 실행)
REM
REM   venv의 pythonw.exe를 "LooKP LinkBot.exe" / "LooKP Bot.exe" 로 복사한다.
REM   이유: 그냥 pythonw.exe로 띄우면 작업관리자에 정체불명의 pythonw.exe로만 보여
REM         봇이 켜져 있는지 확인할 수 없다.
REM   서명은 파일 내용에 붙으므로 이름을 바꿔도 Python Software Foundation 서명이 유지되고
REM   Smart App Control을 통과한다. (직접 빌드한 PyInstaller exe는 서명이 없어 차단된다)
REM
REM   venv를 다시 만들었으면 이 파일을 한 번 실행하면 된다.
setlocal
set S=D:\Study\DIscordBot\venv\Scripts

if not exist "%S%\pythonw.exe" (
  echo [FAIL] venv를 찾을 수 없음: %S%
  exit /b 1
)
copy /Y "%S%\pythonw.exe" "%S%\LooKP LinkBot.exe" >nul
copy /Y "%S%\pythonw.exe" "%S%\LooKP Bot.exe" >nul
echo [OK] 생성 완료:
echo      %S%\LooKP LinkBot.exe   (트레이 런처)
echo      %S%\LooKP Bot.exe       (봇 본체)
echo.
echo 이제 LinkBot.vbs 로 실행하면 작업관리자에 위 이름으로 표시됩니다.
endlocal
