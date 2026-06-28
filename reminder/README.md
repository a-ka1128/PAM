# Reminder Bot — 리마인더 엔진 (B)

수집봇과 **별개인** 봇. 메시지에 우선순위 이모지를 달면 추적하고, 처리될 때까지 재촉합니다.

## 동작
- 🔴/🟡/🔵 **반응 = 플래그(추적 시작)** + 우선순위 (🔴 1h · 🟡 3h · 🔵 12h 간격 재촉)
- ✅ **반응** 또는 그 메시지에 **답장** = **응답/완료 → 재촉 중단**
- 대상 = 그 메시지에 **멘션된 사람**(없으면 작성자), 기본은 **개인 DM**으로 재촉

## 설치 (수집봇과 별도 토큰!)
1. [개발자 포털](https://discord.com/developers/applications)에서 **새 Application** 생성 → Bot → 토큰 발급
2. Bot 화면에서 **MESSAGE CONTENT** + **SERVER MEMBERS** 인텐트 ON
3. 초대 (client_id 본인 것으로):
   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=68608&scope=bot%20applications.commands
   ```
   (권한 = 채널 보기 + 메시지 보내기 + 기록 읽기)
4. `config.py` 의 `TOKEN` 채우기
5. **`run_reminder.bat`** 더블클릭

## 명령어 (소유자 전용)
- `/추적목록` — 미해결 항목 우선순위순
- `/추적해제 번호:N` — 수동 종료

## 메모
- 추적 데이터는 `reminder.db`(로컬, gitignore). 봇 재시작해도 유지.
- 이모지·간격·DM/채널 방식은 `config.py` 에서 변경.
- 지금은 **누구나** 우선순위 이모지로 플래그 가능 (나중에 역할 제한 추가 가능).
