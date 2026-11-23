"""
yt_upload.py — Tải video lên YouTube bằng YouTube Data API v3 (dùng trong Google Colab).

Cần trong Colab Secrets:
  YT_CLIENT_JSON    nội dung file client_secret_xxx.json (OAuth client loại Desktop app)
  YT_REFRESH_TOKEN  tạo bằng authorize_once() — chỉ làm một lần

Quy định của YouTube: dự án API CHƯA qua kiểm duyệt (audit) thì mọi video tải lên bị khóa ở chế độ
riêng tư. Vì vậy chỉ bật tải tự động (CFG['YT_AUTO_UPLOAD'] = True) sau khi YouTube duyệt dự án.
"""
from __future__ import annotations

import datetime as dt
import json, os, time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",      # tải video + thumbnail
          "https://www.googleapis.com/auth/youtube.force-ssl"]    # thêm vào playlist + đọc trạng thái xử lý
DEFAULT_PLAYLIST = "Danh nhân thế giới"                          # playlist mặc định cho mọi video
TOKEN_URI = "https://oauth2.googleapis.com/token"
REDIRECT = "http://localhost"
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def _client(client_json: str) -> dict:
    cfg = json.loads(client_json)
    if "installed" not in cfg:
        raise SystemExit("❌ YT_CLIENT_JSON phải là file của OAuth client loại 'Desktop app' (có khóa \"installed\").")
    return cfg


# ----------------------------------------------------------------------------- cấp quyền (1 lần)
def authorize_once(client_json: str) -> str:
    """Mở link đăng nhập, người dùng dán lại địa chỉ trang 'localhost' → trả về refresh token."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(_client(client_json), scopes=SCOPES, redirect_uri=REDIRECT)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="false")
    print("① Mở link này, đăng nhập đúng tài khoản sở hữu KÊNH YouTube, rồi bấm Cho phép:\n")
    print(url, "\n")
    print("② Trình duyệt sẽ chuyển tới một trang lỗi 'localhost' (không sao cả).")
    print("   Sao chép TOÀN BỘ địa chỉ trên thanh địa chỉ (bắt đầu bằng http://localhost/?...) rồi dán vào ô bên dưới.\n")
    while True:
        resp = input("Dán địa chỉ vào đây rồi Enter: ").strip()
        q = parse_qs(urlparse(resp).query)
        if resp.startswith(("http://localhost", "localhost")) and "code" in q:
            break
        if "error" in q:
            raise SystemExit(f"❌ Google báo: {q['error'][0]} — bạn chưa bấm Cho phép / chưa tích đủ quyền. Chạy lại ô này.")
        print("⚠ Địa chỉ CHƯA ĐÚNG. Địa chỉ cần chép phải bắt đầu bằng  http://localhost/?state=  và có  code=4/")
        print("  Hãy quay lại tab trang lỗi 'This site can't be reached', bấm vào thanh địa chỉ, Ctrl+A, Ctrl+C rồi dán lại.\n")
    try:
        flow.fetch_token(code=q["code"][0])
    except Exception as e:
        raise SystemExit(f"❌ Mã cấp quyền không dùng được ({e}). Mã chỉ sống vài phút và chỉ dùng 1 lần — bấm ▶ chạy lại ô này.")
    granted = set(flow.credentials.scopes or [])
    missing = [x.rsplit("/", 1)[-1] for x in SCOPES if granted and x not in granted]
    if missing:
        raise SystemExit(f"❌ Bạn chưa tích đủ quyền (thiếu: {', '.join(missing)}). Chạy lại ô này và TÍCH CẢ 2 Ô quyền.")
    rt = flow.credentials.refresh_token
    if not rt:
        raise SystemExit("❌ Google không trả về refresh token. Chạy lại ô này một lần nữa.")
    return rt


# ----------------------------------------------------------------------------- kết nối
def service(client_json: str, refresh_token: str):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    c = _client(client_json)["installed"]
    creds = Credentials(None, refresh_token=refresh_token, token_uri=TOKEN_URI,
                        client_id=c["client_id"], client_secret=c["client_secret"], scopes=SCOPES)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ----------------------------------------------------------------------------- lịch đăng
def next_publish_time(hhmm: str, tz: str = "Asia/Ho_Chi_Minh", min_lead_minutes: int = 180, days_ahead: int = 0) -> str:
    """Giờ đăng kế tiếp (ví dụ '19:00' giờ Việt Nam) → chuỗi ISO 8601 UTC mà YouTube yêu cầu."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    now = dt.datetime.now(z)
    h, m = map(int, hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t < now + dt.timedelta(minutes=min_lead_minutes):
        t += dt.timedelta(days=1)
    t += dt.timedelta(days=days_ahead)               # nhiều video cùng lúc → mỗi ngày một video
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_body(title: str, description: str, tags: list[str], privacy: str = "private",
               publish_at: str | None = None, category_id: str = "27", lang: str = "vi") -> dict:
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:                                   # YouTube: hẹn giờ chỉ hợp lệ khi đang 'private'
        status.update(privacyStatus="private", publishAt=publish_at)
    return {"snippet": {"title": title[:100], "description": description[:5000], "tags": tags,
                        "categoryId": category_id, "defaultLanguage": lang, "defaultAudioLanguage": lang},
            "status": status}


# ----------------------------------------------------------------------------- playlist + trạng thái
def _norm(t: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", t).casefold().strip()

def find_playlist(yt, name: str) -> str | None:
    tok = None
    while True:
        r = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=tok).execute()
        for p in r.get("items", []):
            if _norm(p["snippet"]["title"]) == _norm(name):
                return p["id"]
        tok = r.get("nextPageToken")
        if not tok:
            return None

def add_to_playlist(yt, video_id: str, playlist_name: str, log=print) -> bool:
    pid = find_playlist(yt, playlist_name)
    if not pid:
        log(f"  ⚠ không tìm thấy playlist '{playlist_name}' trên kênh — bỏ qua bước thêm vào playlist")
        return False
    yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
    log(f"  ✓ đã thêm vào playlist '{playlist_name}'")
    return True

def wait_processed(yt, video_id: str, timeout_min: int = 40, log=print) -> dict:
    """Chờ YouTube xử lý xong video. Trả về trạng thái; báo rõ nếu bị từ chối (bản quyền, trùng lặp…)."""
    end = time.time() + timeout_min * 60
    st = {}
    while time.time() < end:
        items = yt.videos().list(part="status,processingDetails", id=video_id).execute().get("items", [])
        if items:
            st = items[0]["status"]; pr = items[0].get("processingDetails", {})
            up, ps = st.get("uploadStatus"), pr.get("processingStatus")
            if up in ("rejected", "failed", "deleted"):
                log(f"  ❌ YouTube {up}: {st.get('rejectionReason') or st.get('failureReason')}")
                return st
            if up == "processed" or ps == "succeeded":
                log("  ✓ YouTube đã xử lý xong video")
                return st
            left = pr.get("processingProgress", {}).get("timeLeftMs")
            log(f"  YouTube đang xử lý… ({ps or up}{f', còn ~{int(left) // 60000} phút' if left else ''})")
        time.sleep(30)
    log("  ⚠ quá thời gian chờ — video vẫn đang được YouTube xử lý, xem lại trong YouTube Studio")
    return st

# ----------------------------------------------------------------------------- tải lên
def upload(yt, video: Path, body: dict, thumbnail: Path | None = None, log=print,
           playlist: str | None = DEFAULT_PLAYLIST, wait: bool = True) -> str:
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=50 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
    resp, tries = None, 0
    while resp is None:
        try:
            status, resp = req.next_chunk()
            if status:
                log(f"  đang tải lên YouTube… {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1; time.sleep(5 * tries); continue
            raise
    vid = resp["id"]
    log(f"  ✓ đã tải lên: https://youtu.be/{vid}")
    if thumbnail and thumbnail.exists():
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg")).execute()
            log("  ✓ đã đặt thumbnail")
        except HttpError as e:
            log(f"  ⚠ chưa đặt được thumbnail ({e.resp.status}) — kênh cần xác minh số điện thoại")
    if playlist:
        try:
            add_to_playlist(yt, vid, playlist, log)
        except HttpError as e:
            log(f"  ⚠ chưa thêm được vào playlist ({e.resp.status}) — cần cấp lại quyền (ô số 3)")
    if wait:
        try:
            st = wait_processed(yt, vid, log=log)
            if st.get("publishAt"):
                log(f"  🕒 đã hẹn công khai lúc {st['publishAt']} (UTC)")
            else:
                log(f"  👁 chế độ hiển thị: {st.get('privacyStatus')}")
        except HttpError as e:
            log(f"  ⚠ chưa đọc được trạng thái xử lý ({e.resp.status}) — cần cấp lại quyền (ô số 3)")
    return vid


def upload_folder(yt, out_dir: Path, privacy="private", publish_at=None, log=print,
                  playlist: str | None = DEFAULT_PLAYLIST) -> str:
    """Tải lên từ thư mục kết quả của pipeline (mp4 + title + description + tags + thumbnail)."""
    out_dir = Path(out_dir)
    video = next(out_dir.glob("*.mp4"))
    title = (out_dir / "youtube_title.txt").read_text(encoding="utf-8").splitlines()[0].strip()
    desc = (out_dir / "youtube_description.txt").read_text(encoding="utf-8")
    tags_file = out_dir / "tags.txt"
    tags = [t.strip() for t in tags_file.read_text(encoding="utf-8").split(",") if t.strip()] if tags_file.exists() else []
    vid = upload(yt, video, build_body(title, desc, tags, privacy, publish_at), out_dir / "thumbnail.jpg", log, playlist)
    (out_dir / "youtube_video_id.txt").write_text(f"https://youtu.be/{vid}\n", encoding="utf-8")
    return vid
