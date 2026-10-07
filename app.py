"""
Giao diện web cục bộ cho bộ công cụ đăng Reels: xem Sheet, profile GPM <-> fanpage, mở/đóng Chrome,
chạy kế hoạch / chạy thử / đăng thật và xem log ngay trên trình duyệt.

Chạy:  python app.py          (mở http://127.0.0.1:8765)
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import post_reel as pr                                   # noqa: E402
import post_sheet as ps                                  # noqa: E402

ROOT = Path(__file__).resolve().parent
LOG_DIR = pr.LOG_DIR
CONFIG_FILE = ROOT / "config.json"
UI_PORT = 8765
DEFAULT_CONFIG = {
    "sheet_url": "https://docs.google.com/spreadsheets/d/1966ErI9SHTrLOFM1MPor3EPovJOz6CNsWPH7Q4b_aQ8/edit",
    "webhook": "",
    "default_profile": pr.PROFILE_ID,
    "tab": ps.TAB_QUEUE,
    "pages_tab": ps.TAB_PAGES,
}

app = FastAPI(title="GPMReels")
app.mount("/logs", StaticFiles(directory=str(LOG_DIR)), name="logs")
app.mount("/assets", StaticFiles(directory=str(ROOT / "assets")), name="assets")


# ----------------------------------------------------------------- cấu hình
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


class Config(BaseModel):
    sheet_url: str = ""
    webhook: str = ""
    default_profile: str = ""
    tab: str = ps.TAB_QUEUE
    pages_tab: str = ps.TAB_PAGES


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
def get_config():
    return load_config()


@app.post("/api/config")
def set_config(c: Config):
    cfg = load_config()
    cfg.update(c.model_dump(exclude_unset=True))      # chỉ ghi đè các khoá được gửi lên
    if not cfg.get("default_profile"):
        cfg["default_profile"] = pr.PROFILE_ID
    save_config(cfg)
    return cfg


# ----------------------------------------------------------------- profile GPM
_proc_cache = {"t": 0, "ids": {}}


def running_profiles():
    """{profile_id: có cổng debug?} cho các profile đang có chrome chạy (cache 5s)."""
    if time.time() - _proc_cache["t"] < 20:      # cache 20s: gọi PowerShell ít thôi
        return _proc_cache["ids"]
    ids = {}
    try:
        ps_cmd = ("Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                  "Where-Object { $_.CommandLine -match 'user-data-dir' -and $_.CommandLine -notmatch '--type=' } | "
                  "ForEach-Object { $_.CommandLine }")
        out = pr.run_hidden(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            m = re.search(r"Profile\\([0-9a-f-]{36})", line)
            if m:
                ids[m.group(1)] = "remote-debugging-port" in line
    except Exception:
        pass
    _proc_cache.update(t=time.time(), ids=ids)
    return ids


def debug_port(profile_id):
    f = LOG_DIR / f"port_{profile_id[:8]}.txt"
    try:
        port = int(f.read_text().strip())
        requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).raise_for_status()
        return port
    except Exception:
        return None


def pages_for_profile(profile_id, profile_name, sheet_pages):
    """Fanpage gắn với profile theo tab Pages (mỗi profile thường = 1 fanpage; nhiều hàng thì liệt kê hết)."""
    out = []
    for p in sheet_pages:
        prof = (p.get("profile") or "").strip()
        if prof and (pr.norm_text(prof) in pr.norm_text(profile_name) or prof == profile_id or profile_id.startswith(prof.lower())):
            out.append({"name": p["name"], "asset": p["asset"]})
    return out


@app.get("/api/profiles")
def api_profiles():
    cfg = load_config()
    try:
        profiles = pr.gpm_profiles()
        gpm_ok = True
    except Exception:
        profiles, gpm_ok = [], False
    sheet_pages = []
    try:
        sheet_pages = ps.read_pages(cfg["sheet_url"], cfg["pages_tab"])
    except Exception:
        pass
    run = running_profiles()
    items = []
    for pid, name in profiles:
        port = debug_port(pid)
        if port:
            status = "controlled"
        elif pid in run:
            status = "manual"
        else:
            status = "closed"
        items.append({"id": pid, "name": name, "status": status, "port": port,
                      "default": pid == cfg["default_profile"],
                      "pages": pages_for_profile(pid, name, sheet_pages)})
    return {"gpm_ok": gpm_ok, "profiles": items}


class OpenReq(BaseModel):
    url: str | None = None


def _connect(port):
    from playwright.sync_api import sync_playwright
    return sync_playwright, port


def open_in_profile(profile_id, url):
    """Mở profile (nếu chưa) và điều hướng tab tới url. Chạy trong thread riêng."""
    from playwright.sync_api import sync_playwright
    port = pr.gpm_start(profile_id)
    time.sleep(4)
    if url:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = pr.pick_page(b.contexts[0])
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
    return port


@app.post("/api/profiles/{pid}/start")
def api_start(pid: str, req: OpenReq):
    try:
        port = open_in_profile(pid, req.url)
    except Exception as e:
        raise HTTPException(500, str(e)[:300])
    _proc_cache["t"] = 0
    return {"ok": True, "port": port}


@app.post("/api/profiles/{pid}/stop")
def api_stop(pid: str):
    pr.gpm_stop(pid)
    time.sleep(2)
    _proc_cache["t"] = 0
    return {"ok": True}


# ----------------------------------------------------------------- Sheet
@app.get("/api/sheet")
def api_sheet():
    cfg = load_config()
    link = cfg["sheet_url"]
    try:
        sid = ps.sheet_id(link)
    except Exception:
        return {"ok": False, "error": "Chưa có link Sheet hợp lệ"}
    try:
        tabs = ps.sheet_tabs(link)
        items = ps.read_sheet(link, cfg["tab"])
        pages = ps.read_pages(link, cfg["pages_tab"])
        for it in items:
            p = ps.lookup_page(pages, it["page"]) if it["page"] else None
            it["asset"] = p["asset"] if p else ""
            if p and not it["profile"]:
                it["profile"] = p["profile"]
        todo, skipped = ps.plan(items, link, ps.load_state(), None, False)
        plan_rows = []
        for it, when in todo:
            plan_rows.append({**it, "when": when.strftime("%d/%m/%Y %H:%M") if when else "ngay", "will": True, "reason": ""})
        for it, reason in skipped:
            plan_rows.append({**it, "when": it["time"], "will": False, "reason": reason})
        plan_rows.sort(key=lambda r: r["row"])
        gid = tabs.get(cfg["tab"])
        return {"ok": True, "id": sid, "tabs": tabs, "queue_gid": gid, "rows": plan_rows, "pages": pages,
                "embed": f"https://docs.google.com/spreadsheets/d/{sid}/edit?rm=minimal" + (f"#gid={gid}" if gid is not None else "")}
    except Exception as e:
        return {"ok": False, "id": sid, "error": str(e)[:300]}


# ----------------------------------------------------------------- chạy script (tiến trình con) + log
JOBS = {}


class RunReq(BaseModel):
    mode: str                 # plan | dry | real | scan
    rows: str | None = None
    max: int | None = None
    profile: str | None = None   # cho scan


def _spawn(args, label):
    if any(j["proc"].poll() is None for j in JOBS.values()):
        raise HTTPException(409, "Đang có việc khác chạy, chờ xong đã")
    jid = datetime.now().strftime("%Y%m%d_%H%M%S")
    logf = LOG_DIR / f"ui_{jid}.log"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    fh = open(logf, "w", encoding="utf-8")
    # Chạy bằng python.exe (có stdout/stderr thật) nhưng ẩn cửa sổ; log ghi ra file
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    proc = subprocess.Popen([exe] + args, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, env=env, **pr.HIDDEN)
    JOBS[jid] = {"proc": proc, "log": logf, "label": label, "started": datetime.now().isoformat(timespec="seconds"), "fh": fh}
    return jid


@app.post("/api/run")
def api_run(r: RunReq):
    cfg = load_config()
    base = ["post_sheet.py", "--sheet", cfg["sheet_url"], "--tab", cfg["tab"], "--pages-tab", cfg["pages_tab"],
            "--profile", cfg["default_profile"]]
    if cfg.get("webhook"):
        base += ["--webhook", cfg["webhook"]]
    if r.mode == "scan":
        args = ["post_sheet.py", "--scan-pages", r.profile or "all"] + (["--webhook", cfg["webhook"]] if cfg.get("webhook") else [])
        label = "Quét fanpage"
    elif r.mode in ("plan", "dry", "real"):
        args = base + (["--plan-only"] if r.mode == "plan" else ["--dry-run"] if r.mode == "dry" else [])
        if r.rows:
            args += ["--rows", r.rows]
        if r.max:
            args += ["--max", str(r.max)]
        label = {"plan": "Xem kế hoạch", "dry": "Chạy thử (dry-run)", "real": "ĐĂNG THẬT"}[r.mode]
    else:
        raise HTTPException(400, "mode không hợp lệ")
    jid = _spawn(args, label)
    return {"ok": True, "job": jid}


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "không có job")
    rc = j["proc"].poll()
    if rc is not None:
        try:
            j["fh"].close()
        except Exception:
            pass
    try:
        text = j["log"].read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ""
    # Bỏ các dòng traceback dài dòng
    lines = [ln for ln in text.splitlines() if not re.match(r"^\s*(File |\^|await |return |self\._|raise )", ln)]
    return {"job": jid, "label": j["label"], "running": rc is None, "code": rc, "log": "\n".join(lines[-400:])}


@app.get("/api/jobs")
def api_jobs():
    return [{"job": k, "label": v["label"], "running": v["proc"].poll() is None, "started": v["started"]}
            for k, v in sorted(JOBS.items(), reverse=True)]


@app.post("/api/jobs/{jid}/stop")
def api_job_stop(jid: str):
    j = JOBS.get(jid)
    if j and j["proc"].poll() is None:
        j["proc"].kill()
    return {"ok": True}


@app.post("/api/shutdown")
def api_shutdown():
    """Tắt server (nút 'Tắt phần mềm' trên giao diện). Dừng cả việc đang chạy nếu có."""
    for j in JOBS.values():
        if j["proc"].poll() is None:
            j["proc"].kill()
    threading.Timer(0.7, lambda: os._exit(0)).start()
    return {"ok": True}


@app.get("/api/screens")
def api_screens():
    files = sorted(LOG_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[:12]
    return [{"name": f.name, "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m %H:%M")} for f in files]


if __name__ == "__main__":
    print(f"Mở giao diện: http://127.0.0.1:{UI_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=UI_PORT, log_level="warning")
