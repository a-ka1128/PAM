// =========================================================
// 룩플 봇 → 구글 시트 (일정 / 정산 / 확인필요)
//   - doPost: upsert(쓰기) + action:"delete"(행 삭제)
//   - doGet: 확인필요 탭 읽기(JSON) — 봇이 처리(등록/무시) 읽어감
//   - 모든 시트 변경은 LockService로 직렬화
//   - 봇 POST 형식: { "sheet":"일정", "items":[ {"key":"<msg.id>","fields":{...}}, ... ] }
//                  { "sheet":"확인필요", "action":"delete", "keys":["<msg.id>", ...] }
// =========================================================
var TABS = {
  "일정":     ["날짜", "작업내용", "진행내용", "담당자", "의뢰자", "상태", "D-day", "원본"],
  "정산":     ["받는이", "항목", "금액", "상태", "원본"],
  "확인필요": ["내용", "추정 작업자", "추정 의뢰자", "사유", "원본", "처리"]
};
var STATUSES = ["진행중", "대기중", "공개 대기", "쉬는중", "답변필요", "완료"];   // 일정용
var SETTLE_STATUSES = ["미정산", "회사에서 미정산", "완료"];              // 정산용
var ACTIONS = ["등록", "무시"];                                          // 확인필요 처리
var WORK_COLORS = { "진행중": "#d9ead3", "대기중": "#fff2cc", "공개 대기": "#cfe2f3", "쉬는중": "#efefef", "답변필요": "#f4cccc", "완료": "#b6d7a8" };
var SETTLE_COLORS = { "미정산": "#fff2cc", "회사에서 미정산": "#fce5cd", "완료": "#d9ead3" };

function doPost(e) {
  var d = JSON.parse(e.postData.contents);
  if (d.action === "delete") return doDelete(d);   // 신규: 행 삭제
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);                            // upsert/resort 직렬화
  try {
    var head = TABS[d.sheet];
    if (!head) return ContentService.createTextOutput("unknown sheet: " + d.sheet);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sh = ss.getSheetByName(d.sheet) || ss.insertSheet(d.sheet);
    var keyCol = head.length + 1;
    if (sh.getLastRow() === 0) initTab(sh, d.sheet, head, keyCol);

    var last = sh.getLastRow(), map = {};
    if (last >= 2) {
      var ks = sh.getRange(2, keyCol, last - 1, 1).getValues();
      for (var i = 0; i < ks.length; i++) if (ks[i][0] !== "") map[ks[i][0]] = 2 + i;
    }

    var mode = d.mode || "upsert";       // "upsert"(신규/수정) | "merge"(답장 부분병합)
    var items = d.items || [], n = 0;
    var nextRow = sh.getLastRow() + 1;
    items.forEach(function (it) {
      var f = it.fields || {};
      var exists = map[it.key] !== undefined;
      if (mode === "merge" && !exists) return;        // 유령행 생성 금지 → skip

      var row = exists ? map[it.key] : nextRow;
      if (!exists) nextRow++;

      head.forEach(function (h, ci) {
        if (h === "D-day" || h === "원본" || h === "날짜" || h === "금액") return;   // 특수 처리 별도
        var provided = (f[h] !== undefined && f[h] !== null && String(f[h]).trim() !== "");
        if (exists) {
          if (provided) sh.getRange(row, ci + 1).setValue(f[h]);     // 기존행: 비공백만 덮기(나머지 보존)
        } else {
          sh.getRange(row, ci + 1).setValue(provided ? f[h] : "");   // 신규행: 빈칸 초기화
        }
      });

      var li = head.indexOf("원본");
      if (li >= 0 && f["링크"]) sh.getRange(row, li + 1).setFormula('=HYPERLINK("' + String(f["링크"]).replace(/"/g, "") + '","원본")');

      var dt = head.indexOf("날짜"), dd = head.indexOf("D-day");
      if (dt >= 0 && f["날짜"] !== undefined && String(f["날짜"]).trim() !== "") {
        var dv = String(f["날짜"]);
        if (dv.indexOf("~") >= 0) {                                   // 범위 → 텍스트 유지(정렬은 앞날짜 기준)
          sh.getRange(row, dt + 1).setNumberFormat("@").setValue(dv);
        } else {
          var pd = parseYmd(dv);
          if (pd) sh.getRange(row, dt + 1).setValue(pd).setNumberFormat("yyyy-mm-dd");
          else sh.getRange(row, dt + 1).setValue(dv);                 // 비표준 날짜 원문
        }
      } else if (!exists && dt >= 0) {
        sh.getRange(row, dt + 1).setValue("");                        // 신규+날짜없음=빈칸
      }
      if (dd >= 0 && dt >= 0) { var a1 = sh.getRange(row, dt + 1).getA1Notation(); sh.getRange(row, dd + 1).setFormula('=IF(ISNUMBER(' + a1 + '),' + a1 + '-TODAY(),IFERROR(DATEVALUE(TRIM(MID(' + a1 + ',FIND("~",' + a1 + ')+1,50)))-TODAY(),""))'); }

      var am = head.indexOf("금액");
      if (am >= 0 && f["금액"] !== undefined && String(f["금액"]).trim() !== "") { var num = parseInt(String(f["금액"]).replace(/[^0-9]/g, "")); if (!isNaN(num)) sh.getRange(row, am + 1).setValue(num).setNumberFormat("#,##0"); }

      sh.getRange(row, keyCol).setValue(it.key);
      map[it.key] = row; n++;
    });
    if (head.indexOf("날짜") >= 0) resortSchedule();   // 일정 = 날짜순 자동 정렬
    return ContentService.createTextOutput("ok:" + n);
  } finally { lock.releaseLock(); }
}

// 확인필요/일정 탭 → JSON (원본 URL은 HYPERLINK 수식이라 getFormulas로 추출)
//   확인필요: 봇이 처리(등록/무시) 읽어감.  일정: 리마인더봇 아침 다이제스트용(읽기 전용).
var READ_ALLOWED = { "확인필요": true, "일정": true, "정산": true };   // 정산: 미수금 나이 추적(다이제스트)

// 읽기 토큰 — 배포 URL만 알면 일정 탭(의뢰자명·작업내용·원본링크)이 통째로 열람되던 문제 차단.
//   ⚠️ 토큰은 소스에 넣지 말 것(이 파일은 git 추적됨). 스크립트 속성에 저장한다:
//      Apps Script 편집기 → 프로젝트 설정 → 스크립트 속성 → READ_TOKEN = <임의 문자열>
//   속성이 비어 있으면 기존처럼 무인증 동작(점진 배포용) — 설정하는 순간 강제된다.
function _readTokenOk(e) {
  var want = PropertiesService.getScriptProperties().getProperty("READ_TOKEN");
  if (!want) return true;                        // 미설정 = 기존 동작 유지
  var got = (e && e.parameter && e.parameter.token) || "";
  return got === want;
}

function doGet(e) {
  var name = (e && e.parameter && e.parameter.sheet) || "확인필요";
  var out = { ok: true, sheet: name, rows: [] };
  if (!_readTokenOk(e)) { out.ok = false; out.error = "unauthorized"; return _json(out); }
  if (!READ_ALLOWED[name]) { out.ok = false; out.error = "not allowed"; return _json(out); }
  var head = TABS[name];
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sh) return _json(out);
  var keyCol = head.length + 1, last = sh.getLastRow();
  if (last < 2) return _json(out);

  var lock = LockService.getScriptLock();
  try { lock.waitLock(20000); } catch (err) { out.ok = false; out.error = "locked"; return _json(out); }
  try {
    var iSrc = head.indexOf("원본");
    var forms = (iSrc >= 0) ? sh.getRange(2, iSrc + 1, last - 1, 1).getFormulas() : null;
    if (name === "확인필요") {
      var vals = sh.getRange(2, 1, last - 1, keyCol).getValues();
      var iC = head.indexOf("내용"), iW = head.indexOf("추정 작업자"),
          iR = head.indexOf("추정 의뢰자"), iRe = head.indexOf("사유"), iP = head.indexOf("처리");
      for (var r = 0; r < vals.length; r++) {
        var row = vals[r], key = row[keyCol - 1];
        if (key === "" || key === null) continue;
        var link = "";
        if (forms) { var m = String(forms[r][0] || "").match(/HYPERLINK\("([^"]+)"/i); if (m) link = m[1]; }
        out.rows.push({
          key: String(key),
          "내용": String(row[iC] || ""), "추정작업자": String(row[iW] || ""),
          "추정의뢰자": String(row[iR] || ""), "사유": String(row[iRe] || ""),
          "처리": String(row[iP] || ""), "원본링크": link
        });
      }
    } else {
      // 일정 — 표시값(getDisplayValues)으로 읽어 Date의 UTC 직렬화 밀림을 원천 차단("2026-07-15" 문자열 그대로).
      var disp = sh.getRange(2, 1, last - 1, keyCol).getDisplayValues();
      for (var r2 = 0; r2 < disp.length; r2++) {
        var row2 = disp[r2], key2 = row2[keyCol - 1];
        if (key2 === "" || key2 === null) continue;
        var link2 = "";
        if (forms) { var m2 = String(forms[r2][0] || "").match(/HYPERLINK\("([^"]+)"/i); if (m2) link2 = m2[1]; }
        var o = { key: String(key2), "원본링크": link2 };
        for (var c = 0; c < head.length; c++) { if (head[c] === "원본") continue; o[head[c]] = String(row2[c] || ""); }
        out.rows.push(o);
      }
    }
  } finally { lock.releaseLock(); }
  return _json(out);
}

function _json(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

// 행 삭제 (key=_key 매칭, 내림차순으로 인덱스 밀림 방지)
function doDelete(d) {
  var name = d.sheet || "확인필요";
  var head = TABS[name];
  if (!head) return ContentService.createTextOutput("unknown sheet");
  var keys = d.keys || [];
  if (!keys.length) return ContentService.createTextOutput("deleted:0");
  var keySet = {}; keys.forEach(function (k) { keySet[String(k)] = true; });
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
    if (!sh) return ContentService.createTextOutput("deleted:0");
    var keyCol = head.length + 1, last = sh.getLastRow();
    if (last < 2) return ContentService.createTextOutput("deleted:0");
    var ks = sh.getRange(2, keyCol, last - 1, 1).getValues();
    var targets = [];
    for (var i = 0; i < ks.length; i++) {
      var k = ks[i][0];
      if (k !== "" && keySet[String(k)]) targets.push(2 + i);
    }
    targets.sort(function (a, b) { return b - a; });
    targets.forEach(function (rn) { sh.deleteRow(rn); });
    return ContentService.createTextOutput("deleted:" + targets.length);
  } finally { lock.releaseLock(); }
}

function initTab(sh, name, head, keyCol) {
  sh.getRange(1, 1, 1, head.length).setValues([head])
    .setBackground("#34495e").setFontColor("#ffffff").setFontWeight("bold").setHorizontalAlignment("center");
  sh.setRowHeight(1, 28); sh.setFrozenRows(1);
  var W = { "날짜": 100, "작업내용": 240, "진행내용": 220, "담당자": 140, "의뢰자": 110, "상태": 92, "D-day": 70, "원본": 64, "받는이": 110, "항목": 220, "금액": 100, "내용": 300, "추정 작업자": 110, "추정 의뢰자": 110, "사유": 170, "처리": 92 };
  for (var i = 0; i < head.length; i++) sh.setColumnWidth(i + 1, W[head[i]] || 120);
  sh.getRange(1, keyCol).setValue("_key"); sh.hideColumns(keyCol);
  sh.getRange(2, 1, 5000, head.length).clearDataValidations();   // 잔존 드롭다운 제거(재초기화·스키마변경 안전)

  var rules = [];
  var si = head.indexOf("상태");
  if (si >= 0) {
    var sc = sh.getRange(2, si + 1, 5000, 1);
    var slist = (name === "정산") ? SETTLE_STATUSES : STATUSES;
    var colors = (name === "정산") ? SETTLE_COLORS : WORK_COLORS;
    sc.setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(slist, true).build());
    slist.forEach(function (s) { rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(s).setBackground(colors[s]).setRanges([sc]).build()); });
  }
  var ddi = head.indexOf("D-day");
  if (ddi >= 0) {
    var dr = sh.getRange(2, ddi + 1, 5000, 1);
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberLessThanOrEqualTo(0).setBackground("#f4cccc").setRanges([dr]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberBetween(1, 7).setBackground("#fce5cd").setRanges([dr]).build());
  }
  if (rules.length) sh.setConditionalFormatRules(rules);

  var pi = head.indexOf("처리");
  if (pi >= 0) sh.getRange(2, pi + 1, 5000, 1).setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(ACTIONS, true).build());

  sh.getRange(2, 1, 5000, head.length).setHorizontalAlignment("left");
  var ali = head.indexOf("D-day");
  if (ali >= 0) sh.getRange(2, ali + 1, 5000, 1).setHorizontalAlignment("center");
}

// 기존 확인필요 탭 드롭다운을 [등록/무시]로 재적용 (에디터에서 1회 실행)
function fixActions() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("확인필요");
  if (!sh) return;
  var pi = TABS["확인필요"].indexOf("처리");
  if (pi < 0) return;
  sh.getRange(2, pi + 1, 5000, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(ACTIONS, true).build());
}

// 기존 정산 탭 드롭다운을 정산용으로 재적용 (에디터에서 1회 실행)
function fixSettleStatus() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("정산");
  if (!sh) return;
  var si = TABS["정산"].indexOf("상태");
  if (si < 0) return;
  var sc = sh.getRange(2, si + 1, 5000, 1);
  sc.setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(SETTLE_STATUSES, true).build());
  var rules = sh.getConditionalFormatRules();
  SETTLE_STATUSES.forEach(function (s) { rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(s).setBackground(SETTLE_COLORS[s]).setRanges([sc]).build()); });
  sh.setConditionalFormatRules(rules);
}

// 기존 탭들 정렬 일괄 교정 (에디터에서 1회 실행) — D-day 빼고 전부 좌측
function fixAll() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  Object.keys(TABS).forEach(function (name) {
    var sh = ss.getSheetByName(name); if (!sh) return;
    var head = TABS[name];
    sh.getRange(2, 1, 5000, head.length).setHorizontalAlignment("left");
    var di = head.indexOf("D-day");
    if (di >= 0) sh.getRange(2, di + 1, 5000, 1).setHorizontalAlignment("center");
  });
}

// 일정 탭에 잘못 남은 드롭다운(담당자 등) 전부 제거 + 상태 칸에만 재적용. 에디터에서 1회 실행.
function fixScheduleValidation() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("일정");
  if (!sh) return;
  var head = TABS["일정"];
  sh.getRange(2, 1, 5000, head.length).clearDataValidations();    // 데이터영역 전체 드롭다운 제거
  var si = head.indexOf("상태"), ddi = head.indexOf("D-day");
  if (si < 0) return;
  var sc = sh.getRange(2, si + 1, 5000, 1);
  sc.setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(STATUSES, true).build());  // 상태 칸에만(완료 포함)
  // 조건부서식 재구성 (상태 색 + D-day 색)
  var rules = [];
  STATUSES.forEach(function (s) {
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(s).setBackground(WORK_COLORS[s]).setRanges([sc]).build());
  });
  if (ddi >= 0) {
    var dr = sh.getRange(2, ddi + 1, 5000, 1);
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberLessThanOrEqualTo(0).setBackground("#f4cccc").setRanges([dr]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberBetween(1, 7).setBackground("#fce5cd").setRanges([dr]).build());
  }
  sh.setConditionalFormatRules(rules);
  Logger.log("fixScheduleValidation 완료 — 상태 드롭다운/색 재적용(완료 포함), 담당자 드롭다운 제거");
}

// 일정 탭의 빈 행(작업내용·_key 둘 다 빈 행) 제거 + 재정렬. 에디터에서 1회 실행.
function compactSchedule() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("일정");
  if (!sh) return;
  var lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    var head = TABS["일정"], keyCol = head.length + 1, last = sh.getLastRow();
    if (last < 2) return;
    var wi = head.indexOf("작업내용");
    var vals = sh.getRange(2, 1, last - 1, keyCol).getValues();
    var removed = 0;
    for (var r = last; r >= 2; r--) {                 // 아래에서 위로 삭제(인덱스 밀림 방지)
      var row = vals[r - 2];
      var task = String(row[wi] || "").trim();
      var key = String(row[keyCol - 1] || "").trim();
      if (!task && !key) { sh.deleteRow(r); removed++; }
    }
    resortSchedule();
    Logger.log("compactSchedule: 빈 행 " + removed + "개 제거");
  } finally { lock.releaseLock(); }
}

// 일정 탭 데이터 전부 삭제(헤더·서식·드롭다운 유지). /과거등록 force 재등록 전에 1회 실행.
function clearSchedule() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("일정");
  if (!sh) return;
  var lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    var last = sh.getLastRow();
    if (last >= 2) sh.deleteRows(2, last - 1);
    Logger.log("clearSchedule: 일정 데이터 전부 삭제 (헤더 유지)");
  } finally { lock.releaseLock(); }
}

// 날짜 문자열 → 정렬용 숫자키
function sortKey(v) {
  if (v instanceof Date) return v.getTime();
  var s = String(v || "");
  var m = s.match(/(20\d{2})-(\d{1,2})-(\d{1,2})/);
  if (m) return new Date(+m[1], +m[2] - 1, +m[3]).getTime();
  m = s.match(/(20\d{2})-(\d{1,2})-\?\?/);               // "2026-08-??" → 그 달 15일
  if (m) return new Date(+m[1], +m[2] - 1, 15).getTime();
  m = s.match(/(\d{1,2})\s*[\/.\-]\s*(\d{1,2})/);
  if (m) return new Date(2026, +m[1] - 1, +m[2]).getTime();
  m = s.match(/(\d{1,2})\s*\/\s*\?/);                    // "7/?"·"6/? ~ 7/?" → 그 달 15일로 정렬
  if (m) return new Date(2026, +m[1] - 1, 15).getTime();
  m = s.match(/(\d{1,2})\s*월\s*(초|중순|중|말)?/);
  if (m) return new Date(2026, +m[1] - 1, m[2] === "초" ? 5 : (m[2] === "말" ? 28 : 15)).getTime();
  return new Date(2099, 11, 31).getTime();
}

// 일정 탭을 날짜순 재정렬 (doPost가 자동 호출 + 에디터 1회 실행 가능)
function resortSchedule() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName("일정");
  if (!sh) return;
  var head = TABS["일정"], sortCol = head.length + 2, last = sh.getLastRow();
  if (last < 3) return;
  var dCol = head.indexOf("날짜") + 1;
  sh.getRange(1, sortCol).setValue("_sort");
  var dates = sh.getRange(2, dCol, last - 1, 1).getValues();
  sh.getRange(2, sortCol, last - 1, 1).setValues(dates.map(function (r) { return [sortKey(r[0])]; }));
  sh.getRange(2, 1, last - 1, sortCol).sort({ column: sortCol, ascending: true });
  sh.hideColumns(sortCol);
  var ddi = head.indexOf("D-day");
  if (ddi >= 0) {
    for (var i = 2; i <= last; i++) {
      var a1 = sh.getRange(i, dCol).getA1Notation();
      sh.getRange(i, ddi + 1).setFormula('=IF(ISNUMBER(' + a1 + '),' + a1 + '-TODAY(),IFERROR(DATEVALUE(TRIM(MID(' + a1 + ',FIND("~",' + a1 + ')+1,50)))-TODAY(),""))');
    }
  }
}

function parseYmd(s) { var m = String(s || "").match(/(20\d{2})-(\d{1,2})-(\d{1,2})/); return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null; }

// ===== 마이그레이션 (봇 정지 후 에디터에서 각 1회 실행) =====
// 기존 일정 탭 → 신스키마(진행내용 삽입) + _key "123"->"123#0" 정규화.
function migrateScheduleV2() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName("일정");
  if (!sh) { Logger.log("일정 탭 없음"); return; }
  var lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    var h1 = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
    if (h1.indexOf("진행내용") >= 0) { Logger.log("이미 마이그레이션됨"); return; }

    var last = sh.getLastRow();
    var oldHead = ["날짜", "작업", "담당자", "상태", "D-day", "원본"];
    var oldKeyCol = oldHead.length + 1;            // 7 = G
    var srcCol = oldHead.indexOf("원본") + 1;       // 6 = F
    var keyVals  = (last >= 2) ? sh.getRange(2, oldKeyCol, last - 1, 1).getValues() : [];
    var dateVals = (last >= 2) ? sh.getRange(2, 1, last - 1, 1).getValues() : [];
    var workVals = (last >= 2) ? sh.getRange(2, 2, last - 1, 1).getValues() : [];
    var ownerVals= (last >= 2) ? sh.getRange(2, 3, last - 1, 1).getValues() : [];
    var statVals = (last >= 2) ? sh.getRange(2, 4, last - 1, 1).getValues() : [];
    var srcForms = (last >= 2) ? sh.getRange(2, srcCol, last - 1, 1).getFormulas() : [];

    sh.clear(); sh.clearConditionalFormatRules();
    var newHead = TABS["일정"], newKeyCol = newHead.length + 1;   // 8 = H
    initTab(sh, "일정", newHead, newKeyCol);

    var iDate = newHead.indexOf("날짜"), iWork = newHead.indexOf("작업내용"),
        iProg = newHead.indexOf("진행내용"), iOwner = newHead.indexOf("담당자"),
        iStat = newHead.indexOf("상태"), iDday = newHead.indexOf("D-day"), iSrc = newHead.indexOf("원본");
    var r = 2;
    for (var i = 0; i < keyVals.length; i++) {
      var oldKey = String(keyVals[i][0] || "");
      if (!oldKey) continue;
      var newKey = /#\d+$/.test(oldKey) ? oldKey : (oldKey + "#0");
      var dv = dateVals[i][0];
      if (dv instanceof Date) sh.getRange(r, iDate + 1).setValue(dv).setNumberFormat("yyyy-mm-dd");
      else if (dv !== "" && dv !== null) sh.getRange(r, iDate + 1).setValue(dv);
      sh.getRange(r, iWork + 1).setValue(workVals[i][0] || "");
      sh.getRange(r, iProg + 1).setValue("");                      // 진행내용 빈칸(b안)
      sh.getRange(r, iOwner + 1).setValue(ownerVals[i][0] || "");
      sh.getRange(r, iStat + 1).setValue(statVals[i][0] || "진행중");
      var fml = (srcForms[i] && srcForms[i][0]) ? srcForms[i][0] : "";
      if (fml) sh.getRange(r, iSrc + 1).setFormula(fml);
      var a1 = sh.getRange(r, iDate + 1).getA1Notation();
      sh.getRange(r, iDday + 1).setFormula('=IF(ISNUMBER(' + a1 + '),' + a1 + '-TODAY(),IFERROR(DATEVALUE(TRIM(MID(' + a1 + ',FIND("~",' + a1 + ')+1,50)))-TODAY(),""))');
      sh.getRange(r, newKeyCol).setValue(newKey);
      r++;
    }
    resortSchedule();
    Logger.log("migrateScheduleV2: " + (r - 2) + "행 이전 완료");
  } finally { lock.releaseLock(); }
}

// 기존 일정 탭 → 담당자 옆에 '의뢰자' 컬럼 삽입(기존행은 빈칸, going-forward). 멱등. 봇 정지 후 1회 실행.
function migrateScheduleAddClient() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName("일정");
  if (!sh) { Logger.log("일정 탭 없음"); return; }
  var lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    var h1 = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
    if (h1.indexOf("의뢰자") >= 0) { Logger.log("이미 의뢰자 컬럼 있음 — skip"); return; }
    if (h1.indexOf("진행내용") < 0) { Logger.log("먼저 migrateScheduleV2 실행 필요"); return; }

    var oldHead = ["날짜", "작업내용", "진행내용", "담당자", "상태", "D-day", "원본"];
    var oldKeyCol = oldHead.length + 1;              // 8 = H
    var last = sh.getLastRow();
    var col = function (h) { return oldHead.indexOf(h) + 1; };
    var keyVals  = (last >= 2) ? sh.getRange(2, oldKeyCol, last - 1, 1).getValues() : [];
    var dateVals = (last >= 2) ? sh.getRange(2, col("날짜"), last - 1, 1).getValues() : [];
    var workVals = (last >= 2) ? sh.getRange(2, col("작업내용"), last - 1, 1).getValues() : [];
    var progVals = (last >= 2) ? sh.getRange(2, col("진행내용"), last - 1, 1).getValues() : [];
    var ownerVals= (last >= 2) ? sh.getRange(2, col("담당자"), last - 1, 1).getValues() : [];
    var statVals = (last >= 2) ? sh.getRange(2, col("상태"), last - 1, 1).getValues() : [];
    var srcForms = (last >= 2) ? sh.getRange(2, col("원본"), last - 1, 1).getFormulas() : [];

    sh.clear(); sh.clearConditionalFormatRules();
    var newHead = TABS["일정"], newKeyCol = newHead.length + 1;   // 9 = I
    initTab(sh, "일정", newHead, newKeyCol);

    var iDate = newHead.indexOf("날짜"), iWork = newHead.indexOf("작업내용"),
        iProg = newHead.indexOf("진행내용"), iOwner = newHead.indexOf("담당자"),
        iClient = newHead.indexOf("의뢰자"), iStat = newHead.indexOf("상태"),
        iDday = newHead.indexOf("D-day"), iSrc = newHead.indexOf("원본");
    var r = 2;
    for (var i = 0; i < keyVals.length; i++) {
      var key = String(keyVals[i][0] || "");
      if (!key) continue;
      var dv = dateVals[i][0];
      if (dv instanceof Date) sh.getRange(r, iDate + 1).setValue(dv).setNumberFormat("yyyy-mm-dd");
      else if (dv !== "" && dv !== null) sh.getRange(r, iDate + 1).setValue(dv);
      sh.getRange(r, iWork + 1).setValue(workVals[i][0] || "");
      sh.getRange(r, iProg + 1).setValue(progVals[i][0] || "");
      sh.getRange(r, iOwner + 1).setValue(ownerVals[i][0] || "");
      sh.getRange(r, iClient + 1).setValue("");                    // 의뢰자 — 기존행 빈칸(going-forward)
      sh.getRange(r, iStat + 1).setValue(statVals[i][0] || "진행중");
      var fml = (srcForms[i] && srcForms[i][0]) ? srcForms[i][0] : "";
      if (fml) sh.getRange(r, iSrc + 1).setFormula(fml);
      var a1 = sh.getRange(r, iDate + 1).getA1Notation();
      sh.getRange(r, iDday + 1).setFormula('=IF(ISNUMBER(' + a1 + '),' + a1 + '-TODAY(),IFERROR(DATEVALUE(TRIM(MID(' + a1 + ',FIND("~",' + a1 + ')+1,50)))-TODAY(),""))');
      sh.getRange(r, newKeyCol).setValue(key);
      r++;
    }
    resortSchedule();
    Logger.log("migrateScheduleAddClient: " + (r - 2) + "행 이전 완료 (의뢰자 컬럼 추가)");
  } finally { lock.releaseLock(); }
}

// 정산/확인필요 탭의 raw _key("123") → "123#0" 정규화. 멱등(이미 #n이면 skip).
function migrateKeysV2() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  ["정산", "확인필요"].forEach(function (name) {
    var sh = ss.getSheetByName(name); if (!sh) return;
    var head = TABS[name], keyCol = head.length + 1, last = sh.getLastRow();
    if (last < 2) return;
    var ks = sh.getRange(2, keyCol, last - 1, 1).getValues();
    for (var i = 0; i < ks.length; i++) {
      var k = String(ks[i][0] || "");
      if (k && !/#\d+$/.test(k)) sh.getRange(2 + i, keyCol).setValue(k + "#0");
    }
  });
  Logger.log("migrateKeysV2 완료");
}
