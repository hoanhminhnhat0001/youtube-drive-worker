import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


HEADERS = [
    "Khối", "Môn học", "Thầy cô / khóa học", "Tên file khóa học",
    "Chương / trang tính", "Ô nguồn", "Nội dung / tiêu đề",
    "Link Google Sheet nguồn", "Phát hiện lúc", "Link YouTube",
    "Thư mục Drive đích ID", "Trạng thái tải", "Link file Drive",
    "Drive File ID", "Số lần thử", "Lỗi gần nhất", "Cập nhật lúc",
]
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def credentials():
    return Credentials(
        token=None,
        refresh_token=required("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("GOOGLE_CLIENT_ID"),
        client_secret=required("GOOGLE_CLIENT_SECRET"),
        scopes=SCOPES,
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def partition(url, count):
    digest = hashlib.sha256(url.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count


def source_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def safe_error(error, url):
    text = str(error).replace(url, "<redacted-youtube-url>")
    proxy_url = os.environ.get("YOUTUBE_PROXY", "").strip()
    if proxy_url:
        text = text.replace(proxy_url, "<redacted-proxy>")
    text = re.sub(r"(?i)\b(?:https?|socks5h?)://[^\s]+", "<redacted-proxy>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if "Sign in to confirm" in text or "Please sign in" in text:
        return "YOUTUBE_YEU_CAU_DANG_NHAP: GitHub runner bi YouTube chan/chong bot; can cookie YouTube hop le."
    if "Private video" in text:
        return "VIDEO_RIENG_TU: Tai khoan YouTube khong co quyen xem video nay."
    if "Video unavailable" in text:
        return "VIDEO_KHONG_KHA_DUNG: Video da bi xoa, khoa theo khu vuc hoac khong con truy cap duoc."
    return text[-1000:]


def update_row(sheets, sheet_id, sheet_name, row_number, values):
    data = [{"range": f"'{sheet_name}'!{column}{row_number}", "values": [[value]]}
            for column, value in values.items()]
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def find_existing_file(drive, marker):
    escaped = marker.replace("'", "\\'")
    result = drive.files().list(
        q=f"trashed=false and appProperties has {{ key='youtubeSourceHash' and value='{escaped}' }}",
        spaces="drive",
        fields="files(id,name,webViewLink,size)",
        pageSize=1,
    ).execute()
    files = result.get("files", [])
    return files[0] if files else None


def download_video(url, directory, proxy_session="", on_proxy_rotate=None):
    output = str(directory / "%(title).160B [%(id)s].%(ext)s")
    command = [
        "yt-dlp", "--no-playlist", "--newline", "--restrict-filenames",
        "--socket-timeout", "12", "--retries", "2", "--fragment-retries", "2",
        "--extractor-retries", "2", "--file-access-retries", "2",
        "--retry-sleep", "2", "--sleep-requests", "1",
        "--sleep-interval", "1", "--max-sleep-interval", "3",
        "--remote-components", "ejs:github",
        "--merge-output-format", "mp4",
        "-f", "bestvideo+bestaudio/best",
        "--print", "after_move:filepath", "-o", output, url,
    ]
    proxy_template = os.environ.get("YOUTUBE_PROXY", "").strip()
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if cookies_b64:
        cookie_path = directory / ".youtube-cookies.txt"
        try:
            cookie_path.write_bytes(base64.b64decode(cookies_b64, validate=True))
        except (ValueError, base64.binascii.Error) as error:
            raise RuntimeError("YOUTUBE_COOKIES_B64 is invalid") from error
        command[1:1] = ["--cookies", str(cookie_path)]

    result = None
    active_session = proxy_session or secrets.token_hex(8)
    proxy_attempts = 3 if proxy_template and "{session}" in proxy_template else 1
    for attempt_index in range(proxy_attempts):
        attempt_command = list(command)
        if proxy_template:
            proxy_url = proxy_template.replace("{session}", active_session)
            attempt_command[1:1] = ["--proxy", proxy_url]
        result = subprocess.run(attempt_command, capture_output=True, text=True, check=False)
        error_text = result.stderr or result.stdout or ""
        if result.returncode == 0:
            break
        rotate_markers = (
            "Sign in to confirm", "Please sign in", "HTTP Error 403",
            "403: Forbidden", "WRONG_VERSION_NUMBER", "UNEXPECTED_EOF_WHILE_READING",
            "more expected", "Remote components challenge solver",
        )
        if not any(marker in error_text for marker in rotate_markers):
            break
        if attempt_index + 1 < proxy_attempts:
            active_session = secrets.token_hex(8)
            if on_proxy_rotate:
                on_proxy_rotate(active_session)
    assert result is not None
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or "yt-dlp failed")
    candidates = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    files = [path for path in candidates if path.exists()]
    if not files:
        files = sorted(directory.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("yt-dlp completed without a media file")
    return files[0]


def proxy_session_cell(worker_index):
    # All workers share one authenticated YouTube IP/session.
    return "T2"


def load_proxy_session(sheets, sheet_id, sheet_name, worker_index):
    cell = proxy_session_cell(worker_index)
    values = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{sheet_name}'!{cell}",
    ).execute().get("values", [])
    if values and values[0] and values[0][0].strip():
        return values[0][0].strip()
    session = secrets.token_hex(8)
    save_proxy_session(sheets, sheet_id, sheet_name, worker_index, session)
    return session


def save_proxy_session(sheets, sheet_id, sheet_name, worker_index, session):
    row = 2
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!S{row}:T{row}",
        valueInputOption="RAW",
        body={"values": [["Shared proxy session", session]]},
    ).execute()


def upload_video(drive, path, folder_id, marker):
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True, chunksize=8 * 1024 * 1024)
    request = drive.files().create(
        body={
            "name": path.name,
            "parents": [folder_id],
            "appProperties": {"youtubeSourceHash": marker},
        },
        media_body=media,
        fields="id,name,webViewLink,size,parents",
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    if int(response.get("size", 0)) <= 0:
        raise RuntimeError("Drive upload returned an empty file")
    return response


def safe_folder_name(value, fallback):
    name = re.sub(r"[\\/:*?\"<>|]+", "-", str(value or "")).strip().strip(".")
    return name[:180] or fallback


def get_or_create_folder(drive, parent_id, name):
    name = safe_folder_name(name, "Chưa phân loại")
    escaped_name = name.replace("'", "\\'")
    escaped_parent = parent_id.replace("'", "\\'")
    result = drive.files().list(
        q=("trashed=false and mimeType='application/vnd.google-apps.folder' "
           f"and name='{escaped_name}' and '{escaped_parent}' in parents"),
        spaces="drive",
        fields="files(id,name)",
        pageSize=1,
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    created = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
    ).execute()
    return created["id"]


def resolve_destination_folder(drive, root_id, row):
    parent = root_id
    for name, fallback in ((row[1], "Chưa xác định môn"),
                           (row[2], "Chưa xác định thầy cô"),
                           (row[4], "Chưa xác định chương")):
        parent = get_or_create_folder(drive, parent, safe_folder_name(name, fallback))
    return parent


def main():
    sheet_id = required("YOUTUBE_SHEET_ID")
    sheet_name = os.environ.get("YOUTUBE_SHEET_NAME", "LINK YOUTUBE")
    worker_index = int(os.environ.get("WORKER_INDEX", "0"))
    worker_count = int(os.environ.get("WORKER_COUNT", "3"))
    max_videos = int(os.environ.get("MAX_VIDEOS", "10"))
    root_folder_id = required("YOUTUBE_ROOT_FOLDER_ID")

    creds = credentials()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!A1:Q1",
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()
    rows = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{sheet_name}'!A2:Q",
    ).execute().get("values", [])
    proxy_session = load_proxy_session(sheets, sheet_id, sheet_name, worker_index)

    def persist_proxy_session(session):
        nonlocal proxy_session
        proxy_session = session
        save_proxy_session(sheets, sheet_id, sheet_name, worker_index, session)

    candidates = []
    for offset, original in enumerate(rows, start=2):
        row = original + [""] * (len(HEADERS) - len(original))
        url = row[9].strip()
        status = row[11].strip().upper() or "PENDING"
        if not url or status == "ĐÃ XONG":
            continue
        folder_partition_key = "\u0001".join((row[1], row[2], row[4]))
        if partition(folder_partition_key, worker_count) != worker_index:
            continue
        try:
            previous_attempts = int(float(row[14] or 0))
        except (TypeError, ValueError):
            previous_attempts = 0
        candidates.append((previous_attempts, offset, row, url))

    # Scan every catalog entry before retrying older failures repeatedly.
    candidates.sort(key=lambda item: (item[0], item[1]))
    processed = 0
    attempted = 0
    for previous_attempts, offset, row, url in candidates[:max_videos]:
        attempts = previous_attempts + 1
        attempted += 1
        update_row(sheets, sheet_id, sheet_name, offset, {
            "L": "", "O": attempts, "P": "", "Q": utc_now(),
        })
        try:
            folder_id = resolve_destination_folder(drive, root_folder_id, row)
            marker = source_hash(f"{url}|{folder_id}")
            existing = find_existing_file(drive, marker)
            if existing:
                uploaded = existing
            else:
                proxy_session = load_proxy_session(
                    sheets, sheet_id, sheet_name, worker_index,
                )
                with tempfile.TemporaryDirectory(prefix="youtube-drive-") as temp:
                    path = download_video(
                        url, Path(temp), proxy_session=proxy_session,
                        on_proxy_rotate=persist_proxy_session,
                    )
                    uploaded = upload_video(drive, path, folder_id, marker)
            link = uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded['id']}/view"
            update_row(sheets, sheet_id, sheet_name, offset, {
                "L": "ĐÃ XONG", "M": link, "N": uploaded["id"], "P": "", "Q": utc_now(),
            })
            processed += 1
        except Exception as error:
            final_status = "" if attempts < 5 else "LỖI"
            update_row(sheets, sheet_id, sheet_name, offset, {
                "L": final_status, "P": safe_error(error, url), "Q": utc_now(),
            })
    print(json.dumps({"worker": worker_index, "attempted": attempted, "processed": processed}))


if __name__ == "__main__":
    main()
