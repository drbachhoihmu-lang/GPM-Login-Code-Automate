# GPM-Login-Code-Automate – Tự động đăng / lên lịch Thước phim (Reels) lên Fanpage

Script Playwright chạy **trên đúng profile GPM Login** (giữ nguyên đăng nhập, proxy, vân tay trình duyệt),
thao tác trong Meta Business Suite y như người thật: Tạo thước phim → tải video → mô tả → Chia sẻ → Lên lịch.

## Cài đặt (đã có sẵn trên máy này)
- Python 3.12, `playwright`, `requests` (`pip install -r requirements.txt` nếu cài máy khác; không cần `playwright install` vì dùng Chromium của GPM)
- GPM Login Global đang mở (API cục bộ ở `http://127.0.0.1:9495/api/v1`)

## Dùng
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

Tuỳ chọn:
- `--dry-run` : làm đủ các bước, chụp màn hình `logs/..._before_submit.png`, **không** bấm nút cuối.
- `--resume` : trình soạn đang mở sẵn với video + mô tả (ví dụ lần chạy trước lỗi giữa chừng) → bỏ qua tải lên, chạy tiếp tới cuối. Không cần `--video`.
- `--close-after` : đóng profile GPM khi xong.
- `--profile`, `--asset-id` : đổi profile GPM / fanpage khác (mặc định đã đặt trong file).

## Script làm gì
1. Gọi GPM API mở profile `3fcc3942-...` (nếu profile đang mở bằng tay thì tự đóng và mở lại để có cổng debug).
2. Kết nối Playwright vào trình duyệt đó, mở trình soạn thước phim của fanpage.
3. Tải video qua nút **Thêm video**; trong lúc tải, **bản xem trước được tạm dừng** (im tiếng, đứng hình) cho tới khi tải xong. Chờ Facebook báo đã nhận đủ file.
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
