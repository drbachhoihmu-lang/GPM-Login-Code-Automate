/**
 * Apps Script gắn vào Sheet "Lịch đăng Reels" – 3 việc:
 *   1. setupSheet()   : dựng 3 tab  Pages / Lịch đăng / Đã đăng  (chạy 1 lần, chạy lại không hỏng dữ liệu)
 *   2. doPost()       : webhook để post_sheet.py ghi Page / Trạng thái / Ghi chú vào đúng hàng
 *   3. archiveOld()   : dọn bài đã đăng/đã lên lịch quá N ngày sang tab "Đã đăng" (installNightlyTrigger() để tự chạy 3h sáng)
 *
 * Cài (1 lần):
 *  - Mở Sheet -> Tiện ích mở rộng -> Apps Script -> dán toàn bộ file này -> Lưu.
 *  - Chọn hàm setupSheet ở thanh trên -> Chạy -> cho phép quyền. Sheet có đủ 3 tab.
 *  - (tuỳ chọn) Chọn hàm installNightlyTrigger -> Chạy: mỗi đêm 3h tự dọn bài cũ sang "Đã đăng".
 *  - Triển khai -> Tùy chọn triển khai mới -> Ứng dụng web: Thực thi với tư cách: Tôi; Ai có quyền: Bất kỳ ai
 *    -> Triển khai -> copy "URL ứng dụng web" (…/exec) -> dán vào SHEET_WEBHOOK trong post_sheet.py.
 *  - Mỗi lần sửa file này phải Deploy lại (Quản lý triển khai -> Chỉnh sửa -> Phiên bản mới).
 */

var TAB_PAGES = "Pages";
var TAB_QUEUE = "Lịch đăng";
var TAB_DONE = "Đã đăng";
var ARCHIVE_AFTER_DAYS = 7;

var HEAD_PAGES = ["Tên page", "asset_id", "Profile GPM", "Người phụ trách", "Giờ đăng mặc định", "Ghi chú"];
var HEAD_QUEUE = ["Ngày đăng (dd/mm/yyyy)", "Giờ đăng (hh:mm)", "Page", "Video (link Drive / thư mục Drive / đường dẫn máy)",
                  "Tiêu đề", "Hashtag", "Trạng thái", "Ghi chú"];
var HEAD_DONE = HEAD_QUEUE.concat(["Ngày dọn"]);

// ----------------------------------------------------------------- tiện ích
function norm_(s) {
  return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d").trim();
}

// Số cột (1-based) có tiêu đề chứa 1 trong các từ khoá; 0 = không có
function findCol_(header, keywords) {
  for (var i = 0; i < header.length; i++) {
    var h = norm_(header[i]);
    for (var k = 0; k < keywords.length; k++) if (h.indexOf(keywords[k]) >= 0) return i + 1;
  }
  return 0;
}

function getOrCreate_(ss, name, header) {
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, header.length).setValues([header]);
  } else if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, header.length).setValues([header]);
  }
  var hr = sh.getRange(1, 1, 1, header.length);
  hr.setFontWeight("bold").setBackground("#1F3864").setFontColor("#FFFFFF").setWrap(true);
  sh.setFrozenRows(1);
  return sh;
}

// ----------------------------------------------------------------- 1. dựng 3 tab
function setupSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var oldFirst = ss.getSheets()[0];

  // Pages
  var pages = getOrCreate_(ss, TAB_PAGES, HEAD_PAGES);
  if (pages.getLastRow() < 2) {
    pages.getRange(2, 1, 2, HEAD_PAGES.length).setValues([
      ["Thạc sĩ A.Đức - Rối loạn lo âu", "1105165602680340", "001", "", "19:00", "ví dụ – sửa lại cho đúng"],
      ["Thạc sĩ A.Đức - Trầm Cảm",       "1056835234187428", "002", "", "19:00", "ví dụ – sửa lại cho đúng"]
    ]);
  }
  pages.getRange("B2:B").setNumberFormat("@");            // asset_id là chữ, không bị làm tròn
  pages.setColumnWidths(1, 1, 260); pages.setColumnWidths(2, 1, 150); pages.setColumnWidths(3, 1, 110);
  pages.setColumnWidths(4, 1, 140); pages.setColumnWidths(5, 1, 130); pages.setColumnWidths(6, 1, 260);

  // Lịch đăng
  var queue = getOrCreate_(ss, TAB_QUEUE, HEAD_QUEUE);
  queue.getRange("A2:B").setNumberFormat("@");            // ngày / giờ giữ nguyên chữ, không bị đổi kiểu
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInRange(pages.getRange("A2:A"), true).setAllowInvalid(true)
    .setHelpText("Chọn page trong tab Pages").build();
  queue.getRange("C2:C").setDataValidation(rule);
  var stRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["", "bỏ qua", "Đang đăng", "Đã lên lịch", "Đã đăng", "Lỗi", "Chạy thử OK"], true)
    .setAllowInvalid(true).build();
  queue.getRange("G2:G").setDataValidation(stRule);
  var widths = [130, 90, 230, 320, 320, 200, 110, 260];
  for (var wi = 0; wi < widths.length; wi++) queue.setColumnWidths(wi + 1, 1, widths[wi]);

  // Chuyển hàng từ tab mẫu cũ (nếu có) sang Lịch đăng
  if (oldFirst && [TAB_PAGES, TAB_QUEUE, TAB_DONE].indexOf(oldFirst.getName()) < 0 && oldFirst.getLastRow() >= 2) {
    var data = oldFirst.getDataRange().getValues();
    var h = data[0];
    var c = { time: findCol_(h, ["thoi gian", "gio dang", "lich"]), date: findCol_(h, ["ngay dang"]),
              page: findCol_(h, ["page", "trang dang"]),
              video: findCol_(h, ["video", "link", "duong dan"]), title: findCol_(h, ["tieu de", "caption"]),
              tag: findCol_(h, ["hashtag", "hastag"]), status: findCol_(h, ["trang thai"]), note: findCol_(h, ["ghi chu"]) };
    var rows = [];
    for (var i = 1; i < data.length; i++) {
      var r = data[i];
      if (!r.join("").trim()) continue;
      var g = function (k) { return c[k] > 0 ? String(r[c[k] - 1]).replace(/^'/, "") : ""; };
      var dt = g("date"), tm = g("time");
      if (!c.date) { var parts = tm.split(/\s+/); dt = parts[0] || ""; tm = parts[1] || ""; }   // tách cột gộp cũ
      rows.push([dt, tm, g("page"), g("video"), g("title"), g("tag"), g("status"), g("note")]);
    }
    if (rows.length) {
      queue.getRange(queue.getLastRow() + 1, 1, rows.length, HEAD_QUEUE.length).setValues(rows);
      oldFirst.setName("Mẫu cũ (có thể xoá)");
      oldFirst.hideSheet();
    }
  }

  // Đã đăng
  var done = getOrCreate_(ss, TAB_DONE, HEAD_DONE);
  done.getRange("A2:A").setNumberFormat("@");

  // Thứ tự tab: Lịch đăng, Pages, Đã đăng
  ss.setActiveSheet(queue); ss.moveActiveSheet(1);
  ss.setActiveSheet(pages); ss.moveActiveSheet(2);
  ss.setActiveSheet(done);  ss.moveActiveSheet(3);
  ss.setActiveSheet(queue);
  SpreadsheetApp.flush();
}

// ----------------------------------------------------------------- 2. webhook ghi trạng thái
// post_sheet.py --scan-pages --webhook: thêm/cập nhật fanpage vào tab Pages (khớp theo asset_id)
function upsertPages_(ss, pages) {
  var sh = ss.getSheetByName(TAB_PAGES);
  if (!sh) { sh = getOrCreate_(ss, TAB_PAGES, HEAD_PAGES); }
  var header = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  var cName = findCol_(header, ["ten page", "ten trang", "page"]) || 1;
  var cAsset = findCol_(header, ["asset", "id"]) || 2;
  var cProfile = findCol_(header, ["profile", "gpm"]) || 3;
  var last = sh.getLastRow();
  var assets = last >= 2 ? sh.getRange(2, cAsset, last - 1, 1).getValues().map(function (r) { return String(r[0]).trim(); }) : [];
  var added = 0, updated = 0;
  pages.forEach(function (p) {
    var idx = assets.indexOf(String(p.asset).trim());
    if (idx >= 0) {
      var row = idx + 2;
      sh.getRange(row, cName).setValue(p.name);
      if (!String(sh.getRange(row, cProfile).getValue()).trim() && p.profile) sh.getRange(row, cProfile).setValue(p.profile);
      updated++;
    } else {
      var newRow = sh.getLastRow() + 1;
      sh.getRange(newRow, cName).setValue(p.name);
      sh.getRange(newRow, cAsset).setNumberFormat("@").setValue(String(p.asset));
      sh.getRange(newRow, cProfile).setValue(p.profile || "");
      assets.push(String(p.asset).trim());
      added++;
    }
  });
  return { ok: true, added: added, updated: updated };
}

function doPost(e) {
  var data = JSON.parse(e.postData.contents);          // {row, gid?, sheet?, page?, status?, note?} | {action:"upsert_pages", pages:[...]}
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (data.action === "upsert_pages") {
    return ContentService.createTextOutput(JSON.stringify(upsertPages_(ss, data.pages || [])))
      .setMimeType(ContentService.MimeType.JSON);
  }
  var sh = null;
  if (data.gid !== undefined && data.gid !== null) {   // tab theo gid trong link Sheet
    sh = ss.getSheets().filter(function (s) { return s.getSheetId() == data.gid; })[0] || null;
  }
  if (!sh && data.sheet) sh = ss.getSheetByName(data.sheet);
  if (!sh) sh = ss.getSheetByName(TAB_QUEUE) || ss.getSheets()[0];
  var header = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  var cols = {
    page:   findCol_(header, ["page", "fanpage", "trang dang"]),
    status: findCol_(header, ["trang thai", "status"]),
    note:   findCol_(header, ["ghi chu", "note"])
  };
  var written = [];
  for (var key in cols) {
    if (cols[key] > 0 && data[key] !== undefined && data[key] !== null) {
      sh.getRange(Number(data.row), cols[key]).setValue(data[key]);
      written.push(key);
    }
  }
  return ContentService.createTextOutput(JSON.stringify({ ok: true, row: data.row, sheet: sh.getName(), written: written }))
    .setMimeType(ContentService.MimeType.JSON);
}

// Mở URL /exec trên trình duyệt để kiểm tra đã triển khai đúng chưa
function doGet() {
  return ContentService.createTextOutput("sheet_webhook OK");
}

// ----------------------------------------------------------------- 3. dọn bài cũ
function parseVnTime_(s) {               // "08/10/2026 19:00" -> Date (null nếu không hiểu)
  var m = String(s || "").match(/(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})/);
  if (m) return new Date(+m[3], +m[2] - 1, +m[1], +m[4], +m[5]);
  m = String(s || "").match(/(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})/);
  if (m) return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  return null;
}

function archiveOld() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var queue = ss.getSheetByName(TAB_QUEUE), done = ss.getSheetByName(TAB_DONE);
  if (!queue || !done) return;
  var data = queue.getDataRange().getValues();
  if (data.length < 2) return;
  var h = data[0];
  var cDate = findCol_(h, ["ngay dang"]), cTime = findCol_(h, ["thoi gian", "gio dang", "lich"]),
      cStatus = findCol_(h, ["trang thai", "status"]);
  if ((!cTime && !cDate) || !cStatus) return;
  var cutoff = new Date(Date.now() - ARCHIVE_AFTER_DAYS * 86400000);
  var stamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "dd/MM/yyyy HH:mm");
  var moved = [], keepIdx = [];
  for (var i = 1; i < data.length; i++) {
    var st = norm_(data[i][cStatus - 1]);
    var when = cDate ? String(data[i][cDate - 1]) + " " + (cTime ? String(data[i][cTime - 1]) : "00:00")
                     : String(data[i][cTime - 1]);
    var t = parseVnTime_(when);
    if (st.indexOf("da ") === 0 && t && t < cutoff) moved.push(data[i].concat([stamp]));
    else keepIdx.push(i);
  }
  if (!moved.length) return;
  done.getRange(done.getLastRow() + 1, 1, moved.length, moved[0].length).setValues(moved);
  // xoá từ dưới lên để không lệch chỉ số
  for (var r = data.length - 1; r >= 1; r--) if (keepIdx.indexOf(r) < 0) queue.deleteRow(r + 1);
}

function installNightlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === "archiveOld") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("archiveOld").timeBased().everyDays(1).atHour(3).create();
}
