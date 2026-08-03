' LinkBot.vbs — 트레이 앱을 콘솔창 없이 실행 (더블클릭 또는 시작프로그램 등록용)
'   "LooKP LinkBot.exe"는 venv의 pythonw.exe를 이름만 바꿔 복사한 것이다.
'   왜: 그냥 pythonw.exe로 띄우면 작업관리자에 정체불명의 'pythonw.exe'로만 보여
'       봇이 켜져 있는지 알 수 없다. 이름을 주면 한눈에 보인다.
'       서명은 파일 내용에 붙으므로 이름을 바꿔도 Python Software Foundation 서명이
'       유지되고 Smart App Control도 통과한다(직접 빌드한 PyInstaller exe는 차단됨).
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\Study\DIscordBot\AutoLinkBot\linkbot"
sh.Run """D:\Study\DIscordBot\venv\Scripts\LooKP LinkBot.exe"" tray_app.py", 0, False
