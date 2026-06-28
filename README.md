# Collector — 디스코드 서버 활동 수집 봇 (1단계)

서버 **전체 채널**의 메시지(본문 포함)·메타데이터·이벤트·구조를 **로컬 SQLite**에 모읍니다.
모은 데이터는 2단계에서 **로컬 LLM(Ollama)**로 분석해 "무슨 자동화를 만들면 좋을지"를 찾습니다.

> 🔒 **로컬 전용 원칙**: DB와 토큰은 `.gitignore` 처리되어 외부로 나가지 않습니다.

---

## 1. 디스코드 봇 만들기 (개발자 포털)
1. https://discord.com/developers/applications → **New Application**
2. 좌측 **Bot** → **Reset Token** 으로 토큰 발급 → 복사
3. 같은 Bot 화면에서 **특권 인텐트 2개 켜기**:
   - ✅ **MESSAGE CONTENT INTENT**
   - ✅ **SERVER MEMBERS INTENT**
   - (PRESENCE 는 끔)

## 2. 봇 초대 (각 서버에)
아래 URL의 `YOUR_CLIENT_ID` 를 (개발자 포털 General Information의) Application ID로 바꿔 접속:
```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=66560&scope=bot%20applications.commands
```
- `permissions=66560` = **채널 보기 + 메시지 기록 읽기**(읽기 전용 최소권한).
- 관리자 권한으로 초대해도 됩니다(편하면). 비공개 채널까지 보려면 해당 채널 접근 권한이 있어야 합니다.

## 3. 설정 채우기 — `config.py`
이미 `config.py` 가 만들어져 있습니다(.gitignore 처리됨). 열어서 채우세요:
```python
TOKEN = "발급받은_토큰"
OWNER_ID = 343290913172226049     # 본인 디스코드 유저 ID (맞는지 확인)
```
- **서버 ID는 넣을 필요 없어요** — 봇을 서버에 초대하면 자동 인식되고 명령어가 그 서버에 등록됩니다.
- 유저/채널 ID가 필요하면 디스코드 **개발자 모드**(설정>고급)를 켜고 우클릭 → "ID 복사".

## 4. 설치 & 실행
```powershell
# 기존 venv 사용 (D:\Study\DIscordBot\venv) 또는 새로 생성
D:\Study\DIscordBot\venv\Scripts\python.exe -m pip install -r requirements.txt
D:\Study\DIscordBot\venv\Scripts\python.exe bot.py
```
`로그인: ...` 로그가 뜨면 정상.

## 5. 명령어 (소유자 전용, 응답은 본인만 보임)
| 명령어 | 설명 |
|---|---|
| `/수집 시작` | 실시간 수집 ON (중지 후라면 빈 구간 자동 gap-fill) |
| `/수집 중지` | **컴퓨터 끄기 전** 일시중지. 다음 시작 때 그 사이 메시지를 메움 |
| `/수집 종료` | 세션 종료. 이후 공백은 메우지 않음 |
| `/백필 일수:14` | 이 서버의 과거 14일치 메시지 수집(중복 제외, 진행상황 표시) |
| `/상태` | 수집 상태·서버별 수집량·가동시간 등 |

### 추천 운영 흐름 (3~4일)
1. 각 서버에서 `/백필 일수:14` (과거치 확보)
2. `/수집 시작` (이후 실시간 수집)
3. 자기 전 컴 끄기 전 → `/수집 중지` → 끔. 아침에 켜고 `/수집 시작` (밤새 구간 자동 보충)
   - 깜빡하고 그냥 꺼도 OK: 재시작 시 상태가 'collecting'이면 자동 gap-fill.
4. 다 모았으면 `/수집 종료`

## 6. 저장되는 것
- **messages**: 본문, 시각/서버/채널/작성자/길이, 답글·스레드, 링크 도메인, 첨부 유무, 멘션 수, 수정/삭제 표시
- **attachments**: 파일 메타데이터만(이름·확장자·타입·크기) — **이미지/파일 자체는 저장 안 함**
- **mentions / reactions / member_events / voice_events**
- **guild/channel/role 스냅샷**: 채널 이름·주제·카테고리·역할·인원 (분석 맥락용)

## 7. 데이터 보기 / 분석 (2단계 예고)
- DB 파일: `collector.db` (DB Browser for SQLite 등으로 열람 가능)
- 2단계에서 pandas 통계 + 로컬 LLM 분석 스크립트를 추가할 예정 (전부 로컬).

---
### 메모
- 한글 슬래시 명령어(`/수집` 등)가 동기화 에러를 내면, 알려주세요 — 영문 이름으로 바로 바꿔드립니다.
- 보안: 옆 폴더 `Mafia.py` 에 봇 토큰이 코드에 박혀 있습니다. 이 상위 폴더가 git 저장소라 push 시 유출되니, 그 토큰도 분리/재발급을 권장합니다.
