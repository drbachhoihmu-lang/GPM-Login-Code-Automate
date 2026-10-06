"""
Đăng / lên lịch Thước phim (Reels) lên Fanpage qua Meta Business Suite,
chạy trên đúng profile GPM Login (Playwright kết nối qua GPM Local API).

Ví dụ:
  python post_reel.py --video "D:\\videos\\clip1.mp4" --caption "abc #thacsianhduc" --schedule "07/10/2026 12:00"
  python post_reel.py --video clip.mp4 --caption "..." --now                       # đăng ngay
  python post_reel.py --video clip.mp4 --caption "..." --schedule "2026-10-07 12:00" --dry-run
  python post_reel.py --caption "..." --schedule "07/10/2026 12:00" --resume      # trình soạn đã có video + mô tả
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Console Windows mặc định cp1252 -> in tiếng Việt sẽ lỗi
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ----------------------------------------------------------------- cấu hình
GPM_API = "http://127.0.0.1:9495/api/v1"
PROFILE_ID = "3fcc3942-0973-4809-a4a1-b7b1c89d4468"
PAGE_ASSET_ID = "1105165602680340"
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

COMPOSER_URL = ("https://business.facebook.com/latest/reels_composer/"
                "?ref=biz_web_home_create_reel&asset_id={asset}&context_ref=HOME")
SCHEDULED_URL = "https://business.facebook.com/latest/posts/scheduled_posts?asset_id={asset}"
PUBLISHED_URL = "https://business.facebook.com/latest/posts/published_posts?asset_id={asset}"
DESC_BOX_NAME = "Hãy viết vào ô hộp thoại để thêm văn bản vào bài viết."
STEPS = ("Tạo", "Chỉnh sửa", "Chia sẻ")

# Bản xem trước là <canvas> vẽ từ một <video> không nằm trong DOM, nút tạm dừng chỉ là hình vẽ.
# Chèn đoạn này trước khi mở trang để giữ tham chiếu tới mọi <video> và tạm dừng được chúng.
PAUSE_PREVIEW_JS = """
(() => {
  const vids = new Set();
  const hook = (el) => {
    vids.add(el);
    el.addEventListener('play', () => { if (window.__gpmPausePreview) el.pause(); });
  };
  const origCreate = Document.prototype.createElement;
  Document.prototype.createElement = function (tag, ...rest) {
    const el = origCreate.call(this, tag, ...rest);
    if (String(tag).toLowerCase() === 'video') hook(el);
    return el;
  };
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    if (!vids.has(this)) hook(this);
    if (window.__gpmPausePreview) { try { this.pause(); } catch (e) {} return Promise.resolve(); }
    return origPlay.apply(this, arguments);
  };
  window.__gpmPauseAll = () => {
    window.__gpmPausePreview = true;
    let n = 0;
    vids.forEach(v => { try { if (!v.paused) { v.pause(); n++; } } catch (e) {} });
    return n;
  };
  window.__gpmResumeAllowed = () => { window.__gpmPausePreview = false; };
})();
"""


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------- GPM Login
def gpm_start(profile_id, retries=8):
    """Mở profile qua GPM Local API, trả về cổng remote debugging."""
    # Nếu lần trước script đã mở profile và cửa sổ vẫn còn -> dùng lại cổng cũ
    port_file = LOG_DIR / f"port_{profile_id[:8]}.txt"
    if port_file.exists():
        try:
            old = int(port_file.read_text().strip())
            requests.get(f"http://127.0.0.1:{old}/json/version", timeout=3).raise_for_status()
            log(f"GPM: profile đang mở sẵn, dùng lại debug port {old}")
            return old
        except Exception:
            pass
    url = f"{GPM_API}/profiles/start/{profile_id}?window_size=1400,900&window_pos=0,0&skip_proxy_check=true"
    for i in range(retries):
        r = requests.get(url, timeout=90).json()
        if r.get("success"):
            port = r["data"]["remote_debugging_port"]
            port_file.write_text(str(port))
            log(f"GPM: profile đã mở, debug port {port}")
            return port
        msg = r.get("message", "")
        log(f"GPM: start thất bại ({msg}), thử lại {i + 1}/{retries}")
        if msg == "ProfileInUse":
            # Trình duyệt đang mở nhưng không có cổng debug -> đóng rồi mở lại
            if i == 0:
                requests.get(f"{GPM_API}/profiles/stop/{profile_id}", timeout=60)
            elif i == 2:
                kill_profile_browser(profile_id)
        time.sleep(5)
    raise RuntimeError("Không mở được profile GPM qua API")


def gpm_stop(profile_id):
    try:
        requests.get(f"{GPM_API}/profiles/stop/{profile_id}", timeout=60)
    except Exception:
        pass


def kill_profile_browser(profile_id):
    """GPM stop đôi khi không đóng được cửa sổ mở bằng tay -> tự tắt chrome của profile."""
    ps = ("Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -match '{profile_id}' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    log("GPM: đã tắt tiến trình chrome của profile")
    time.sleep(4)


# ----------------------------------------------------------------- tiện ích Playwright
def footer_button(page, name):
    """Nút `name` ở thanh đáy (Hủy / Quay lại / Tiếp / Lên lịch / Chia sẻ).
    Cùng tên còn có nút ở stepper đầu trang, mũi tên cuộn hình thu nhỏ, lựa chọn 'Lên lịch'...
    Thanh đáy căn phải nên lấy nút nằm xa nhất về bên phải."""
    loc = page.get_by_role("button", name=name, exact=True)
    best, best_x = None, -1
    for i in range(loc.count()):
        b = loc.nth(i).bounding_box()
        if b and b["x"] > best_x:
            best, best_x = loc.nth(i), b["x"]
    if best is None:
        raise RuntimeError(f"Không thấy nút '{name}'")
    return best


def current_step(page):
    """Bước đang chọn trên stepper Tạo · Chỉnh sửa · Chia sẻ (aria-current="step")."""
    return page.evaluate("""() => {
        for (const b of document.querySelectorAll('[role=button][aria-current="step"]')) {
            const t = (b.innerText || '').trim();
            if (['Tạo', 'Chỉnh sửa', 'Chia sẻ'].includes(t)) return t;
        }
        return null;
    }""")


def body_text(page):
    return page.evaluate("() => document.body.innerText")


def wait_copyright_check(page, max_s=600):
    """Bước Chỉnh sửa: FB quét bản quyền, video dài có thể mất vài phút; chờ xong rồi mới bấm Tiếp."""
    t0 = last = time.time()
    while "Đang kiểm tra nội dung có bản quyền" in body_text(page):
        if time.time() - t0 > max_s:
            log("CẢNH BÁO: kiểm tra bản quyền quá lâu, vẫn đi tiếp")
            return
        if time.time() - last >= 30:
            log(f"  ...FB đang kiểm tra bản quyền ({time.time() - t0:.0f}s)")
            last = time.time()
        page.wait_for_timeout(2000)


def goto_step(page, target, max_s=900):
    """Bấm Tiếp cho tới khi stepper ở `target`; nếu trang chưa chuyển thì bấm lại."""
    t0 = time.time()
    while True:
        cur = current_step(page)
        if cur == target:
            page.wait_for_timeout(1500)
            return
        if time.time() - t0 > max_s:
            shot(page, "step_stuck")
            raise RuntimeError(f"Không chuyển được sang bước '{target}' (đang ở '{cur}')")
        if cur == "Chỉnh sửa":
            wait_copyright_check(page)
        try:
            btn = footer_button(page, "Tiếp")
        except RuntimeError:
            page.wait_for_timeout(2000)
            continue
        if not btn.is_enabled():
            page.wait_for_timeout(2000)
            continue
        btn.click()
        for _ in range(15):                 # chờ tối đa 15s cho stepper đổi
            page.wait_for_timeout(1000)
            if current_step(page) != cur:
                break
        else:
            log(f"  ...bấm Tiếp ở bước '{cur}' chưa chuyển trang, bấm lại")


def upload_via_cdp(page, open_button, path):
    """Playwright từ chối chuyển file >50MB khi nối qua cổng debug ("not co-located").
    Thay vào đó tự chặn hộp thoại chọn file bằng DevTools rồi gọi DOM.setFileInputFiles với
    đường dẫn trên đĩa: Chrome tự đọc file, không giới hạn dung lượng."""
    cdp = page.context.new_cdp_session(page)
    chooser = {}
    cdp.on("Page.fileChooserOpened", lambda ev: chooser.update(ev))
    cdp.send("Page.enable")
    cdp.send("Page.setInterceptFileChooserDialog", {"enabled": True})
    try:
        open_button.click()
        for _ in range(100):
            if chooser:
                break
            page.wait_for_timeout(100)
        if "backendNodeId" not in chooser:
            raise RuntimeError("Không bắt được hộp thoại chọn file của Facebook")
        cdp.send("DOM.setFileInputFiles", {"files": [path], "backendNodeId": chooser["backendNodeId"]})
    finally:
        try:
            cdp.send("Page.setInterceptFileChooserDialog", {"enabled": False})
        except Exception:
            pass
        cdp.detach()


def upload_percent(page):
    """Số % Facebook hiển thị cạnh tên file trong khối 'File phương tiện' (None nếu không còn)."""
    return page.evaluate("""() => {
        const t = document.body.innerText;
        const a = t.indexOf('File phương tiện'), b = t.indexOf('Chi tiết về thước phim');
        const m = t.slice(a < 0 ? 0 : a, b < 0 ? undefined : b).match(/(\\d{1,3})%/);
        return m ? parseInt(m[1]) : null;
    }""")


def pause_preview(page):
    try:
        return page.evaluate("() => window.__gpmPauseAll ? window.__gpmPauseAll() : -1")
    except Exception:
        return -1


def shot(page, name):
    p = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"
    try:
        page.screenshot(path=str(p))
    except Exception:
        pass
    return p


def parse_schedule(s):
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Không hiểu thời gian '{s}' (dùng DD/MM/YYYY HH:MM hoặc YYYY-MM-DD HH:MM)")


# ----------------------------------------------------------------- luồng đăng reel
def open_and_upload(page, video, caption, asset_id, pause_during_upload=True):
    """Mở trình soạn, tải video, nhập mô tả. Kết thúc ở bước 'Tạo' với video đã lên FB."""
    video = str(Path(video).resolve())
    if pause_during_upload:
        page.add_init_script(PAUSE_PREVIEW_JS)

    # 1. Mở trình soạn thước phim
    log("Mở trình soạn thước phim")
    page.goto(COMPOSER_URL.format(asset=asset_id), wait_until="domcontentloaded", timeout=90000)
    add_video = page.get_by_role("button", name="Thêm video")
    try:
        add_video.wait_for(timeout=30000)
    except PWTimeout:
        # Dự phòng: về trang chủ rồi bấm "Tạo thước phim"
        page.goto(f"https://business.facebook.com/latest/home/?asset_id={asset_id}", wait_until="domcontentloaded")
        page.get_by_role("button", name="Tạo thước phim").click()
        add_video.wait_for(timeout=30000)
    page.wait_for_timeout(2000)

    # 2. Tải video lên
    size_mb = Path(video).stat().st_size / 1e6
    log(f"Tải video: {video} ({size_mb:.0f} MB)")
    upload = {"receive": False}

    def _on_resp(r):   # FB gọi .../upload/requests/receive/ khi đã nhận đủ file
        if "video/upload/requests/receive" in r.url and r.ok:
            upload["receive"] = True

    page.on("response", _on_resp)
    upload_via_cdp(page, add_video, video)
    if pause_during_upload:
        page.wait_for_timeout(1500)
        log(f"Tạm dừng bản xem trước trong lúc tải ({pause_preview(page)} video đang phát)")

    # 3. Chờ tải lên xong. LƯU Ý: nút "Tiếp" sáng ngay khi mới tải được vài %, nên phải
    #    dựa vào số % cạnh tên file + tín hiệu FB báo đã nhận đủ (upload/requests/receive).
    max_wait = int(max(300, 120 + size_mb * 2))      # ~2s/MB qua proxy, tối thiểu 5 phút
    log(f"Chờ tải lên/xử lý video (tối đa {max_wait // 60} phút)...")
    t0 = last_log = time.time()
    pct_gone_since = None
    while True:
        page.wait_for_timeout(2000)
        if pause_during_upload:
            pause_preview(page)                      # bản xem trước có thể được tạo lại
        elapsed = time.time() - t0
        pct = upload_percent(page)
        try:
            ready = footer_button(page, "Tiếp").is_enabled()
        except RuntimeError:
            ready = False
        if upload["receive"] and ready and (pct is None or pct >= 100):
            break
        # Dự phòng nếu FB đổi endpoint: % đã biến mất 30s, nút Tiếp sáng -> coi như xong
        if pct is None and ready and elapsed > 20:
            pct_gone_since = pct_gone_since or time.time()
            if time.time() - pct_gone_since > 30:
                break
        else:
            pct_gone_since = None
        if time.time() - last_log >= 30:
            log(f"  ...đang tải lên {pct if pct is not None else '?'}% ({elapsed:.0f}s)")
            last_log = time.time()
        body = body_text(page)
        if any(k in body for k in ("không hỗ trợ", "Không thể tải", "Đã xảy ra lỗi", "ít nhất 5 ảnh")):
            shot(page, "upload_error")
            raise RuntimeError("Facebook báo lỗi khi tải video (xem ảnh trong logs/)")
        if elapsed > max_wait:
            shot(page, "upload_timeout")
            raise RuntimeError("Quá thời gian chờ tải lên/xử lý video")
    page.remove_listener("response", _on_resp)
    log(f"Video sẵn sàng sau ~{time.time() - t0:.0f}s (FB đã nhận đủ file: {upload['receive']})")
    if pause_during_upload:
        page.evaluate("() => window.__gpmResumeAllowed && window.__gpmResumeAllowed()")

    # 4. Mô tả
    log("Nhập mô tả")
    box = page.get_by_role("textbox", name=DESC_BOX_NAME)
    box.scroll_into_view_if_needed()
    box.click()
    page.keyboard.type(caption, delay=30)
    page.wait_for_timeout(1200)
    page.keyboard.press("Escape")       # đóng gợi ý hashtag nếu có
    page.wait_for_timeout(500)
    got = box.inner_text().strip()
    if got != caption.strip():
        log(f"CẢNH BÁO: mô tả đọc lại khác: {got!r}")


def finish_post(page, caption, when=None, dry_run=False, asset_id=PAGE_ASSET_ID):
    """Từ bước hiện tại của trình soạn: sang 'Chia sẻ', chọn lịch, bấm nút cuối, xác nhận.
    when=None -> đăng ngay; when=datetime -> lên lịch. Trả về True nếu đã thấy bài trong danh sách."""
    # 5. Tiếp -> Chỉnh sửa (chờ kiểm tra bản quyền) -> Tiếp -> Chia sẻ
    log(f"Đang ở bước '{current_step(page)}', chuyển sang 'Chia sẻ'")
    goto_step(page, "Chia sẻ")
    page.get_by_role("button", name="Chia sẻ ngay", exact=True).wait_for(timeout=30000)
    page.wait_for_timeout(1500)

    # 6. Chọn đăng ngay / lên lịch
    if when:
        log(f"Lên lịch: {when:%d/%m/%Y %H:%M}")
        opt = page.get_by_role("button", name="Lên lịch", exact=True).first
        opt.scroll_into_view_if_needed()
        opt.click()
        d = page.get_by_placeholder("dd/mm/yyyy").first
        d.wait_for(timeout=15000)
        d.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(when.strftime("%d/%m/%Y"), delay=50)
        page.wait_for_timeout(600)
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        page.keyboard.press("Escape")
        for label, val in (("giờ", f"{when.hour:02d}"), ("phút", f"{when.minute:02d}")):
            sp = page.get_by_role("spinbutton", name=label).first
            sp.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(val, delay=60)
            page.wait_for_timeout(300)
        page.keyboard.press("Tab")
        page.wait_for_timeout(800)
        rb = page.evaluate("""() => ({
            date: document.querySelector('input[placeholder="dd/mm/yyyy"]')?.value,
            h: document.querySelector('input[aria-label="giờ"]')?.getAttribute('aria-valuenow'),
            m: document.querySelector('input[aria-label="phút"]')?.getAttribute('aria-valuenow')})""")
        log(f"Đọc lại lịch: {rb}")
        ok = (rb.get("date") or "").startswith(f"{when.day} ") and str(when.year) in (rb.get("date") or "") \
            and int(rb.get("h") or -1) == when.hour and int(rb.get("m") or -1) == when.minute
        if not ok:
            shot(page, "schedule_mismatch")
            raise RuntimeError(f"Ngày giờ không khớp sau khi nhập: {rb}")
        final_name, list_url = "Lên lịch", SCHEDULED_URL
    else:
        log("Đăng ngay")
        final_name, list_url = "Chia sẻ", PUBLISHED_URL

    shot(page, "before_submit")
    if dry_run:
        log("DRY-RUN: dừng trước nút cuối, không bấm. Bỏ bản nháp và quay về danh sách.")
        page.goto(list_url.format(asset=asset_id), wait_until="domcontentloaded", timeout=90000)
        return False

    # 7. Bấm nút cuối
    footer_button(page, final_name).click()
    log(f"Đã bấm '{final_name}', chờ Facebook xác nhận...")
    page.wait_for_timeout(15000)

    # 8. Kiểm tra trong danh sách (trình soạn đôi khi treo vòng xoay dù đã thành công)
    check = page.context.new_page()
    try:
        for attempt in range(12):     # ~3 phút
            check.goto(list_url.format(asset=asset_id), wait_until="domcontentloaded", timeout=90000)
            check.wait_for_timeout(8000)
            if caption.strip()[:25] in body_text(check):
                log("XÁC NHẬN: đã thấy bài trong danh sách " + ("Đã lên lịch" if when else "Đã đăng"))
                shot(check, "confirmed")
                return True
            log(f"Chưa thấy bài trong danh sách, thử lại {attempt + 1}/12")
            check.wait_for_timeout(7000)
    finally:
        check.close()
    shot(page, "not_confirmed")
    raise RuntimeError("Không thấy bài trong danh sách sau 3 phút – kiểm tra tay trên Business Suite")


def post_reel(page, video, caption, when=None, dry_run=False, asset_id=PAGE_ASSET_ID, resume=False):
    page.on("dialog", lambda d: d.accept())
    if resume:
        if "reels_composer" not in page.url:
            raise RuntimeError("--resume cần tab đầu tiên của profile đang mở trình soạn thước phim")
    else:
        open_and_upload(page, video, caption, asset_id)
    return finish_post(page, caption, when, dry_run, asset_id)


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Đăng/lên lịch Reels lên Fanpage qua profile GPM Login")
    ap.add_argument("--video", help="Đường dẫn file video (.mp4)")
    ap.add_argument("--caption", required=True, help="Mô tả / hashtag")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--schedule", help='Thời điểm đăng, ví dụ "07/10/2026 12:00" hoặc "2026-10-07 12:00"')
    g.add_argument("--now", action="store_true", help="Đăng ngay")
    ap.add_argument("--resume", action="store_true",
                    help="Trình soạn đang mở sẵn với video + mô tả: bỏ qua tải lên, chạy tiếp tới cuối")
    ap.add_argument("--profile", default=PROFILE_ID, help="ID profile GPM")
    ap.add_argument("--asset-id", default=PAGE_ASSET_ID, help="asset_id của fanpage trong Business Suite")
    ap.add_argument("--dry-run", action="store_true", help="Làm hết các bước nhưng không bấm nút cuối")
    ap.add_argument("--close-after", action="store_true", help="Đóng profile GPM khi xong")
    a = ap.parse_args()

    if not a.resume:
        if not a.video:
            sys.exit("Thiếu --video (hoặc dùng --resume)")
        if not Path(a.video).is_file():
            sys.exit(f"Không thấy file video: {a.video}")
    when = parse_schedule(a.schedule) if a.schedule else None
    if when and when <= datetime.now():
        sys.exit("Thời điểm lên lịch phải ở tương lai")

    port = gpm_start(a.profile)
    time.sleep(5)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()
        try:
            ok = post_reel(page, a.video, a.caption, when, a.dry_run, a.asset_id, a.resume)
        except Exception as e:
            shot(page, "error")
            log(f"LỖI: {e}")
            raise
        finally:
            if a.close_after:
                gpm_stop(a.profile)
    log("Hoàn tất." if ok else "Kết thúc (dry-run).")


if __name__ == "__main__":
    main()
