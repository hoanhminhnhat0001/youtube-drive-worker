import hashlib
import json
import mimetypes
import os
import re
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
    return re.sub(r"\s+", " ", text).strip()[:1000]


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


def download_video(url, directory):
    output = str(directory / "%(title).160B [%(id)s].%(ext)s")
    command = [
        "yt-dlp", "--no-playlist", "--newline", "--restrict-filenames",
        "--retries", "5", "--fragment-retries", "5",
        "--sleep-requests", "1", "--sleep-interval", "2", "--max-sleep-interval", "8",
        "--merge-output-format", "mp4",
        "-f", "bestvideo+bestaudio/best",
        "--print", "after_move:filepath", "-o", output, url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or "yt-dlp failed")
    candidates = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    files = [path for path in candidates if path.exists()]
    if not files:
        files = sorted(directory.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("yt-dlp completed without a media file")
    return files[0]


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

    processed = 0
    attempted = 0
    for offset, original in enumerate(rows, start=2):
        row = original + [""] * (len(HEADERS) - len(original))
        url = row[9].strip()
        status = row[11].strip().upper() or "PENDING"
        if not url or status in {"ĐÃ XONG", "LỖI"}:
            continue
        folder_partition_key = "\u0001".join((row[1], row[2], row[4]))
        if partition(folder_partition_key, worker_count) != worker_index:
            continue
        try:
            attempts = int(float(row[14] or 0)) + 1
        except (TypeError, ValueError):
            attempts = 1
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
                with tempfile.TemporaryDirectory(prefix="youtube-drive-") as temp:
                    path = download_video(url, Path(temp))
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
        if attempted >= max_videos:
            break
    print(json.dumps({"worker": worker_index, "attempted": attempted, "processed": processed}))


if __name__ == "__main__":
    main()
