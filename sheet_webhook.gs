/**
 * Google Apps Script gắn vào Sheet "Lịch đăng Reels" để post_sheet.py ghi ngược
 * Page đăng / Trạng thái / Ghi chú vào đúng hàng (Sheet công khai không cho ghi trực tiếp).
 *
 * Cài 1 lần (2 phút):
 *  1. Mở Sheet -> Tiện ích mở rộng (Extensions) -> Apps Script -> dán toàn bộ file này, Lưu.
 *  2. Triển khai (Deploy) -> Tùy chọn triển khai mới (New deployment) -> loại: Ứng dụng web (Web app)
 *     - Thực thi với tư cách (Execute as): Tôi (Me)
 *     - Ai có quyền truy cập (Who has access): Bất kỳ ai (Anyone)
 *     -> Triển khai -> cho phép quyền -> copy "URL ứng dụng web" (…/exec).
 *  3. Dán URL đó vào SHEET_WEBHOOK trong post_sheet.py (hoặc chạy với --webhook URL).
 *  Mỗi lần sửa file này phải Deploy lại (Quản lý triển khai -> Chỉnh sửa -> Phiên bản mới).
 */

function norm_(s) {
  return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d");
}

// Tìm số cột theo từ khoá trong hàng tiêu đề (0 = không có cột đó)
function findCol_(header, keywords) {
  for (var i = 0; i < header.length; i++) {
    var h = norm_(header[i]);
    for (var k = 0; k < keywords.length; k++) if (h.indexOf(keywords[k]) >= 0) return i + 1;
  }
  return 0;
}

function doPost(e) {
  var data = JSON.parse(e.postData.contents);          // {row, gid?, sheet?, page?, status?, note?}
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = null;
  if (data.gid !== undefined && data.gid !== null) {   // tab theo gid trong link Sheet (nhiều tab)
    sh = ss.getSheets().filter(function (s) { return s.getSheetId() == data.gid; })[0] || null;
  }
  if (!sh && data.sheet) sh = ss.getSheetByName(data.sheet);
  if (!sh) sh = ss.getSheets()[0];
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
  return ContentService.createTextOutput(JSON.stringify({ ok: true, row: data.row, written: written }))
    .setMimeType(ContentService.MimeType.JSON);
}

// Mở URL /exec trên trình duyệt để kiểm tra đã triển khai đúng chưa
function doGet() {
  return ContentService.createTextOutput("sheet_webhook OK");
}
