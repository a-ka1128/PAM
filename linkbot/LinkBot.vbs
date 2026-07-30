' LinkBot.vbs — 트레이 앱을 콘솔창 없이 실행 (더블클릭 또는 시작프로그램 등록용)
'   pythonw.exe로 tray_app.py를 백그라운드 실행. cmd 깜빡임조차 없다.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\Study\DIscordBot\AutoLinkBot\linkbot"
sh.Run """D:\Study\DIscordBot\venv\Scripts\pythonw.exe"" tray_app.py", 0, False
