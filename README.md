# GPM-Login-Code-Automate – Tự động đăng / lên lịch Thước phim (Reels) lên Fanpage

Script Playwright chạy **trên đúng profile GPM Login** (giữ nguyên đăng nhập, proxy, vân tay trình duyệt),
thao tác trong Meta Business Suite y như người thật: Tạo thước phim → tải video → mô tả → Chia sẻ → Lên lịch.

## Cài đặt (đã có sẵn trên máy này)
- Python 3.12, `playwright`, `requests` (`pip install -r requirements.txt` nếu cài máy khác; không cần `playwright install` vì dùng Chromium của GPM)
- GPM Login Global đang mở (API cục bộ ở `http://127.0.0.1:9495/api/v1`)

## Giao diện web (khuyên dùng)
Bấm đúp icon **GPMReels** ngoài Desktop (= `pythonw tray.py`): phần mềm chạy **ở khay hệ thống** cạnh đồng hồ như Unikey, không có cửa sổ đen. Bấm đúp icon ở khay → cửa sổ nhỏ (nút *Mở giao diện web*, *Mở Google Sheet*, *Ẩn*, *Thoát hẳn*); bấm **X** chỉ ẩn xuống khay; chuột phải icon khay → **Thoát hẳn** mới tắt. Bấm icon Desktop lần nữa khi đang chạy chỉ mở lại giao diện web. `pythonw tray.py --tray` = khởi động ẩn sẵn ở khay, không bật cửa sổ. (Cách cũ có cửa sổ đen: `start_ui.bat`.)
Giao diện web tại `http://127.0.0.1:8765`: **menu dọc bên trái** (5 tab), nội dung tab đang chọn hiện bên phải; tab đang mở được nhớ lại (và có trên URL: `#lich`, `#profile`, `#log`, `#anh`, `#caidat`). Cuối menu là trạng thái GPM Login / việc đang chạy / tự động kiểm tra và nút *Mở Google Sheet*.
- **Lịch đăng** (badge = số bài chờ): bảng hàng đợi đọc từ Sheet với cột *Kế hoạch* (sẽ đăng / vì sao bỏ qua); nút **Bắt đầu đăng các bài chờ** (có hỏi xác nhận), **Xem kế hoạch**, **Chạy thử**, **Đăng lại hàng lỗi**, lọc theo hàng / số bài tối đa; khung **Tự động kiểm tra Sheet**.
- **Profile GPM** (badge = số profile đang mở / tổng): mỗi profile một thẻ, chấm xanh = đang mở và điều khiển được, vàng = mở tay, xám = đóng; **fanpage mặc định** của profile lấy từ tab Pages (cột Profile GPM — mỗi profile 1 fanpage); nút **Mở Chrome / Mở Business Suite / Đóng Chrome**, nút **Mở ↗** để mở đúng trang đó trong profile.
- **Đang chạy & Log**: log chạy trực tiếp, nút Dừng; bấm bất kỳ nút chạy nào ở tab Lịch đăng sẽ tự chuyển sang tab này.
- **Ảnh chụp**: ảnh màn hình các bước đăng trong `logs/`.
- **Cài đặt**: link Sheet, URL webhook, profile mặc định, tên tab → lưu vào `config.json` (không đưa lên git); nút **Tắt phần mềm**.
Điều kiện: GPM Login đang chạy. Các nút chỉ gọi lại đúng các script dòng lệnh bên dưới (log ở `logs/ui_*.log`).

## Dùng dòng lệnh
```bash
python post_reel.py --video "D:\videos\clip1.mp4" --caption "abc #thacsianhduc" --schedule "07/10/2026 12:00"
```
```bash
python post_reel.py --video "D:\videos\clip1.mp4" --caption "abc #thacsianhduc" --now
```
Từ Google Drive (thư mục phải chia sẻ **"Bất kỳ ai có đường liên kết"**):
```bash
python post_reel.py --drive "https://drive.google.com/drive/folders/XXXX" --schedule "07/10/2026 19:00"
```
- Thư mục: lấy file video đầu tiên; mô tả lấy từ dòng **`Tiêu đề: ...`** trong file kịch bản `.docx` cùng thư mục (truyền `--caption` nếu muốn tự đặt; không có cả hai thì dùng tên video).
- Link file video trực tiếp (`/file/d/ID/view`) cũng được; khi đó cần `--caption`.
- Video tải về nằm ở `downloads/`.

### Đăng hàng loạt theo Google Sheet (`post_sheet.py`)
Sheet: **Lịch đăng Reels - Fanpage** (chia sẻ *Bất kỳ ai có đường liên kết → Người xem*), gồm **3 tab** — dựng tự động bằng hàm `setupSheet()` trong [sheet_webhook.gs](sheet_webhook.gs) (xem phần cài Apps Script bên dưới):

**Tab `Pages` (danh mục, mỗi page 1 hàng):** `Tên page | asset_id | Profile GPM | Người phụ trách | Giờ đăng mặc định | Ghi chú`. Thêm page mới = thêm 1 hàng (asset_id lấy trên URL Business Suite `?asset_id=…`; Profile GPM = một phần tên profile, vd `002`).

**Tab `Lịch đăng` (hàng đợi, script đọc):**

| Ngày đăng | Giờ đăng | Page | Video | Tiêu đề | Hashtag | Trạng thái | Ghi chú |
|---|---|---|---|---|---|---|---|
| `08/10/2026` (hoặc `ngay`) | `19:00` / `19h30`; để trống = *Giờ đăng mặc định* của page ở tab Pages | chọn từ dropdown (tab Pages) | link file Drive / thư mục Drive / đường dẫn máy | mô tả (có thể gộp hashtag) | tuỳ chọn | script ghi; tay ghi `bỏ qua` | script ghi |

(Bố cục cũ 1 cột gộp `Thời gian đăng` = `08/10/2026 19:00` vẫn được nhận.)

- **Page** → script tra tab Pages lấy **asset_id + profile GPM**, kiểm tra ô "Đăng lên" của trình soạn đúng trang đó mới đăng, và sau khi đăng ghi lại tên trang thật. **Để trống Page** → dùng fanpage mặc định của profile (hàng đầu tiên trong tab Pages có Profile GPM đó). Page không có trong tab Pages → script mở bộ chọn trang trong Business Suite để dò asset_id (cache `logs/pages_<profile>.json`). Có thể thêm cột `Profile` trên hàng để ghi đè profile.
- Các hàng cùng profile được gom lại, mở trình duyệt một lần. Profile đang mở bằng tay sẽ bị đóng và mở lại qua API để có cổng điều khiển.
- Nhìn riêng từng page: *Dữ liệu → Chế độ xem bộ lọc*, mỗi người tự lưu một view. Sort cột Thời gian để thấy lịch chung của mọi page.

**Tab `Đã đăng` (lưu trữ):** Apps Script `archiveOld()` dọn các hàng `Đã …` có giờ đăng quá 7 ngày sang đây (`installNightlyTrigger()` để tự chạy 3h sáng), hàng đợi luôn ngắn.

Tên tab đổi bằng `--tab` / `--pages-tab`. Không có tab `Lịch đăng` thì script đọc tab trong link (`#gid=`) hoặc tab đầu tiên.

**Profile / fanpage mới:** script không lưu danh sách profile, mỗi lần chạy hỏi thẳng GPM API, nên profile tạo mới trong GPM (đã đăng nhập Facebook) dùng được ngay. Để điền tab Pages nhanh:
```bash
python post_sheet.py --list-profiles                 # danh sách profile GPM hiện có
python post_sheet.py --scan-pages                     # mở từng profile, dò mọi fanpage + asset_id -> logs/pages_scan.csv
python post_sheet.py --scan-pages 002 --webhook URL   # chỉ profile 002, và ghi thẳng vào tab Pages (thêm mới / cập nhật theo asset_id)
```
- **Trạng thái**: trống hoặc `Chờ đăng` = sẽ đăng; bạn ghi tay `bỏ qua` để không đăng. Script ghi: `Đang đăng` → `Đã lên lịch` / `Đã đăng` / `Lỗi` (kèm lý do ở Ghi chú) / `Chạy thử OK` (dry-run). Hàng `Đã …` và `bỏ qua` không bao giờ đăng lại; hàng `Lỗi` chỉ đăng lại khi bấm **Đăng lại hàng lỗi** (hoặc `--retry-errors`); hàng `Đang đăng` (lần trước bị ngắt) sẽ được làm lại.
- **Cách đăng** (2 ô chọn trên cửa sổ GPMReels, ngay dưới ô *Tự động kiểm tra*; khung *Tự động kiểm tra Sheet* trên web cũng có):
  - *Lên lịch trên Facebook* (mặc định, `--mode schedule`): khi tới lượt, bài được đưa lên Facebook ngay và chọn **Lên lịch** đúng ngày giờ trong Sheet → Facebook tự phát; tắt máy vẫn đăng. Giờ đã qua → bỏ qua ("thời gian … đã qua").
  - *Tới giờ mới đăng* (`--mode due`): phần mềm giữ bài lại (*Kế hoạch*: "chưa tới giờ"), tới đúng ngày giờ trong Sheet mới mở trình soạn và bấm **Chia sẻ ngay** → trạng thái `Đã đăng`, Ghi chú kèm "lịch Sheet …" (và "trễ N phút" nếu đăng muộn). Máy + GPM Login phải đang bật lúc đó; bài lên sau giờ ghi vài phút (thời gian tải video). Giờ đã qua mà chưa đăng → đăng ngay ở lần kiểm tra kế tiếp.
- **Tự động kiểm tra**: bật checkbox *Tự động kiểm tra Sheet* (trên cửa sổ GPMReels hoặc trang web), đặt số phút (mặc định 5). Đến kỳ, phần mềm đọc Sheet; có bài `Chờ đăng` (và GPM Login đang bật, không có việc khác đang chạy) thì tự chạy đăng; kết quả mỗi lần kiểm tra hiện ngay dưới checkbox. Ở chế độ *Tới giờ mới đăng*, vòng kiểm tra còn **thức đúng phút** bài sớm nhất tới giờ, không phải đợi hết chu kỳ. Hàng lỗi không bị thử lại vô hạn — chỉ đăng lại khi bạn bấm *Đăng lại hàng lỗi*.
- Thư mục Drive mà **Tiêu đề để trống** → lấy dòng `Tiêu đề:` trong file kịch bản `.docx`, rồi nối thêm cột Hashtag.
- Cột Thời gian nên để định dạng *Văn bản thuần* (hoặc gõ `'08/10/2026 19:00` với dấu `'` đầu ô) để Sheets không tự đổi thành ngày.
- Hàng đã đăng còn được ghi nhớ trong `logs/sheet_state.json` → chạy lại không đăng trùng kể cả khi chưa có webhook. Mỗi lần chạy xuất `logs/sheet_report_*.csv` (Hàng / Page / Thời gian / Trạng thái / Ghi chú).

**Cài Apps Script (1 lần, ~3 phút)** — dựng 3 tab + webhook ghi ngược + dọn bài cũ:
1. Mở Sheet → *Tiện ích mở rộng → Apps Script* → dán toàn bộ [sheet_webhook.gs](sheet_webhook.gs) → Lưu.
2. Chọn hàm **`setupSheet`** ở thanh trên → **Chạy** → cho phép quyền. Sheet có đủ 3 tab (hàng mẫu cũ được chuyển sang `Lịch đăng`, tab cũ bị ẩn, có thể xoá).
3. (tuỳ chọn) Chọn hàm **`installNightlyTrigger`** → Chạy: mỗi đêm 3h tự dọn bài cũ sang `Đã đăng`.
4. *Triển khai → Tùy chọn triển khai mới → Ứng dụng web*: Thực thi với tư cách **Tôi**, Ai có quyền truy cập **Bất kỳ ai** → Triển khai → copy **URL ứng dụng web** (`…/exec`) → dán vào `SHEET_WEBHOOK` trong `post_sheet.py` (hoặc `--webhook URL`). Mở URL đó trên trình duyệt thấy `sheet_webhook OK` là đúng.
Không có webhook thì script vẫn chạy, chỉ không ghi được Trạng thái/Page vào Sheet (vẫn có báo cáo CSV trong `logs/`).

```bash
python post_sheet.py --sheet "https://docs.google.com/spreadsheets/d/XXXX/edit" --plan-only   # chỉ in kế hoạch
python post_sheet.py --sheet "https://docs.google.com/spreadsheets/d/XXXX/edit" --dry-run     # chạy thử, không bấm nút cuối
python post_sheet.py --sheet "https://docs.google.com/spreadsheets/d/XXXX/edit"               # đăng/lên lịch tất cả hàng đang chờ
```
Tuỳ chọn thêm: `--max 1` (chỉ 1 bài), `--rows 2,5` (chỉ các hàng này), `--pause 30-90` (nghỉ giữa 2 bài, giây), `--mode due` (tới giờ mới đăng, xem *Cách đăng* ở trên).

Tuỳ chọn:
- `--dry-run` : làm đủ các bước, chụp màn hình `logs/..._before_submit.png`, **không** bấm nút cuối.
- `--resume` : trình soạn đang mở sẵn với video + mô tả (ví dụ lần chạy trước lỗi giữa chừng) → bỏ qua tải lên, chạy tiếp tới cuối. Không cần `--video`.
- `--close-after` : đóng profile GPM khi xong.
- `--profile`, `--asset-id` : đổi profile GPM / fanpage khác (mặc định đã đặt trong file).

## Script làm gì
1. Gọi GPM API mở profile `3fcc3942-...` (nếu profile đang mở bằng tay thì tự đóng và mở lại để có cổng debug).
2. Kết nối Playwright vào trình duyệt đó, mở trình soạn thước phim của fanpage.
3. Tải video qua nút **Thêm video**; ngay khi bản xem trước bắt đầu chạy, script **bấm nút tạm dừng** (thẻ `<a>` đầu tiên trong nhóm 3 icon đè trên canvas xem trước) rồi đo lại bằng cách chụp canvas — log ghi `đứng yên = True`. Không thấy nút thì dùng dự phòng: đặt tốc độ phát = 0 + tắt tiếng. Tải xong bấm lại cho phát tiếp. Chờ Facebook báo đã nhận đủ file mới đi tiếp.
4. Nhập **Mô tả**, bấm **Tiếp** sang **Chỉnh sửa** (chờ Facebook kiểm tra bản quyền với video dài), rồi **Tiếp** sang **Chia sẻ**; đọc stepper để chắc đã chuyển bước, chưa chuyển thì bấm lại.
5. Chọn **Lên lịch**, nhập ngày (dd/mm/yyyy) + giờ:phút, đọc lại để chắc đúng, rồi bấm **Lên lịch** (hoặc **Chia sẻ** nếu `--now`).
6. Mở tab mới vào *Nội dung → Đã lên lịch* để **xác nhận bài đã nằm trong danh sách** (trình soạn của Facebook hay treo vòng xoay dù đã thành công).

Ảnh chụp từng lần chạy nằm trong `logs/`.

## Lưu ý
- Thước phim bắt buộc là **video**; nếu dùng ảnh thì Facebook đòi tối thiểu 5 ảnh. Muốn biến 1 ảnh thành video 6 giây:
  ```bash
  ffmpeg -y -loop 1 -i anh.png -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 -t 6 -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p" -r 30 -c:v libx264 -c:a aac -shortest test/test_reel.mp4
  ```
- Múi giờ lịch đăng lấy theo cài đặt trang (hiện là Asia/Jakarta = giờ Việt Nam).
- File lớn (hàng trăm MB) vẫn tải được: script đưa đường dẫn file cho Chrome qua DevTools (`DOM.setFileInputFiles`) vì Playwright chặn file >50 MB khi nối qua cổng debug. Nút "Tiếp" sáng ngay khi mới tải vài %, nên script chờ theo số % cạnh tên file và tín hiệu Facebook đã nhận đủ file.
- Facebook đổi giao diện thì có thể phải chỉnh lại tên nút trong `post_reel.py` (các tên nút đều là chữ tiếng Việt hiển thị trên màn hình).
