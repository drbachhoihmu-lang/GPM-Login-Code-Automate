"""
Đăng / lên lịch hàng loạt Thước phim theo một Google Sheet chia sẻ công khai.

Sheet gồm 3 tab (dựng bằng setupSheet() trong sheet_webhook.gs):
  "Lịch đăng" (hàng đợi, script đọc) – hàng 1 là tiêu đề cột, tên cột linh hoạt (nhận diện theo từ khoá):
    Ngày đăng + Giờ  : 2 cột "08/10/2026" | "19:00" (hoặc 1 cột gộp "08/10/2026 19:00"); ghi "ngay" để đăng ngay
                       Để TRỐNG cả hai -> tự xếp vào khung giờ mặc định trống kế tiếp của page (tính từ hiện tại,
                       không trùng bài đã có); chỉ ghi Ngày -> khung trống đầu tiên của ngày đó; chỉ ghi Giờ -> hôm nay/mai
                       --mode schedule (mặc định): lên lịch trên Facebook ngay khi chạy, Facebook tự phát đúng giờ
                       --mode due: tới đúng ngày/giờ đó phần mềm mới bấm "Chia sẻ ngay"; hàng chưa tới giờ để lại
    Page             : tên page (chọn từ tab Pages) -> script tra asset_id + profile GPM trong tab Pages.
                       Quy ước MỖI PROFILE MỘT FANPAGE: page không gắn với profile nào (không có trong tab Pages,
                       chưa ghi Profile GPM, hoặc ghi trùng profile với page khác) -> hàng ghi 'Lỗi' kèm lý do,
                       KHÔNG mượn profile khác để đổi sang page đó
    Video            : link file Drive / link thư mục Drive / đường dẫn file trên máy
    Tiêu đề          : mô tả bài (có thể kèm luôn hashtag); trống + thư mục Drive -> lấy 'Tiêu đề:' trong .docx
    Hashtag          : (tuỳ chọn) nối vào sau tiêu đề
    Trạng thái       : script ghi (Đang đăng / Đã lên lịch / Đã đăng / Lỗi); tay ghi "bỏ qua" để không đăng
    Ghi chú          : script ghi lý do lỗi / giờ xác nhận
    (Profile         : tuỳ chọn, ghi đè profile lấy từ tab Pages)
  "Pages" (danh mục): Tên page | asset_id | Profile GPM | Người phụ trách | Giờ đăng mặc định | Ghi chú
                      Giờ đăng mặc định = các khung giờ, vd "07:00, 10:00, 12:00, 14:00, 18:00" (web sửa được: tab Giờ đăng)
  "Đã đăng" (lưu trữ): phần mềm dọn bài Đã đăng/Đã lên lịch của các ngày trước hôm qua sang đây, mỗi ngày một lượt (--archive)

Thứ tự đăng: mỗi profile GPM gắn một fanpage (tab Pages), không đổi page bên trong một profile. Mở tối đa N profile
cùng lúc (--open-max, mặc định 3), mỗi lượt đăng 1 bài của một profile rồi chuyển sang profile kế -> các page được đăng
xen kẽ, không đăng liền nhiều bài một page. Profile hết bài thì đóng, mở profile kế tiếp, tới khi hết hàng đợi.
Các cửa sổ Chrome đang mở cùng lúc được xếp cạnh nhau (chia đều màn hình: 2 nửa / 3 cột / lưới), không đè lên nhau.

Ví dụ:
  python post_sheet.py --sheet "https://docs.google.com/spreadsheets/d/XXXX/edit?gid=0"
  python post_sheet.py --sheet "..." --dry-run          # chạy thử, không bấm nút cuối
  python post_sheet.py --sheet "..." --max 1            # chỉ đăng 1 bài đầu tiên đang chờ
  python post_sheet.py --sheet "..." --rows 3,5         # chỉ đăng hàng 3 và 5 (số hàng trên Sheet)
"""
import argparse
import contextlib
import csv
import io
import json
import random
import re
import sys
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from post_reel import (LOG_DIR, PAGE_ASSET_ID, PROFILE_ID, VIDEO_EXT, fetch_from_drive, gpm_check, gpm_profiles, gpm_start,
                       gpm_stop, log, parse_schedule, pick_page, place_window, post_reel, resolve_asset_id, resolve_profile_id,
                       scan_pages, shot)

# Link Sheet mặc định (có thể thay bằng --sheet)
SHEET_URL = ""
TAB_QUEUE, TAB_PAGES = "Lịch đăng", "Pages"     # tên tab hàng đợi / danh mục page (đổi bằng --tab, --pages-tab)
TAB_DONE = "Đã đăng"                                  # tab lưu trữ: bài đã đăng của các ngày trước được dọn sang đây
# URL ứng dụng web Apps Script (xem sheet_webhook.gs) để ghi Page đăng / Trạng thái / Ghi chú ngược vào Sheet.
# Để trống = không ghi (vẫn có báo cáo CSV trong logs/).
SHEET_WEBHOOK = ""
STATE_FILE = LOG_DIR / "sheet_state.json"      # ghi nhớ hàng nào đã đăng để không đăng lại

# Giá trị cột Trạng thái do script ghi
ST_RUNNING, ST_SCHEDULED, ST_POSTED, ST_ERROR, ST_DRY = "Đang đăng", "Đã lên lịch", "Đã đăng", "Lỗi", "Chạy thử OK"
ST_WAITING = "Chờ đăng"                           # nút 'Xếp lịch' ghi vào hàng đang trống trạng thái
# Cách đăng:  "schedule" = lên lịch trên Facebook ngay khi chạy (Facebook tự phát đúng giờ ghi trong Sheet)
#             "due"      = tới đúng ngày/giờ ghi trong Sheet phần mềm mới bấm "Chia sẻ ngay"; hàng chưa tới giờ để lại
POST_MODES = ("schedule", "due")
PAST_FIX = "ghi Ngày/Giờ mới (hoặc xoá Ngày/Giờ để tự xếp) rồi xoá ô Trạng thái để đăng lại"


def sheet_gid(link):
    """gid của tab trong link Sheet (None nếu link không có -> tab đầu tiên)."""
    m = re.search(r"[#&?]gid=(\d+)", link or "")
    return int(m.group(1)) if m else None


# "Cách ghi vào Sheet" (--webhook / ô webhook ở Cài đặt) nhận 1 trong 2:
#   - URL Apps Script …/exec  : POST JSON, script chạy với tư cách chủ Sheet rồi điền ô (sheet_webhook.gs)
#   - đường dẫn file .json    : service account Google (Sheets API, gspread) – Sheet phải cho email trong file
#                               quyền Người chỉnh sửa (hoặc đang chia sẻ "Bất kỳ ai có link → chỉnh sửa")
_SA = {"file": None, "client": None}
_WRITE_LINK = {"link": None}          # link Sheet của lần prepare() gần nhất -> service account biết ghi vào file nào


def is_sa_file(writer):
    return bool(writer) and str(writer).strip().lower().endswith(".json")


def _sa_client(sa_file):
    if _SA["client"] is None or _SA["file"] != sa_file:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(sa_file, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        _SA["client"], _SA["file"] = gspread.authorize(creds), sa_file
    return _SA["client"]


def _sa_ws(sa_file, link, gid=None, tab=None):
    """Worksheet theo gid > tên tab > tab đầu."""
    sh = _sa_client(sa_file).open_by_key(sheet_id(link))
    if gid is not None:
        ws = next((w for w in sh.worksheets() if w.id == int(gid)), None)
        if ws is not None:
            return ws
    if tab:
        try:
            return sh.worksheet(tab)
        except Exception:
            pass
    return sh.get_worksheet(0)


def _sa_update(sa_file, link, row, gid, fields):
    import gspread
    ws = _sa_ws(sa_file, link, gid, TAB_QUEUE)
    cols = map_columns(ws.row_values(1), required=())
    if "date" not in cols and "time" in cols and fields.get("date") is not None:     # bố cục 1 cột gộp
        fields = {**fields, "time": f"{fields['date']} {fields.get('time') or ''}".strip(), "date": None}
    cells = [gspread.Cell(int(row), cols[k] + 1, str(v)) for k, v in fields.items() if v is not None and k in cols]
    if cells:
        ws.update_cells(cells, value_input_option="RAW")           # RAW: "10/10/2026" giữ là chữ, không bị đổi kiểu
    return True


def sheet_update(webhook, row, gid=None, link=None, **fields):
    """Ghi các ô (page/status/note/date/time) của hàng `row` (ở tab `gid`) qua Apps Script hoặc service account.
    Lỗi chỉ cảnh báo, không dừng."""
    if not webhook:
        return False
    try:
        if is_sa_file(webhook):
            link = link or _WRITE_LINK["link"]
            if not link:
                raise RuntimeError("chưa biết link Sheet")
            return _sa_update(webhook, link, row, gid, fields)
        r = requests.post(webhook, json={"row": row, "gid": gid, **fields}, timeout=30)
        if r.status_code != 200 or '"ok"' not in r.text:
            log(f"  (webhook trả về {r.status_code}: {r.text[:120]})")
            return False
        return True
    except Exception as e:
        log(f"  (không ghi được vào Sheet: {str(e)[:120]})")
        return False


def _sa_upsert_pages(sa_file, link, pages, tab=TAB_PAGES):
    """Như upsertPages_ trong sheet_webhook.gs: tìm theo asset_id -> cập nhật (tên, profile nếu trống, giờ) / thêm hàng mới."""
    import gspread
    ws = _sa_ws(sa_file, link, None, tab)
    vals = ws.get_all_values()
    header = vals[0] if vals else []
    cols = map_columns(header, PAGES_KEYS, required=("name", "asset")) if header else {}
    c_name, c_asset, c_prof, c_hours = cols.get("name", 0), cols.get("asset", 1), cols.get("profile", 2), cols.get("hour", 4)
    cell = lambda r, c: r[c].strip() if c < len(r) else ""
    assets = [cell(r, c_asset) for r in vals[1:]]
    added = updated = 0
    cells = []
    for p in pages:
        a = str(p.get("asset") or "").strip()
        if not a:
            continue
        if a in assets:
            row = assets.index(a) + 2
            if p.get("name"):
                cells.append(gspread.Cell(row, c_name + 1, p["name"]))
            if p.get("profile") and not cell(vals[row - 1], c_prof):
                cells.append(gspread.Cell(row, c_prof + 1, p["profile"]))
            if p.get("hours") is not None:
                cells.append(gspread.Cell(row, c_hours + 1, str(p["hours"])))
            updated += 1
        elif p.get("name"):                      # chỉ cập nhật giờ cho page chưa có trong tab -> bỏ qua
            new = [""] * max(len(header), c_hours + 1)
            new[c_name], new[c_asset], new[c_prof] = p["name"], a, p.get("profile") or ""
            if p.get("hours") is not None:
                new[c_hours] = str(p["hours"])
            ws.append_row(new, value_input_option="RAW")
            assets.append(a)
            added += 1
    if cells:
        ws.update_cells(cells, value_input_option="RAW")
    return added, updated


def sheet_upsert_pages(webhook, link, pages):
    """Thêm/cập nhật tab Pages ([{asset, name?, profile?, hours?}]) qua webhook (action upsert_pages) hoặc service account.
    Trả về (ok, chuỗi kết quả để hiện/log)."""
    if not webhook:
        return False, "chưa có webhook / service account"
    try:
        if is_sa_file(webhook):
            added, updated = _sa_upsert_pages(webhook, link, pages)
            return True, f"đã ghi tab Pages trên Sheet (thêm {added}, cập nhật {updated})"
        r = requests.post(webhook, json={"action": "upsert_pages", "pages": pages}, timeout=60)
        ok = r.status_code == 200 and '"ok"' in r.text
        return ok, ("đã ghi tab Pages trên Sheet" if ok else f"Sheet: {r.text[:120]}")
    except Exception as e:
        return False, f"không ghi được tab Pages: {str(e)[:120]}"



# ----------------------------------------------------------------- dọn bài đã đăng sang tab "Đã đăng"
def _cell(row, cols, key):
    i = cols.get(key)
    return row[i].strip().lstrip("'").strip() if i is not None and i < len(row) else ""


def _row_date(row, cols):
    """Ngày đăng (date) của một hàng trên Sheet: cột Ngày riêng, hoặc phần ngày của cột gộp 'dd/mm/yyyy hh:mm'. None = không đọc được."""
    txt = _cell(row, cols, "date") if "date" in cols else (_cell(row, cols, "time").split() or [""])[0]
    try:
        return datetime.strptime(norm_date(txt), "%d/%m/%Y").date()
    except ValueError:
        return None


def archive_pick(vals, keep_from):
    """Chọn hàng cần dọn trong toàn bộ giá trị tab Lịch đăng (vals[0] = tiêu đề): Trạng thái 'Đã …' (Đã đăng / Đã lên lịch)
    và ngày đăng < keep_from (date). Trả về (picks [(chỉ số trong vals, hàng, ngày)], số hàng 'Đã …' không đọc được ngày, cols)."""
    cols = map_columns(vals[0], required=())
    if "status" not in cols or ("date" not in cols and "time" not in cols):
        raise RuntimeError(f"Tab hàng đợi thiếu cột Trạng thái / Ngày đăng. Tiêu đề hiện có: {vals[0]}")
    picks, nodate = [], 0
    for i in range(1, len(vals)):
        r = vals[i]
        if not any(str(c).strip() for c in r):
            continue
        if not norm(_cell(r, cols, "status")).startswith("da "):
            continue
        d = _row_date(r, cols)
        if d is None:
            nodate += 1
        elif d < keep_from:
            picks.append((i, list(r), d))
    return picks, nodate, cols


def _sa_archive(sa_file, link, queue_tab, done_tab, keep_from, stamp, dry_run=False):
    import gspread
    sh = _sa_client(sa_file).open_by_key(sheet_id(link))
    ws_q = sh.worksheet(queue_tab)
    vals = ws_q.get_all_values()
    if len(vals) < 2:
        return {"ok": True, "moved": 0, "days": {}, "nodate": 0}
    picks, nodate, cols = archive_pick(vals, keep_from)
    days = {}
    for _, _, d in picks:
        days[d.strftime("%d/%m/%Y")] = days.get(d.strftime("%d/%m/%Y"), 0) + 1
    if not picks or dry_run:
        return {"ok": True, "moved": 0, "would_move": len(picks), "days": days, "nodate": nodate}
    header = vals[0]
    try:
        ws_d = sh.worksheet(done_tab)
    except gspread.WorksheetNotFound:
        ws_d = sh.add_worksheet(done_tab, rows=1000, cols=len(header) + 1)
    if not any(str(c).strip() for c in (ws_d.row_values(1) or [])):
        ws_d.update("A1", [list(header) + ["Ngày dọn"]], value_input_option="RAW")
    width = max(len(header), max(len(r) for _, r, _ in picks))
    out = [(r + [""] * (width - len(r)))[:width] + [stamp] for _, r, _ in picks]
    ws_d.append_rows(out, value_input_option="RAW")          # RAW: ngày/giờ giữ là chữ
    # Xoá khỏi hàng đợi, từ dưới lên; đọc lại trước khi xoá để không xoá nhầm nếu Sheet vừa bị sửa tay
    fresh = ws_q.get_all_values()

    def same(i, r):
        f = fresh[i] if i < len(fresh) else []
        return all(_cell(f, cols, k) == _cell(r, cols, k) for k in ("video", "status", "date", "time") if k in cols)
    bad = [i + 1 for i, r, _ in picks if not same(i, r)]
    if bad:
        return {"ok": False, "moved": 0, "days": days, "nodate": nodate,
                "error": f"Sheet vừa thay đổi (hàng {bad[:5]}): đã chép {len(picks)} hàng sang '{done_tab}' nhưng KHÔNG xoá khỏi "
                         f"'{queue_tab}' – xoá tay các hàng trùng rồi dọn lại"}
    idx = sorted((i + 1 for i, _, _ in picks), reverse=True)        # số hàng 1-based, xoá từ dưới lên theo từng dải liền nhau
    while idx:
        end = start = idx.pop(0)
        while idx and idx[0] == start - 1:
            start = idx.pop(0)
        ws_q.delete_rows(start, end)
    return {"ok": True, "moved": len(picks), "days": days, "nodate": nodate}


def sheet_archive(webhook, link, queue_tab=TAB_QUEUE, done_tab=TAB_DONE, keep_days=1, now=None, dry_run=False):
    """Dọn bài đã đăng: hàng Trạng thái 'Đã đăng' / 'Đã lên lịch' có ngày đăng cũ hơn (hôm nay - keep_days) được chuyển
    NGUYÊN CẢ NGÀY một lượt sang tab done_tab (thêm cột 'Ngày dọn'), rồi xoá khỏi queue_tab. keep_days=1: giữ hôm nay + hôm qua.
    Hàng 'Đã …' không đọc được ngày (vd ô Ngày ghi 'ngay') được giữ lại, đếm ở 'nodate'. Ghi qua service account (gspread)
    hoặc Apps Script (action 'archive'). Trả về dict: ok, moved, days {dd/mm/yyyy: n}, nodate, error."""
    now = now or datetime.now()
    keep_from = now.date() - timedelta(days=keep_days)
    if not webhook:
        return {"ok": False, "moved": 0, "error": "chưa có cách ghi vào Sheet (webhook / service_account.json)"}
    try:
        if is_sa_file(webhook):
            return _sa_archive(webhook, link, queue_tab, done_tab, keep_from, now.strftime("%d/%m/%Y %H:%M"), dry_run)
        r = requests.post(webhook, json={"action": "archive", "keep_from": keep_from.strftime("%d/%m/%Y"),
                                         "queue": queue_tab, "done": done_tab, "dry_run": bool(dry_run)}, timeout=60)
        if r.status_code != 200:
            return {"ok": False, "moved": 0, "error": f"webhook trả về {r.status_code}: {r.text[:120]}"}
        return r.json()
    except Exception as e:
        return {"ok": False, "moved": 0, "error": str(e)[:200]}


def archive_summary(res):
    """Một dòng mô tả kết quả sheet_archive() cho log / giao diện."""
    if not res.get("ok"):
        return "lỗi dọn: " + str(res.get("error") or "?")
    days = res.get("days") or {}
    txt = ", ".join(f"{d}: {n} bài" for d, n in sorted(days.items(), key=lambda x: datetime.strptime(x[0], "%d/%m/%Y")))
    if res.get("moved"):
        out = f"đã chuyển {res['moved']} bài sang tab '{TAB_DONE}' ({txt})"
    elif res.get("would_move"):
        out = f"sẽ chuyển {res['would_move']} bài ({txt})"
    else:
        out = "không có bài nào đủ điều kiện dọn (chỉ dọn bài của các ngày trước hôm qua)"
    if res.get("nodate"):
        out += f"; {res['nodate']} bài 'Đã …' không đọc được ngày nên giữ lại"
    return out


def cmd_archive(webhook, link, queue_tab, keep_days, dry_run):
    res = sheet_archive(webhook, link, queue_tab, TAB_DONE, keep_days, dry_run=dry_run)
    log(("(chạy thử) " if dry_run else "") + archive_summary(res))
    return 0 if res.get("ok") else 1


# ----------------------------------------------------------------- đọc Sheet
def sheet_id(link):
    m = re.search(r"/spreadsheets/d/([\w-]+)", link or "")
    if not m:
        raise ValueError(f"Không nhận ra link Google Sheet: {link}")
    return m.group(1)


def sheet_tabs(link):
    """{tên tab: gid} của Sheet công khai (đọc từ trang htmlview). {} nếu không đọc được."""
    try:
        r = requests.get(f"https://docs.google.com/spreadsheets/d/{sheet_id(link)}/htmlview", timeout=60)
        if r.status_code != 200:
            return {}
        import html as _html
        tabs = {}
        # htmlview nhúng danh sách tab dạng JS: items.push({name: "Tên", pageUrl: "...htmlview?gid=123..."})
        for name, url in re.findall(r'items\.push\(\{name:\s*"((?:[^"\\]|\\.)*)",\s*pageUrl:\s*"((?:[^"\\]|\\.)*)"', r.text):
            m = re.search(r"gid=(\d+)", url.replace("\\/", "/"))
            if m:
                tabs[json.loads(f'"{name}"').strip()] = int(m.group(1))
        if not tabs:   # dự phòng: menu tab dạng HTML
            tabs = {_html.unescape(n).strip(): int(g)
                    for g, n in re.findall(r'id="sheet-button-(\d+)"[^>]*>\s*<a[^>]*>([^<]+)</a>', r.text)}
        return tabs
    except Exception:
        return {}


def sheet_csv_url(link, tab=None):
    """Link export CSV: tab theo tên (tra gid qua htmlview) > gid trong link > tab đầu tiên.
    Trả về (url, gid|None). Lưu ý gviz 'sheet=tên' KHÔNG dùng được: tên sai nó âm thầm trả tab đầu."""
    sid = sheet_id(link)
    gid = None
    if tab:
        tabs = sheet_tabs(link)
        hit = next((g for n, g in tabs.items() if norm(n) == norm(tab)), None)
        if hit is None:
            return None, None
        gid = hit
    else:
        m = re.search(r"[#&?]gid=(\d+)", link)
        gid = int(m.group(1)) if m else None
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv" + (f"&gid={gid}" if gid is not None else ""), gid


def norm(s):
    """Chữ thường, bỏ dấu, bỏ ký tự thừa – để so tên cột."""
    s = unicodedata.normalize("NFKD", str(s or "").replace("Đ", "D").replace("đ", "d"))
    return re.sub(r"[^a-z0-9]+", " ", s.encode("ascii", "ignore").decode().lower()).strip()


COLUMN_KEYS = {           # tab Lịch đăng: tên cột chuẩn -> các từ khoá nhận diện (sau khi bỏ dấu)
    "profile": ("profile", "tai khoan", "account", "nick", "gpm"),
    "page":    ("page", "fanpage", "trang dang"),
    "video":   ("video", "link", "drive", "duong dan", "path", "file"),
    "title":   ("tieu de", "caption", "mo ta", "noi dung", "title"),
    "hashtag": ("hashtag", "hastag", "tag"),
    "date":    ("ngay dang", "ngay", "date"),                       # cột Ngày riêng (dd/mm/yyyy)
    "time":    ("thoi gian", "gio dang", "gio", "lich", "schedule", "time", "hour"),   # cột Giờ riêng, hoặc cột gộp
    "status":  ("trang thai", "status"),
    "note":    ("ghi chu", "note"),
}
PAGES_KEYS = {            # tab Pages
    "name":    ("ten page", "ten trang", "page", "fanpage", "trang"),
    "asset":   ("asset", "id"),
    "profile": ("profile", "tai khoan", "gpm"),
    "owner":   ("phu trach", "owner"),
    "hour":    ("gio", "hour"),
}


def map_columns(header, keys=COLUMN_KEYS, required=("video",)):
    cols = {}
    for idx, name in enumerate(header):
        n = norm(name)
        if not n:
            continue
        for key, kws in keys.items():
            if key not in cols and any(kw in n for kw in kws):
                cols[key] = idx
                break
    missing = [k for k in required if k not in cols]
    if keys is COLUMN_KEYS and "time" not in cols and "date" not in cols:
        missing.append("thời gian / ngày đăng")
    if missing:
        raise RuntimeError(f"Sheet thiếu cột {missing}. Tiêu đề cột hiện có: {header}")
    return cols


def norm_time(s):
    """'19h', '19h30', '7:00 PM', '19:00:00' -> 'HH:MM' ('' nếu trống)."""
    s = (s or "").strip().lower().replace("h", ":").replace(" ", "").rstrip(":")
    if not s:
        return ""
    m = re.match(r"^(\d{1,2})(?::(\d{1,2}))?(?::\d{1,2})?(am|pm|sa|ch)?$", s)
    if not m:
        return s
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap in ("pm", "ch") and h < 12:
        h += 12
    if ap in ("am", "sa") and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def norm_date(s):
    """'8/10/2026', '08/10/2026', '2026-10-08', '08-10-2026' -> 'dd/mm/yyyy' (giữ nguyên nếu không hiểu)."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", s)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(3)):02d}/{int(m.group(2)):02d}/{m.group(1)}"
    return s


def parse_hours(s):
    """Khung giờ của page: '07, 10, 12:30, 14h, 18' / '07:00; 10:00' / list -> ['07:00', '10:00', ...] (sắp xếp, bỏ trùng)."""
    if isinstance(s, (list, tuple)):
        s = ",".join(str(x) for x in s)
    out = set()
    for tok in re.split(r"[,;/|\n]+|\s+", str(s or "")):
        t = norm_time(tok)
        if re.fullmatch(r"\d{2}:\d{2}", t) and int(t[:2]) < 24 and int(t[3:]) < 60:
            out.add(t)
    return sorted(out)


def fetch_rows(link, tab=None):
    """Các hàng CSV của một tab (None nếu tab không tồn tại). `link` có thể là file .csv trên máy."""
    if link.lower().endswith(".csv") and Path(link).is_file():
        text = Path(link).read_text(encoding="utf-8-sig")
    else:
        url, _ = sheet_csv_url(link, tab)
        if url is None:
            return None
        r = requests.get(url, timeout=60)
        if r.status_code != 200 or "<html" in r.text[:200].lower():
            raise RuntimeError("Không tải được Sheet – kiểm tra link và chia sẻ 'Bất kỳ ai có đường liên kết' (Người xem)")
        text = r.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def read_sheet(link, tab=None):
    """Hàng đợi: danh sách dict row (số hàng trên Sheet), profile, page, video, title, hashtag, time, status, note.
    `tab` = tên tab hàng đợi; không có tab đó thì dùng gid trong link / tab đầu tiên."""
    rows = fetch_rows(link, tab)
    if rows is None:
        log(f"(không có tab '{tab}', đọc tab đầu tiên / tab trong link)")
        rows = fetch_rows(link, None)
    if not rows:
        raise RuntimeError("Sheet trống")
    cols = map_columns(rows[0])
    # Bỏ dấu ' đầu ô (dùng để giữ "08/10/2026 19:00" là chữ, không bị Sheets đổi thành ngày)
    get = lambda row, key: (row[cols[key]].strip().lstrip("'").strip() if key in cols and cols[key] < len(row) else "")
    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        d, t = get(row, "date"), get(row, "time")
        if "date" not in cols:                   # bố cục gộp "dd/mm/yyyy hh:mm" (hoặc chỉ ngày / chỉ giờ)
            parts = t.split()
            if len(parts) >= 2:
                d, t = parts[0], " ".join(parts[1:])
            elif re.match(r"^(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}|\d{4}-\d{1,2}-\d{1,2})$", t):
                d, t = t, ""
            else:
                d = ""
        immediate = norm(d) in ("ngay", "now", "dang ngay") or norm(t) in ("ngay", "now", "dang ngay")
        date = "" if immediate else norm_date(d)
        hour = "" if immediate else norm_time(t)
        if immediate:
            when = "ngay"
        elif date and hour:
            when = f"{date} {hour}"
        else:
            when = ""                            # thiếu ngày và/hoặc giờ -> tự xếp theo khung giờ của page (assign_slots)
        out.append({"row": i, "profile": get(row, "profile"), "page": get(row, "page"), "video": get(row, "video"),
                    "title": get(row, "title"), "hashtag": get(row, "hashtag"), "time": when,
                    "date": date, "hour": hour, "date_only": bool(date and not hour),
                    "status": get(row, "status"), "note": get(row, "note")})
    return out


def read_pages(link, tab=TAB_PAGES):
    """Tab Pages -> [{name, asset, profile, owner, hour}]; [] nếu không có tab (hoặc link là file .csv)."""
    if link.lower().endswith(".csv"):
        return []
    try:
        rows = fetch_rows(link, tab)
    except Exception as e:
        log(f"(không đọc được tab {tab}: {str(e)[:80]})")
        return []
    if not rows or len(rows) < 2:
        return []
    try:
        cols = map_columns(rows[0], PAGES_KEYS, required=("name", "asset"))
    except RuntimeError:
        return []
    get = lambda row, key: (row[cols[key]].strip().lstrip("'").strip() if key in cols and cols[key] < len(row) else "")
    out = []
    for row in rows[1:]:
        if get(row, "name"):
            out.append({"name": get(row, "name"), "asset": get(row, "asset"), "profile": get(row, "profile"),
                        "owner": get(row, "owner"), "hour": get(row, "hour")})
    return out


def lookup_page(pages, want):
    """Tìm page trong tab Pages theo tên (một phần, không dấu) hoặc asset_id."""
    w = norm(want)
    if not w:
        return None
    exact = [p for p in pages if norm(p["name"]) == w or p["asset"] == want.strip()]
    if exact:
        return exact[0]
    hits = [p for p in pages if w in norm(p["name"])]
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------- trạng thái cục bộ
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def row_key(sheet_link, item):
    m = re.search(r"/spreadsheets/d/([\w-]+)", sheet_link)
    sid = m.group(1) if m else Path(sheet_link).stem
    return f"{sid}:r{item['row']}:{item['video']}"


# ----------------------------------------------------------------- lập kế hoạch
def plan(items, sheet_link, state, only_rows=None, retry_errors=False, mode="schedule", now=None, horizon_days=None,
         window_min=20):
    """Chia hàng thành: sẽ đăng (kèm when) / bỏ qua (kèm lý do).
    Trạng thái trống hoặc 'Chờ đăng' -> sẽ đăng. 'Đã …' (Đã đăng / Đã lên lịch), 'bỏ qua', 'skip', 'done',
    'xong' -> bỏ qua; 'Lỗi' (trên Sheet hoặc trong logs/sheet_state.json) -> bỏ qua trừ khi retry_errors;
    'Đang đăng' (lần trước bị ngắt) -> làm lại.
    mode="schedule": when = giờ trên Sheet (lên lịch trên Facebook); giờ đã qua mà chưa đăng -> it['past_bad'] (ghi 'Lỗi' + ghi chú
                     vào Sheet qua write_page_errors, không tự dời sang ngày khác).
    mode="due":      giờ chưa tới -> để lại (it['waiting']=True, lý do 'chưa tới giờ');
                     giờ vừa qua trong window_min phút -> đăng bù ngay (when=None, it['due'] = giờ ghi trên Sheet);
                     quá window_min phút mà chưa đăng -> it['past_bad'] (ghi 'Lỗi').
    Hàng chỉ ghi Ngày mà ngày đã qua (assign_slots đặt it['past_due']) -> it['past_bad'] tương tự.
    horizon_days (mode schedule): chỉ lên lịch bài có ngày <= hôm nay + N ngày (1 = tới hết ngày mai);
                     bài xa hơn để lại (waiting, it['wake_at'] = 0h ngày nó tới lượt). None = không giới hạn."""
    now = now or datetime.now()
    limit_date = now.date() + timedelta(days=horizon_days) if horizon_days is not None and horizon_days >= 0 else None
    todo, skipped = [], []
    for it in items:
        reason = None
        when = None
        it["waiting"], it["due"], it["wake_at"], it["wait_label"] = False, "", None, ""
        it["page_bad"], it["slot_bad"], it["past_bad"] = False, False, False
        st = norm(it["status"])
        prev = state.get(row_key(sheet_link, it), {})
        # Sổ ghi nhớ chỉ chặn đăng trùng khi kết quả CHƯA ghi được vào Sheet (sheet_written). Một khi Sheet đã có
        # Trạng thái thì Sheet là nguồn sự thật: người dùng xoá Trạng thái (hoặc ghi 'Chờ đăng') = muốn đăng lại.
        remembered = prev.get("status") in ("done", "error") and not prev.get("sheet_written")
        # đã đăng/lỗi lần trước nhưng Sheet chưa ghi (trống / 'Chờ đăng') -> nút 'Xếp lịch' sẽ điền lại (write_slots)
        it["stale"] = remembered and st in ("", "cho dang")
        if only_rows and it["row"] not in only_rows:
            reason = "không nằm trong --rows"
        elif st.startswith("da ") or st in ("bo qua", "skip", "done", "xong"):
            reason = f"trạng thái '{it['status']}'"
        elif st.startswith("loi") and not retry_errors:
            reason = f"trạng thái '{it['status']}' (bấm 'Đăng lại hàng lỗi' / --retry-errors để đăng lại)"
        elif remembered and prev["status"] == "done":
            reason = "đã đăng lần chạy trước nhưng Sheet chưa được ghi (logs/sheet_state.json) – bấm 'Xếp lịch vào Sheet' để điền lại"
        elif remembered and not retry_errors:
            reason = f"lỗi lần trước: {(prev.get('note') or '')[:60]} (sửa hàng, hoặc 'Đăng lại hàng lỗi')"
        elif it.get("page_error"):                    # page không gắn với profile nào -> không đăng, ghi 'Lỗi' vào Sheet
            reason, it["page_bad"] = it["page_error"], True
        elif not it["video"]:
            reason = "thiếu video"
        elif it.get("past_due"):                      # chỉ ghi Ngày mà ngày đã qua -> ghi 'Lỗi' + ghi chú vào Sheet
            reason = f"{it['past_due']} (lúc kiểm tra {now:%H:%M %d/%m}) mà bài chưa đăng – {PAST_FIX}"
            it["past_bad"] = True
        elif not it["time"]:
            reason = it.get("auto_fail") or "thiếu ngày/giờ đăng"
            it["slot_bad"] = bool(it.get("auto_fail_err"))     # hết khung giờ trống -> ghi 'Lỗi' + ghi chú vào Sheet
        elif norm(it["time"]) in ("ngay", "now", "dang ngay"):
            when = None
        else:
            try:
                when = parse_schedule(it["time"])
            except ValueError as e:
                reason = str(e)
            else:
                if mode == "due":
                    it["due"] = when.strftime("%d/%m/%Y %H:%M")
                    if when > now:
                        it["waiting"], it["wake_at"], it["wait_label"] = True, when, "chưa tới giờ"
                        reason = f"chưa tới giờ ({when:%H:%M %d/%m})"
                    elif when < now - timedelta(minutes=window_min):   # quá giờ lâu (máy tắt / GPM không chạy) -> 'Lỗi'
                        late = int((now - when).total_seconds() // 60)
                        reason = (f"giờ đăng {it['time']} trên Sheet đã qua {late} phút lúc kiểm tra ({now:%H:%M %d/%m}) mà bài "
                                  f"chưa đăng (chỉ đăng bù trong {window_min} phút) – {PAST_FIX}")
                        it["past_bad"] = True
                    else:
                        when = None                       # vừa tới giờ -> bấm "Chia sẻ ngay"
                elif when <= now:                         # giờ đã qua mà chưa đăng -> 'Lỗi' + ghi chú vào Sheet, không tự dời
                    reason = f"giờ đăng {it['time']} trên Sheet đã qua (lúc kiểm tra {now:%H:%M %d/%m}) mà bài chưa đăng – {PAST_FIX}"
                    it["past_bad"] = True
                elif limit_date and when.date() > limit_date:     # ngoài giới hạn ngày -> để lại, tới ngày mới lên lịch
                    it["waiting"], it["wait_label"] = True, "chưa tới lượt"
                    it["wake_at"] = datetime.combine(when.date() - timedelta(days=horizon_days), datetime.min.time())
                    reason = (f"chưa tới lượt: hôm nay chỉ lên lịch tới hết {limit_date:%d/%m}, bài này {when:%H:%M %d/%m} "
                              f"(sẽ lên lịch từ 0h {it['wake_at']:%d/%m})")
        if reason:
            skipped.append((it, reason))
        else:
            todo.append((it, when))
    # "ngay" trước, rồi theo giờ (mode due: theo giờ ghi trên Sheet, bài tới giờ sớm nhất đăng trước)
    todo.sort(key=lambda x: (x[1] is not None, x[1] or (parse_schedule(x[0]["due"]) if x[0]["due"] else datetime.min)))
    return todo, skipped


def write_page_errors(webhook, link, tab, skipped, gid=None):
    """Hàng bị bỏ qua vì (a) page không gắn với profile nào (quy ước mỗi profile một fanpage), (b) hàng trống không còn
    khung giờ trống đúng thứ tự (kẹp giữa hàng trên và hàng dưới cùng page), hoặc (c) giờ/ngày đăng ghi trên Sheet đã qua
    lúc kiểm tra mà bài chưa đăng -> ghi Trạng thái 'Lỗi' + lý do vào Sheet.
    plan() chỉ gắn các lý do này cho hàng đang trống / 'Chờ đăng' / 'Đang đăng', nên hàng đã ghi 'Lỗi' không bị ghi lại.
    Trả về [(it, lý do)]; it['page_bad'] / it['slot_bad'] / it['past_bad'] cho biết loại lỗi."""
    bad = [(it, reason) for it, reason in skipped if it.get("page_bad") or it.get("slot_bad") or it.get("past_bad")]
    if bad and webhook:
        if gid is None:
            gid = sheet_csv_url(link, tab)[1] if not link.lower().endswith(".csv") else None
            if gid is None:
                gid = sheet_gid(link)
        for it, reason in bad:
            if sheet_update(webhook, it["row"], gid, status=ST_ERROR, note=reason[:300]):
                log(f"  Sheet hàng {it['row']}: Trạng thái = {ST_ERROR} "
                    f"({'page chưa gắn profile' if it.get('page_bad') else 'quá giờ chưa đăng' if it.get('past_bad') else 'không còn khung giờ trống đúng thứ tự'})")
    return bad


# ----------------------------------------------------------------- tự xếp giờ theo khung giờ của page
def slot_candidates(hours, start, only_date=None, days=60):
    """Các mốc (datetime) theo khung giờ `hours` ['07:00', ...] từ `start` trở đi; chỉ trong ngày `only_date` nếu có."""
    if not hours:
        return
    day0 = only_date or start.date()
    for n in range(1 if only_date else days):
        day = day0 + timedelta(days=n)
        for h in hours:
            dt = datetime.combine(day, datetime.strptime(h, "%H:%M").time())
            if dt >= start:
                yield dt


def assign_slots(items, sheet_link, state, mode="schedule", now=None, lead_min=20, window_min=20, retry_errors=False):
    """Hàng chưa đủ ngày+giờ -> tự xếp vào khung giờ mặc định của page (it['hours']), tính từ hiện tại, tránh trùng
    mốc đã có (hàng khác trên Sheet + bài đã đăng/lên lịch trong logs/sheet_state.json, kể cả hàng đã xoá khỏi Sheet).
    Ghi vào it['time'] (dd/mm/yyyy HH:MM), it['date'], it['hour'], it['auto']=True; không xếp được -> it['auto_fail'].
    Hàng có đủ Ngày+Giờ thì giữ nguyên (KHÔNG tự dời): giờ đã qua mà chưa đăng -> plan() ghi 'Lỗi' + ghi chú vào Sheet.
    Chỉ ghi Ngày mà ngày đã qua -> it['past_due'] (plan() ghi 'Lỗi' tương tự).
      schedule: mốc phải cách hiện tại >= lead_min phút (Facebook cần thời gian lên lịch ở tương lai).
      due:      mốc tính từ (hiện tại - window_min) để bài có khung giờ vừa qua được đăng ngay ở lần kiểm tra này.
    Hàm thuần theo (Sheet, state, now): không cần lưu gì, chạy lại cho cùng kết quả.
    Trả về dict occupied {khoá page: set(datetime)} để tính khung trống sắp tới."""
    now = now or datetime.now()
    occupied = {}

    def keys_of(asset, name):
        return [k for k in (str(asset or "").strip(), norm(name)) if k]

    def add_occ(asset, name, dt):
        for k in keys_of(asset, name):
            occupied.setdefault(k, set()).add(dt)

    def is_occ(asset, name, dt):
        return any(dt in occupied.get(k, ()) for k in keys_of(asset, name))

    def skip_row(it):
        st = norm(it["status"])
        return st in ("bo qua", "skip", "done", "xong") or (st.startswith("loi") and not retry_errors)

    for it in items:
        it.setdefault("auto", False)
        it["auto_fail"] = ""
        if it["time"] and it["time"] != "ngay" and not skip_row(it):
            try:
                add_occ(it.get("asset"), it.get("page"), parse_schedule(it["time"]))
            except ValueError:
                pass
    for st in state.values():                      # bài đã đăng / lên lịch (mốc trong 1 ngày qua trở đi)
        slot = st.get("slot") or (st.get("when") if st.get("when") not in (None, "now") else None)
        if st.get("status") == "done" and slot:
            try:
                dt = datetime.fromisoformat(slot)
            except ValueError:
                continue
            if dt > now - timedelta(days=1):
                add_occ(st.get("asset"), st.get("page"), dt)

    def same_page(a, b):
        ka, kb = keys_of(a.get("asset"), a.get("page")), keys_of(b.get("asset"), b.get("page"))
        if ka or kb:
            return bool(set(ka) & set(kb))
        return norm(a.get("profile")) == norm(b.get("profile"))      # không có tab Pages: page mặc định của profile

    def fixed_time(x):
        """Mốc đã chốt của hàng x (Ngày+Giờ trên Sheet, hoặc vừa tự xếp ở vòng này); None = chưa có / 'ngay' / hàng bỏ qua."""
        if not x.get("time") or x["time"] == "ngay" or skip_row(x) or x.get("auto_fail"):
            return None
        try:
            return parse_schedule(x["time"])
        except ValueError:
            return None

    def bounds_of(idx, it):
        """THỨ TỰ HÀNG TRÊN SHEET = THỨ TỰ ĐĂNG (trong cùng page): hàng trống phải SAU hàng gần nhất phía trên đã có giờ
        (ghi tay, đã lên lịch, hoặc vừa tự xếp) và TRƯỚC hàng gần nhất phía dưới đã ghi Ngày+Giờ (hoặc đã lên lịch);
        hàng dưới chỉ ghi Ngày -> phải trước 0h ngày đó. Trả về (lo, mô tả lo, hi, mô tả hi); None = không ràng buộc."""
        lo = lo_txt = hi = hi_txt = None
        for p in reversed(items[:idx]):
            if same_page(p, it):
                t = fixed_time(p)
                if t is not None:
                    lo, lo_txt = t, f"hàng {p['row']} ({t:%H:%M %d/%m})"
                    break
        for n in items[idx + 1:]:
            if not same_page(n, it) or skip_row(n):
                continue
            t = fixed_time(n)
            if t is not None:
                if t > now:                        # giờ đã qua mà chưa đăng -> hàng đó bị ghi Lỗi, không ràng buộc
                    hi, hi_txt = t, f"hàng {n['row']} ({t:%H:%M %d/%m})"
                    break
                continue
            if n.get("date") and not n.get("hour"):          # hàng dưới chỉ ghi Ngày
                try:
                    d = datetime.strptime(n["date"], "%d/%m/%Y").date()
                except ValueError:
                    continue
                if d > now.date():
                    hi, hi_txt = datetime.combine(d, datetime.min.time()), f"hàng {n['row']} (ngày {d:%d/%m})"
                    break
        return lo, lo_txt, hi, hi_txt

    start = now + timedelta(minutes=lead_min) if mode == "schedule" else now - timedelta(minutes=window_min)
    for idx, it in enumerate(items):
        it["auto_from"], it["auto_fail_err"], it["past_due"] = "", False, ""
        if skip_row(it) or norm(it["status"]).startswith("da ") or it["time"] == "ngay":
            continue
        prev = state.get(row_key(sheet_link, it), {})
        if not prev.get("sheet_written") and (prev.get("status") == "done" or (prev.get("status") == "error" and not retry_errors)):
            continue                                   # kết quả lần trước chưa vào Sheet -> vẫn coi là đã xong
        if it["time"]:                                 # có đủ Ngày+Giờ: giữ nguyên, không tự dời (giờ đã qua -> plan() ghi Lỗi)
            continue
        only_date = None
        if it.get("date"):
            try:
                only_date = datetime.strptime(it["date"], "%d/%m/%Y").date()
            except ValueError:
                it["auto_fail"] = f"không hiểu ngày '{it['date']}'"
                continue
            if only_date < now.date():                 # chỉ ghi Ngày mà ngày đã qua -> 'Lỗi' + ghi chú, không tự dời
                it["past_due"] = f"ngày {it['date']} ghi trên Sheet đã qua"
                continue
        hours = [it["hour"]] if it.get("hour") else list(it.get("hours") or [])
        if not hours:
            it["auto_fail"] = "page chưa có khung giờ mặc định (tab Giờ đăng trên web, hoặc cột Giờ đăng mặc định ở tab Pages)"
            continue
        # hàng trống: phải nằm đúng thứ tự giữa các hàng trên/dưới cùng page
        lo, lo_txt, hi, hi_txt = bounds_of(idx, it)
        s0 = start if lo is None else max(start, lo + timedelta(minutes=1))
        pick = next((dt for dt in slot_candidates(hours, s0, only_date)
                     if (hi is None or dt < hi) and not is_occ(it.get("asset"), it.get("page"), dt)), None)
        if pick is None:
            if lo is not None or hi is not None:
                where = (f"giữa {lo_txt} và {hi_txt}" if lo is not None and hi is not None
                         else f"trước {hi_txt}" if hi is not None else f"sau {lo_txt} trong 60 ngày")
                it["auto_fail"] = (f"không còn khung giờ trống của page {where}" + (f" trong ngày {it['date']}" if only_date else "")
                                   + " – thứ tự hàng trên Sheet là thứ tự đăng. Thêm khung giờ cho page (tab Giờ đăng), "
                                     "ghi Ngày/Giờ tay, hoặc chuyển hàng này xuống dưới; rồi xoá ô Trạng thái để đăng lại")
            else:
                it["auto_fail"] = (f"ngày {it['date']} đã qua hoặc không còn khung giờ trống" if only_date
                                   else "không còn khung giờ trống trong 60 ngày tới")
            it["auto_fail_err"] = True                 # hết khung giờ -> ghi 'Lỗi' + ghi chú vào Sheet (write_page_errors)
            continue
        it["time"], it["date"], it["hour"], it["auto"] = pick.strftime("%d/%m/%Y %H:%M"), pick.strftime("%d/%m/%Y"), pick.strftime("%H:%M"), True
        add_occ(it.get("asset"), it.get("page"), pick)
    return occupied


def write_slots(webhook, link, tab, items, only_rows=None):
    """Nút 'Xếp lịch':
      (1) hàng vừa tự xếp (it['auto']) -> ghi Ngày/Giờ vào Sheet để cố định lịch ngay, không đợi tới lúc đăng;
      (2) hàng đã đăng / lỗi ở lần chạy trước (logs/sheet_state.json) mà Sheet còn trống Trạng thái (lúc đó chưa có
          cách ghi) -> điền lại Trạng thái, Page, Ghi chú và Ngày/Giờ đã đăng (nếu ô đang trống).
    Trả về (written [(row, 'dd/mm/yyyy HH:MM')], synced [(row, trạng thái, 'dd/mm/yyyy HH:MM')], failed [(row, lý do)], no_webhook)."""
    if not webhook:
        return [], [], [], True
    gid = sheet_csv_url(link, tab)[1] if not link.lower().endswith(".csv") else None
    if gid is None:
        gid = sheet_gid(link)
    state = load_state()
    written, synced, failed = [], [], []
    for it in items:
        if only_rows and it["row"] not in only_rows:
            continue
        if it.get("auto"):
            if sheet_update(webhook, it["row"], gid, link=link, date=it["date"], time=it["hour"], status=ST_WAITING if not it["status"] else None):
                written.append((it["row"], it["time"]))
            else:
                failed.append((it["row"], "webhook không ghi được"))
            continue
        prev = state.get(row_key(link, it), {})
        if not it.get("stale") or prev.get("status") not in ("done", "error"):
            continue
        scheduled = prev.get("when") not in (None, "now")
        fields = {"status": (ST_SCHEDULED if scheduled else ST_POSTED) if prev["status"] == "done" else ST_ERROR,
                  "page": prev.get("page") or None, "note": prev.get("note") or None}
        slot = prev.get("slot") or (prev.get("when") if scheduled else None)
        if slot and not it["date"] and not it["hour"]:
            try:
                dt = datetime.fromisoformat(slot)
                fields["date"], fields["time"] = dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M")
            except ValueError:
                pass
        if sheet_update(webhook, it["row"], gid, link=link, **fields):
            synced.append((it["row"], fields["status"], f"{fields.get('date', '')} {fields.get('time', '')}".strip()))
            prev["sheet_written"] = True              # từ nay xoá Trạng thái trên Sheet = cho đăng lại
        else:
            failed.append((it["row"], "webhook không ghi được"))
    if synced:
        save_state(state)
    return written, synced, failed, False


def with_tags(caption, tags):
    """Nối cột Hashtag vào sau tiêu đề (bỏ qua nếu tiêu đề đã chứa sẵn)."""
    caption, tags = (caption or "").strip(), (tags or "").strip()
    if tags and tags not in caption:
        caption = (caption + " " + tags).strip()
    return caption


def caption_of(it):
    return with_tags(it["title"], it["hashtag"]) or None


def resolve_video(it):
    """Trả về (đường dẫn file trên máy, caption). Link Drive -> tải về; đường dẫn máy -> dùng luôn.
    Thư mục Drive mà Tiêu đề để trống -> lấy dòng 'Tiêu đề:' trong .docx, rồi nối thêm cột Hashtag."""
    src, title, tags = it["video"].strip(), it["title"].strip(), it["hashtag"].strip()
    if "drive.google.com" in src:
        path, cap = fetch_from_drive(src, title or None)
        return path, with_tags(cap, tags)
    p = Path(src)
    if not p.is_file():
        raise RuntimeError(f"Không thấy file: {src}")
    if p.suffix.lower() not in VIDEO_EXT:
        raise RuntimeError(f"Không phải file video: {src}")
    return str(p), with_tags(title or p.stem, tags)


# ----------------------------------------------------------------- lập kế hoạch dùng chung
def prepare(link, tab=TAB_QUEUE, pages_tab=TAB_PAGES, default_profile=PROFILE_ID, only_rows=None,
            retry_errors=False, need_gpm=False, mode="schedule", page_hours=None, lead_min=20, window_min=20, now=None,
            horizon_days=1):
    """Đọc Sheet + tab Pages, gắn asset/profile/khung giờ cho từng hàng, tự xếp giờ cho hàng trống, chia sẽ-đăng / bỏ-qua.
    Dùng chung cho dòng lệnh, giao diện web và bộ tự động kiểm tra.
    page_hours: {asset_id hoặc tên page: ['07:00', ...]} sửa trên web (ghi đè cột Giờ đăng mặc định của tab Pages).
    Trả về dict: items, pages (kèm hours / hours_src / next_free), todo, skipped, profile_names, gpm_ok, messages, mode,
    horizon_days: mode schedule chỉ lên lịch tới hôm nay + N ngày (mặc định 1 = tới hết ngày mai; None = không giới hạn).
    next_due: mốc sớm nhất cần thức dậy (bài chưa tới giờ ở mode due / 0h ngày bài tới lượt ở mode schedule), None nếu không có."""
    if mode not in POST_MODES:
        mode = "schedule"
    now = now or datetime.now()
    page_hours = page_hours or {}
    _WRITE_LINK["link"] = link

    def hours_of(asset, name):
        web = page_hours.get(str(asset or "").strip()) or next((v for k, v in page_hours.items() if norm(k) == norm(name)), None)
        return (parse_hours(web), "web") if web else (None, "")
    msgs = []
    items = read_sheet(link, tab)
    pages = read_pages(link, pages_tab)
    if pages:
        msgs.append(f"Tab {pages_tab}: {len(pages)} page – " + ", ".join(f"{p['name']} ({p['profile'] or 'profile mặc định'})" for p in pages))
    else:
        msgs.append(f"(không có tab {pages_tab}: page sẽ được tra qua bộ chọn trang trong Business Suite)")
    try:
        profile_names = dict(gpm_profiles())
        gpm_ok = True
    except Exception as e:
        profile_names, gpm_ok = {}, False
        if need_gpm:
            raise RuntimeError("Không kết nối được GPM Login (cổng 9495) – hãy mở GPM Login rồi chạy lại") from e
        msgs.append("(GPM Login chưa chạy – chỉ lập kế hoạch)")

    def default_page_of(profile_want):
        for p in pages:
            if p["profile"] and (norm(p["profile"]) in norm(profile_want) or norm(profile_want) in norm(p["profile"])):
                return p
        return None

    def profile_key(want):
        """Nhận ra 2 cách ghi cùng trỏ 1 profile GPM: id khi GPM đang chạy, không thì tên đã chuẩn hoá."""
        want = (want or "").strip()
        if gpm_ok and want:
            if want in profile_names:
                return want
            hits = [pid for pid, nm in profile_names.items() if pid.startswith(want.lower()) or norm(want) in norm(nm)]
            if len(hits) == 1:
                return hits[0]
        return norm(want)
    # Quy ước MỖI PROFILE MỘT FANPAGE: fanpage của profile = hàng đầu tiên trong tab Pages ghi profile đó. Page ghi trùng
    # profile với page khác / không ghi profile / không có trong tab Pages -> hàng đó báo lỗi, KHÔNG mượn profile khác đăng.
    owner = {}
    for p in pages:
        p["page_error"] = ""
        if not p["profile"]:
            p["page_error"] = f"page '{p['name']}' chưa ghi Profile GPM ở tab {pages_tab} – chưa có profile nào gắn page này"
            continue
        k = profile_key(p["profile"])
        if k in owner:
            p["page_error"] = (f"page '{p['name']}' ghi profile '{p['profile']}' nhưng profile này đã gắn page "
                               f"'{owner[k]['name']}' (mỗi profile một fanpage) – tạo profile GPM riêng cho page '{p['name']}'")
            msgs.append("CHÚ Ý: " + p["page_error"])
        else:
            owner[k] = p
    for p in pages:                                  # khung giờ: web ghi đè > cột Giờ đăng mặc định trên Sheet
        p["sheet_hours"] = parse_hours(p.get("hour"))
        web, src = hours_of(p["asset"], p["name"])
        p["hours"], p["hours_src"] = (web, "web") if web else (p["sheet_hours"], "sheet" if p["sheet_hours"] else "")
    for it in items:
        it["page_error"] = ""
        p = lookup_page(pages, it["page"]) if it["page"] else None
        if p is None and not it["page"] and pages:
            want = it["profile"] or profile_names.get(default_profile, default_profile)
            p = default_page_of(want)
            if p is None:
                it["page_error"] = f"hàng không ghi Page và profile '{want}' chưa gắn fanpage nào ở tab {pages_tab}"
        if pages and it["page"] and p is None:
            it["page_error"] = (f"page '{it['page']}' không có trong tab {pages_tab} (hoặc tên khớp nhiều page) – chưa có "
                                "profile nào gắn page này; thêm hàng vào tab Pages với profile GPM riêng, không mượn profile khác")
        elif p is not None and p.get("page_error"):
            it["page_error"] = p["page_error"]
        elif p is not None and it["page"] and it["profile"] and profile_key(it["profile"]) != profile_key(p["profile"]):
            it["page_error"] = (f"hàng ghi profile '{it['profile']}' nhưng page '{p['name']}' gắn với profile '{p['profile']}' "
                                f"(tab {pages_tab}) – không đổi page của profile khác")
        it["asset"] = p["asset"] if p else ""
        if p and not it["profile"]:
            it["profile"] = p["profile"]
        if p:
            it["page"], it["hours"] = p["name"], p["hours"]
        else:                                        # không có tab Pages: khung giờ theo tên page sửa trên web
            it["hours"] = hours_of("", it["page"])[0] or []
    state = load_state()
    occupied = assign_slots(items, link, state, mode, now, lead_min, window_min, retry_errors)
    todo, skipped = plan(items, link, state, only_rows, retry_errors, mode, now, horizon_days, window_min)
    start = now + timedelta(minutes=lead_min) if mode == "schedule" else now
    for p in pages:                                  # 3 khung giờ trống sắp tới của mỗi page (xem ở tab Giờ đăng)
        free = []
        for dt in slot_candidates(p["hours"], start):
            if not any(dt in occupied.get(k, ()) for k in (p["asset"], norm(p["name"])) if k):
                free.append(dt.strftime("%H:%M %d/%m"))
                if len(free) >= 3:
                    break
        p["next_free"] = free
    waiting = [it["wake_at"] for it, _ in skipped if it.get("waiting") and it.get("wake_at")]
    return {"items": items, "pages": pages, "todo": todo, "skipped": skipped,
            "profile_names": profile_names, "gpm_ok": gpm_ok, "messages": msgs,
            "mode": mode, "horizon_days": horizon_days, "next_due": min(waiting) if waiting else None}


# ----------------------------------------------------------------- quét profile / fanpage
def cmd_check_gpm():
    """Chẩn đoán GPM (chỉ đọc): API, nhân trình duyệt + proxy từng profile, bản cập nhật bị bỏ qua, thư mục cài, log GPM."""
    d = gpm_check()
    log(f"GPM Local API: {'OK – ' + d['version'] if d['api_ok'] else 'KHÔNG kết nối được'}")
    if d["cores"].get("chrome"):
        print(f"   Nhân Chrome bản GPM này hỗ trợ: {', '.join(d['cores']['chrome'])}")
    if d["skipped_update"]:
        print(f"   Bản GPM mới đã bị bấm bỏ qua: {d['skipped_update']}")
    for p in d["profiles"]:
        core = "—" if p["core_ok"] is None else ("hỗ trợ" if p["core_ok"] else "KHÔNG HỖ TRỢ")
        st = "chưa kiểm" if p["proxy_ok"] is None else ("OK" if p["proxy_ok"] else f"LỖI ({p['proxy_err']})")
        print(f"   {p['name']:28s} {p['browser']:22s} nhân: {core:13s} proxy {p['proxy'] or '(không có)':24s} {st}"
              + (f" {p['proxy_ms']} ms" if p["proxy_ms"] is not None else ""))
    if d["install"].get("exe"):
        print(f"   Thư mục cài: {Path(d['install']['exe']).parent}  |  file font vân tay: "
              f"{'có' if d['install']['fonts_map_ok'] else 'thiếu (không chặn mở profile)'}")
    for ln in d["log_errors"]:
        print("   log GPM:", ln)
    for a in d["advice"]:
        log("=> " + a)


def cmd_list_profiles():
    profiles = gpm_profiles()
    log(f"GPM Login có {len(profiles)} profile:")
    for pid, name in profiles:
        print(f"   {name:30s}  {pid}")


def cmd_scan_pages(which, webhook, close_after=False, link=None):
    """Mở từng profile GPM, đọc danh sách fanpage (kèm asset_id), xuất logs/pages_scan.csv theo đúng cột
    tab Pages; có webhook thì thêm/cập nhật thẳng vào tab Pages."""
    profiles = gpm_profiles()
    if which.lower() != "all":
        wanted = [resolve_profile_id(w.strip()) for w in which.split(",") if w.strip()]
        profiles = [(pid, name) for pid, name in profiles if pid in wanted]
    rows = []
    with sync_playwright() as p:
        for pid, pname in profiles:
            log(f"##### Quét fanpage của profile '{pname}' #####")
            try:
                port = gpm_start(pid)
                time.sleep(5)
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                place_window(browser)
                page = pick_page(browser.contexts[0])
                page.on("dialog", lambda d: d.accept())
                found = scan_pages(page, pid)
            except Exception as e:
                log(f"  LỖI profile '{pname}': {str(e)[:150]}")
                continue
            finally:
                if close_after:
                    gpm_stop(pid)
            for f in found:
                log(f"  {f['platform']:9s} {f['name']}  asset_id={f['asset'] or '?'}")
                if f["platform"] == "Facebook" and f["asset"]:
                    rows.append([f["name"], f["asset"], pname, "", "", f"quét {datetime.now():%d/%m/%Y}"])
    out = LOG_DIR / "pages_scan.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Tên page", "asset_id", "Profile GPM", "Người phụ trách", "Giờ đăng mặc định", "Ghi chú"])
        w.writerows(rows)
    log(f"Đã ghi {len(rows)} fanpage vào {out} (dán vào tab Pages)")
    if webhook and rows:
        _, msg = sheet_upsert_pages(webhook, link, [{"name": a, "asset": b, "profile": c} for a, b, c, *_ in rows])
        log(f"Tab Pages trên Sheet: {msg}")


def parse_pause(text):
    """'20-60' -> (20, 60); '30' -> (30, 30). Giây, không âm, tối đa 3600, tự đảo nếu ghi ngược."""
    parts = [int(float(x)) for x in str(text).replace(" ", "").split("-") if x != ""]
    if not parts or len(parts) > 2:
        raise ValueError(text)
    lo, hi = (parts[0], parts[-1])
    lo, hi = (max(0, min(3600, lo)), max(0, min(3600, hi)))
    return (lo, hi) if lo <= hi else (hi, lo)


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Đăng/lên lịch Reels hàng loạt theo Google Sheet công khai")
    ap.add_argument("--list-profiles", action="store_true", help="In danh sách profile GPM rồi thoát")
    ap.add_argument("--archive", action="store_true",
                    help=f"Dọn bài Đã đăng / Đã lên lịch của các ngày trước hôm qua sang tab '{TAB_DONE}' (nguyên cả ngày) rồi thoát; "
                         "kết hợp --dry-run để chỉ đếm")
    ap.add_argument("--keep-days", type=int, default=1, help="--archive: giữ lại bài của hôm nay và N ngày trước đó (mặc định 1 = hôm qua)")
    ap.add_argument("--check-gpm", action="store_true", help="Chẩn đoán GPM: API, proxy từng profile, thư mục cài, log lỗi (chỉ đọc)")
    ap.add_argument("--scan-pages", metavar="PROFILE", nargs="?", const="all",
                    help="Quét fanpage (kèm asset_id) của profile GPM ('all' hoặc tên/id, cách nhau dấu phẩy) -> logs/pages_scan.csv; "
                         "có --webhook thì ghi thẳng vào tab Pages")
    ap.add_argument("--sheet", default=SHEET_URL, help="Link Google Sheet (chia sẻ 'Bất kỳ ai có đường liên kết')")
    ap.add_argument("--tab", default=TAB_QUEUE, help=f"Tên tab hàng đợi (mặc định '{TAB_QUEUE}')")
    ap.add_argument("--pages-tab", default=TAB_PAGES, help=f"Tên tab danh mục page (mặc định '{TAB_PAGES}')")
    ap.add_argument("--max", type=int, default=0, help="Tối đa bao nhiêu bài trong lần chạy này (0 = tất cả)")
    ap.add_argument("--rows", help="Chỉ đăng các hàng này, ví dụ 2,3,7 (số hàng trên Sheet)")
    ap.add_argument("--dry-run", action="store_true", help="Làm hết các bước nhưng không bấm nút cuối")
    ap.add_argument("--plan-only", action="store_true", help="Chỉ in kế hoạch, không mở trình duyệt")
    ap.add_argument("--webhook", default=SHEET_WEBHOOK, help="URL Apps Script để ghi Trạng thái/Page ngược vào Sheet")
    ap.add_argument("--retry-errors", action="store_true", help="Đăng lại cả các hàng đang ghi 'Lỗi'")
    ap.add_argument("--mode", choices=POST_MODES, default="schedule",
                    help="schedule = lên lịch trên Facebook ngay khi chạy (mặc định); "
                         "due = tới đúng ngày/giờ ghi trong Sheet mới đăng ('Chia sẻ ngay'), hàng chưa tới giờ để lại")
    ap.add_argument("--pause", default=None,
                    help="Nghỉ ngẫu nhiên giữa 2 bài, giây: 20-60 hoặc một số; 0 = không nghỉ "
                         "(mặc định: pause_min/pause_max trong --config, không có thì 20-60)")
    ap.add_argument("--config", help="config.json của giao diện web: lấy khung giờ page sửa trên web (page_hours) + slot_lead_min")
    ap.add_argument("--lead", type=int, default=None, help="Tự xếp giờ: khung giờ phải cách hiện tại ít nhất N phút (mặc định 20)")
    ap.add_argument("--window", type=int, default=20,
                    help="Mode due: giờ đăng vừa qua trong vòng N phút vẫn được đăng bù; quá N phút mà chưa đăng -> ghi 'Lỗi' (mặc định 20)")
    ap.add_argument("--horizon", type=int, default=1,
                    help="Mode schedule: chỉ lên lịch bài tới hôm nay + N ngày (mặc định 1 = tới hết ngày mai; 0 = chỉ hôm nay; -1 = không giới hạn)")
    ap.add_argument("--open-max", type=int, default=None,
                    help="Mở tối đa N profile Chrome cùng lúc; bài đăng xen kẽ giữa các profile đang mở (mỗi profile một fanpage), "
                         "profile hết bài thì đóng rồi mở profile kế tiếp; các cửa sổ Chrome xếp cạnh nhau trên màn hình, không đè "
                         "lên nhau (mặc định 3, hoặc max_open_profiles trong --config)")
    ap.add_argument("--profile", default=PROFILE_ID, help="Profile GPM mặc định (id hoặc một phần tên) cho hàng không ghi cột Profile")
    ap.add_argument("--asset-id", default=PAGE_ASSET_ID)
    ap.add_argument("--close-after", action="store_true", help="Đóng profile GPM khi xong")
    a = ap.parse_args()
    if a.check_gpm:
        return cmd_check_gpm()
    if a.list_profiles:
        return cmd_list_profiles()
    if a.archive:
        if not a.sheet:
            sys.exit("Thiếu --sheet")
        return cmd_archive(a.webhook, a.sheet, a.tab, a.keep_days, a.dry_run)
    if a.scan_pages:
        return cmd_scan_pages(a.scan_pages, a.webhook, a.close_after, a.sheet)
    if not a.sheet:
        sys.exit("Thiếu --sheet (hoặc điền SHEET_URL trong post_sheet.py)")
    only_rows = {int(x) for x in a.rows.split(",")} if a.rows else None

    page_hours, lead, open_max, pause = {}, 20, 3, "20-60"
    if a.config:
        try:
            cfgj = json.loads(Path(a.config).read_text(encoding="utf-8"))
            page_hours = cfgj.get("page_hours") or {}
            lead = int(cfgj.get("slot_lead_min", 20))
            open_max = int(cfgj.get("max_open_profiles", 3))
            pause = f"{cfgj.get('pause_min', 20)}-{cfgj.get('pause_max', 60)}"
        except Exception as e:
            log(f"(không đọc được {a.config}: {str(e)[:80]})")
    if a.lead is not None:
        lead = a.lead
    if a.open_max is not None:
        open_max = a.open_max
    if a.pause is not None:
        pause = a.pause
    try:
        lo, hi = parse_pause(pause)
    except ValueError:
        sys.exit(f"--pause '{pause}' không hợp lệ: ghi dạng 20-60 hoặc một số giây")
    try:
        prep = prepare(a.sheet, a.tab, a.pages_tab, a.profile, only_rows, a.retry_errors, need_gpm=not a.plan_only,
                       mode=a.mode, page_hours=page_hours, lead_min=lead, window_min=a.window,
                       horizon_days=a.horizon if a.horizon >= 0 else None)
    except RuntimeError as e:
        sys.exit(str(e))
    for m in prep["messages"]:
        log(m)
    items, pages, todo, skipped = prep["items"], prep["pages"], prep["todo"], prep["skipped"]
    state = load_state()
    if a.max:
        todo = todo[:a.max]

    n_wait = sum(1 for it, _ in skipped if it.get("waiting"))
    limit_txt = (f" tới hết {(datetime.now() + timedelta(days=a.horizon)):%d/%m}" if a.mode == "schedule" and a.horizon >= 0 else "")
    log(f"Sheet có {len(items)} hàng: sẽ đăng {len(todo)}, bỏ qua {len(skipped)}"
        + (f" (trong đó {n_wait} {'chưa tới giờ' if a.mode == 'due' else 'chưa tới lượt'})" if n_wait else "")
        + f" · cách đăng: {'tới giờ mới đăng' if a.mode == 'due' else 'lên lịch trên Facebook' + limit_txt}"
        + ("" if a.webhook else " (không có webhook: trạng thái chỉ ghi vào logs/, không ghi vào Sheet)"))
    # Cột Profile -> id profile GPM (trống = --profile); gom hàng theo profile, mỗi profile mở trình duyệt 1 lần
    profile_names, gpm_ok = prep["profile_names"], prep["gpm_ok"]
    default_profile = resolve_profile_id(a.profile) if gpm_ok else a.profile
    groups, bad = {}, []
    for it, when in todo:
        try:
            if gpm_ok:
                pid = resolve_profile_id(it["profile"]) if it["profile"] else default_profile
            else:                                   # plan-only khi GPM chưa chạy: chỉ gom theo tên ghi trên Sheet
                pid = it["profile"] or default_profile
            groups.setdefault(pid, []).append((it, when))
        except Exception as e:
            bad.append((it, str(e)))
    for it, when in todo:
        pid = next((p for p, rows in groups.items() if any(r[0] is it for r in rows)), None)
        tm = (when.strftime('%d/%m/%Y %H:%M') if when
              else f"TỚI GIỜ {it['due']} -> đăng ngay" if it.get("due") else "ĐĂNG NGAY")
        tm += " (tự xếp)" if it.get("auto") else ""
        log(f"  [hàng {it['row']}] {tm} | "
            f"profile: {profile_names.get(pid, it['profile'] or '?')} | trang: {it['page'] or '(mặc định)'} | "
            f"{(caption_of(it) or '(mô tả lấy từ kịch bản trong thư mục Drive)')[:50]} | {it['video'][:40]}")
    for it, reason in skipped:
        log(f"  [hàng {it['row']}] "
            f"{'LỖI page: ' if it.get('page_bad') else 'LỖI quá giờ: ' if it.get('past_bad') else 'LỖI xếp giờ: ' if it.get('slot_bad') else 'bỏ qua: '}"
            f"{reason}")
    for it, reason in bad:
        log(f"  [hàng {it['row']}] LỖI profile: {reason}")
    page_bad = [(it, reason) for it, reason in skipped if it.get("page_bad") or it.get("slot_bad") or it.get("past_bad")]
    if a.plan_only or not (todo or page_bad):
        return

    gid = sheet_csv_url(a.sheet, a.tab)[1] if not a.sheet.lower().endswith(".csv") else None
    if gid is None:
        gid = sheet_gid(a.sheet)
    # Hàng có page không gắn với profile nào (mỗi profile một fanpage) -> ghi 'Lỗi' + lý do vào Sheet, không mượn profile khác
    write_page_errors(a.webhook, a.sheet, a.tab, skipped, gid)
    results = [(it, None, "error", reason, "") for it, reason in bad + page_bad]
    for it, reason in bad:
        sheet_update(a.webhook, it["row"], gid, status=ST_ERROR, note=reason[:300])
    n_total = sum(len(rows) for rows in groups.values())
    max_open = max(1, open_max)
    log(f"Thứ tự đăng: xen kẽ giữa các profile đang mở (mỗi profile một fanpage, mỗi lượt 1 bài); mở tối đa {max_open} profile "
        "Chrome cùng lúc, profile hết bài thì đóng rồi mở profile kế tiếp")
    n_win = max(1, min(max_open, len(groups)))   # số ô chia màn hình: các cửa sổ Chrome mở cùng lúc xếp cạnh nhau, không đè lên nhau
    pending = deque(groups.items())      # profile chờ mở, theo thứ tự xuất hiện trong kế hoạch
    sessions = deque()                   # profile đang mở, xoay vòng: mỗi lượt đăng 1 bài rồi nhường profile kế
    unopened = []                        # bài của profile không mở được -> để lại (Sheet không ghi, lần sau đăng tiếp)
    n, cur = 0, None
    with (sync_playwright() if groups else contextlib.nullcontext()) as p:    # không có bài -> không khởi động Playwright
        try:
            while pending or sessions:
                while pending and len(sessions) < max_open:       # mở thêm profile cho đủ chỗ
                    pid, rows = pending.popleft()
                    name = profile_names.get(pid, pid)
                    page_names = sorted({it["page"] or "(mặc định)" for it, _ in rows})
                    log(f"##### Mở profile GPM: {name} ({len(rows)} bài, fanpage: {', '.join(page_names)}) #####")
                    if len(page_names) > 1:
                        log(f"  CHÚ Ý: profile '{name}' có bài cho {len(page_names)} page khác nhau – quy ước mỗi profile một fanpage "
                            "(kiểm tra cột Page / tab Pages); vẫn đăng theo thứ tự giờ, không xoay vòng page")
                    used = {s["win"] for s in sessions}                    # ô màn hình các profile đang mở chiếm
                    win = next((i for i in range(n_win) if i not in used), len(sessions) % n_win)
                    try:
                        port = gpm_start(pid, win=(win, n_win))
                        time.sleep(5)
                        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                        place_window(browser, win, n_win)
                        page = pick_page(browser.contexts[0])
                    except Exception as e:
                        log(f"LỖI mở profile '{name}': {str(e)[:200]} -> bỏ qua {len(rows)} bài của profile này "
                            "(không ghi vào Sheet, lần chạy sau đăng tiếp)")
                        unopened.extend(rows)
                        continue
                    sessions.append({"pid": pid, "name": name, "browser": browser, "page": page, "queue": deque(rows), "win": win})
                if not sessions:
                    break
                cur = s = sessions.popleft()
                it, when = s["queue"].popleft()                   # bài kế tiếp của profile này (theo thứ tự giờ)
                pid, page = s["pid"], s["page"]
                n += 1
                key = row_key(a.sheet, it)
                log(f"===== Bài {n}/{n_total} – hàng {it['row']} | profile {s['name']} | trang {it['page'] or '(mặc định)'} =====")
                info = {"expect_page": it["page"]}
                if not a.dry_run:                    # hàng tự xếp giờ: ghi luôn Ngày/Giờ đã xếp vào Sheet
                    extra = {"date": it["date"], "time": it["hour"]} if it.get("auto") else {}
                    sheet_update(a.webhook, it["row"], gid, status=ST_RUNNING, note="", **extra)
                try:
                    asset = it.get("asset") or resolve_asset_id(page, it["page"], pid) or a.asset_id
                    video, caption = resolve_video(it)
                    ok = post_reel(page, video, caption, when, a.dry_run, asset, info=info)
                    status = "done" if ok else "dry-run"
                    note = f"Xác nhận {datetime.now():%H:%M %d/%m}" if ok else f"Chạy thử {datetime.now():%H:%M %d/%m}"
                    if it.get("due"):                 # mode due: ghi kèm khung giờ (và độ trễ nếu đáng kể)
                        late = int((datetime.now() - parse_schedule(it["due"])).total_seconds() // 60)
                        note += (f" – tự xếp {it['due']}" if it.get("auto") else f" – lịch Sheet {it['due']}") \
                                + (f" (trễ {late} phút)" if late >= 15 else "")
                    elif it.get("auto"):
                        note += f" – tự xếp {it['time']}"
                except Exception as e:
                    shot(page, f"row{it['row']}_error")
                    status, note = "error", str(e)[:300]
                    log(f"LỖI hàng {it['row']}: {note}")
                    s["page"] = pick_page(s["browser"].contexts[0])      # tab có thể đã hỏng, chọn lại
                page_name = info.get("page") or ""
                results.append((it, when, status, note, page_name))
                if status in ("done", "error"):
                    slot = when or (parse_schedule(it["due"]) if it.get("due") else None)
                    state[key] = {"status": status, "note": note, "page": page_name, "asset": it.get("asset") or "",
                                  "profile": s["name"],
                                  "at": datetime.now().isoformat(timespec="seconds"),
                                  "when": when.isoformat() if when else "now",
                                  "slot": slot.isoformat() if slot else None, "auto": bool(it.get("auto"))}
                    save_state(state)
                st_text = {"done": ST_SCHEDULED if when else ST_POSTED, "error": ST_ERROR, "dry-run": ST_DRY}[status]
                posted_at = ({"date": f"{datetime.now():%d/%m/%Y}", "time": f"{datetime.now():%H:%M}"}
                             if status == "done" and when is None and not it.get("due") else {})   # đăng "ngay": ghi giờ thật
                if sheet_update(a.webhook, it["row"], gid, page=page_name or None, status=st_text, note=note, **posted_at):
                    log(f"  Sheet hàng {it['row']}: Trạng thái = {st_text}" + (f", Page = {page_name}" if page_name else ""))
                    if key in state:              # Sheet đã có kết quả -> từ nay Sheet là nguồn sự thật cho hàng này
                        state[key]["sheet_written"] = True
                        save_state(state)
                if s["queue"]:
                    sessions.append(s)                            # còn bài -> nhường lượt cho profile kế
                else:
                    log(f"##### Profile GPM {s['name']}: đã đăng hết bài #####")
                    if pending or a.close_after:
                        log(f"Đóng profile {s['name']}" + (" để mở profile kế tiếp" if pending else ""))
                        gpm_stop(s["pid"])
                cur = None
                if pending or sessions:
                    wait = random.randint(lo, hi)
                    if wait > 0:
                        log(f"Nghỉ {wait}s trước bài tiếp theo (chỉnh ở Cài đặt / --pause)...")
                        time.sleep(wait)
        finally:
            if a.close_after:
                for s in ([cur] if cur else []) + list(sessions):
                    gpm_stop(s["pid"])

    # Báo cáo: file CSV để dán cột Trạng thái/Ghi chú ngược vào Sheet nếu muốn
    rep = LOG_DIR / f"sheet_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(rep, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Hàng", "Profile", "Page đăng", "Thời gian", "Trạng thái", "Ghi chú"])
        for it, when, status, note, page_name in results:
            w.writerow([it["row"], it["profile"], page_name,
                        when.strftime("%d/%m/%Y %H:%M") if when else (it.get("due") or "ngay"),
                        {"done": ST_SCHEDULED if when else ST_POSTED, "dry-run": ST_DRY, "error": ST_ERROR}[status], note])
    done = sum(1 for r in results if r[2] == "done")
    err = sum(1 for r in results if r[2] == "error")
    dry = sum(1 for r in results if r[2] == "dry-run")
    log(f"XONG: {done} thành công, {err} lỗi" + (f" (trong đó {len(page_bad)} hàng ghi Lỗi vì page chưa gắn profile / quá giờ chưa đăng / không còn khung giờ trống đúng thứ tự)" if page_bad else "")
        + (f", {dry} chạy thử (dry-run)" if dry else "")
        + (f", {len(unopened)} bài chưa đăng vì không mở được profile (vẫn chờ trên Sheet)" if unopened else "")
        + f". Báo cáo: {rep}")


if __name__ == "__main__":
    main()
