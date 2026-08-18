import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import imageio_ffmpeg
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
VPNBOOK_SERVERS = [
    "us16.vpnbook.com", "us178.vpnbook.com", "ca149.vpnbook.com",
    "ca196.vpnbook.com", "uk205.vpnbook.com", "uk68.vpnbook.com",
    "de20.vpnbook.com", "de220.vpnbook.com", "fr200.vpnbook.com",
    "fr2311.vpnbook.com",
]


class VpnBookManager:
    def __init__(self, sheets, sheet_id, sheet_name):
        self.sheets = sheets
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self.index = self._load_index()
        self.process = None
        self.source_address = ""
        self.temp_dir = Path(tempfile.mkdtemp(prefix="vpnbook-"))

    def _load_index(self):
        values = self.sheets.spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=f"'{self.sheet_name}'!V2",
        ).execute().get("values", [])
        try:
            return int(values[0][0]) % len(VPNBOOK_SERVERS)
        except (IndexError, TypeError, ValueError):
            return 0

    def _save_index(self):
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'{self.sheet_name}'!U2:V2",
            valueInputOption="RAW",
            body={"values": [["VPNBook server index", self.index]]},
        ).execute()

    @staticmethod
    def _fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def _credentials(self):
        html = self._fetch("https://www.vpnbook.com/freevpn/openvpn").decode("utf-8", "replace")
        match = re.search(r"Password\s*</[^>]+>\s*<code[^>]*>([^<]+)</code>", html, re.I)
        if not match:
            match = re.search(r"Password.{0,500}?<code[^>]*>([^<]+)</code>", html, re.I | re.S)
        if not match:
            raise RuntimeError("VPNBook password was not found on the official page")
        return "vpnbook", match.group(1).strip()

    def connect(self):
        self.disconnect()
        server = VPNBOOK_SERVERS[self.index]
        ip = socket.gethostbyname(server)
        query = urllib.parse.urlencode({"hostname": server, "protocol": "udp25000", "ip": ip})
        config = self._fetch(f"https://www.vpnbook.com/api/openvpn?{query}")
        config_path = self.temp_dir / "vpnbook.ovpn"
        auth_path = self.temp_dir / "auth.txt"
        log_path = self.temp_dir / "openvpn.log"
        config_path.write_bytes(config)
        username, password = self._credentials()
        auth_path.write_text(f"{username}\n{password}\n", encoding="utf-8")
        # OpenVPN runs as root, so create a runner-readable log before starting it.
        log_path.touch()
        log_path.chmod(0o666)
        command = [
            "sudo", "openvpn", "--config", str(config_path),
            "--auth-user-pass", str(auth_path), "--auth-nocache",
            "--route-nopull",
            "--writepid", str(self.temp_dir / "openvpn.pid"),
            "--log", str(log_path),
        ]
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.process.poll() is not None:
                break
            if log_path.exists() and "Initialization Sequence Completed" in log_path.read_text(errors="replace"):
                address = subprocess.run(
                    ["bash", "-lc", "ip -o -4 addr show | awk '$2 ~ /^tun/ {print $4; exit}' | cut -d/ -f1"],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                interface = subprocess.run(
                    ["bash", "-lc", "ip -o -4 addr show | awk '$2 ~ /^tun/ {print $2; exit}'"],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                if not address or not interface:
                    raise RuntimeError("VPNBook connected without a tunnel address")
                subprocess.run(["sudo", "ip", "rule", "add", "from", address, "table", "200"], check=False)
                subprocess.run(["sudo", "ip", "route", "replace", "default", "dev", interface, "table", "200"], check=True)
                self.source_address = address
                return server
            time.sleep(1)
        tail = log_path.read_text(errors="replace")[-1000:] if log_path.exists() else ""
        self.disconnect()
        raise RuntimeError(f"VPNBook failed to connect: {tail}")

    def disconnect(self):
        if self.source_address:
            subprocess.run(
                ["sudo", "ip", "rule", "del", "from", self.source_address, "table", "200"],
                check=False,
            )
            subprocess.run(["sudo", "ip", "route", "flush", "table", "200"], check=False)
            self.source_address = ""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def rotate(self):
        self.index = (self.index + 1) % len(VPNBOOK_SERVERS)
        self._save_index()
        return self.connect()


class ProtonVpnManager:
    def __init__(self):
        self.process = None
        self.source_address = ""
        self.temp_dir = Path(tempfile.mkdtemp(prefix="protonvpn-"))

    def connect(self):
        self.disconnect()
        config_path = self.temp_dir / "proton.ovpn"
        auth_path = self.temp_dir / "auth.txt"
        log_path = self.temp_dir / "openvpn.log"
        try:
            config_path.write_bytes(base64.b64decode(required("PROTON_OPENVPN_CONFIG_B64"), validate=True))
        except (ValueError, base64.binascii.Error) as error:
            raise RuntimeError("PROTON_OPENVPN_CONFIG_B64 is invalid") from error
        auth_path.write_text(
            f"{required('PROTON_OPENVPN_USERNAME')}\n{required('PROTON_OPENVPN_PASSWORD')}\n",
            encoding="utf-8",
        )
        auth_path.chmod(0o600)
        log_path.touch()
        log_path.chmod(0o666)
        command = [
            "sudo", "openvpn", "--config", str(config_path),
            "--auth-user-pass", str(auth_path), "--auth-nocache",
            "--route-nopull", "--dev", "proton0", "--dev-type", "tun",
            "--log", str(log_path),
        ]
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.process.poll() is not None:
                break
            log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
            if "Initialization Sequence Completed" in log_text:
                address = subprocess.run(
                    ["bash", "-lc", "ip -o -4 addr show dev proton0 | awk '{print $4; exit}' | cut -d/ -f1"],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                if not address:
                    time.sleep(1)
                    continue
                subprocess.run(["sudo", "ip", "rule", "add", "from", address, "table", "200"], check=False)
                subprocess.run(["sudo", "ip", "route", "replace", "default", "dev", "proton0", "table", "200"], check=True)
                self.source_address = address
                return "US-FREE#119"
            time.sleep(1)
        tail = log_path.read_text(errors="replace")[-1200:] if log_path.exists() else ""
        self.disconnect()
        raise RuntimeError(f"Proton VPN failed to connect: {tail}")

    def disconnect(self):
        if self.source_address:
            subprocess.run(["sudo", "ip", "rule", "del", "from", self.source_address, "table", "200"], check=False)
            subprocess.run(["sudo", "ip", "route", "flush", "table", "200"], check=False)
            self.source_address = ""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def rotate(self):
        # This free endpoint can authenticate successfully while its public IP is
        # blocked by YouTube. Fall back to the runner network for this job.
        self.disconnect()
        print(json.dumps({
            "network": "proton",
            "status": "blocked",
            "action": "fallback_to_github_runner",
        }), flush=True)
        return "direct"


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
    last_error = None
    for attempt in range(5):
        try:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()
            return
        except Exception as error:
            last_error = error
            if attempt < 4:
                time.sleep(2 ** attempt)
    raise last_error


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


def download_video(url, directory, proxy_session="", on_network_rotate=None,
                   source_address_provider=None):
    output = str(directory / "%(title).160B [%(id)s].%(ext)s")
    command = [
        "yt-dlp", "--no-playlist", "--newline", "--restrict-filenames",
        "--ffmpeg-location", imageio_ffmpeg.get_ffmpeg_exe(),
        "--socket-timeout", "12", "--retries", "2", "--fragment-retries", "2",
        "--extractor-retries", "2", "--file-access-retries", "2",
        "--retry-sleep", "2", "--sleep-requests", "1",
        "--sleep-interval", "1", "--max-sleep-interval", "3",
        "--js-runtimes", "deno",
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
    proxy_attempts = 3 if (proxy_template and "{session}" in proxy_template) or on_network_rotate else 1
    for attempt_index in range(proxy_attempts):
        attempt_command = list(command)
        if source_address_provider:
            source_address = source_address_provider()
            if source_address:
                attempt_command[1:1] = ["--source-address", source_address]
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
            if proxy_template:
                active_session = secrets.token_hex(8)
            if on_network_rotate:
                on_network_rotate(active_session if proxy_template else None)
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
        status, response = request.next_chunk()
        if status:
            uploaded_mb = status.resumable_progress / (1024 * 1024)
            total_mb = status.total_size / (1024 * 1024) if status.total_size else 0
            print(json.dumps({
                "status": "uploading",
                "file": path.name,
                "uploaded_mb": round(uploaded_mb, 1),
                "total_mb": round(total_mb, 1),
                "percent": round(status.progress() * 100, 1),
            }, ensure_ascii=False), flush=True)
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
    ignore_previous_errors = os.environ.get("IGNORE_PREVIOUS_ERRORS", "1").strip() == "1"
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
    vpnbook = None
    if os.environ.get("PROTON_OPENVPN_CONFIG_B64", "").strip():
        vpnbook = ProtonVpnManager()
        vpnbook.connect()
    elif os.environ.get("VPNBOOK_ENABLED", "").strip() == "1":
        vpnbook = VpnBookManager(sheets, sheet_id, sheet_name)
        vpnbook.connect()

    def persist_proxy_session(session):
        nonlocal proxy_session
        proxy_session = session
        save_proxy_session(sheets, sheet_id, sheet_name, worker_index, session)

    def rotate_network(session):
        if session:
            persist_proxy_session(session)
        if vpnbook:
            vpnbook.rotate()

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
        if ignore_previous_errors:
            previous_attempts = 0
        candidates.append((previous_attempts, offset, row, url))

    # Always process the sheet from top to bottom, regardless of old failures.
    candidates.sort(key=lambda item: item[1])
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
                        on_network_rotate=rotate_network if vpnbook or os.environ.get("YOUTUBE_PROXY") else None,
                        source_address_provider=(lambda: vpnbook.source_address) if vpnbook else None,
                    )
                    uploaded = upload_video(drive, path, folder_id, marker)
            link = uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded['id']}/view"
            update_row(sheets, sheet_id, sheet_name, offset, {
                "L": "ĐÃ XONG", "M": link, "N": uploaded["id"], "P": "", "Q": utc_now(),
            })
            processed += 1
        except Exception as error:
            final_status = "" if ignore_previous_errors or attempts < 5 else "LỖI"
            error_message = safe_error(error, url)
            print(json.dumps({
                "worker": worker_index,
                "row": offset,
                "status": "failed",
                "error": error_message,
            }, ensure_ascii=False), flush=True)
            update_row(sheets, sheet_id, sheet_name, offset, {
                "L": final_status, "P": error_message, "Q": utc_now(),
            })
    print(json.dumps({"worker": worker_index, "attempted": attempted, "processed": processed}))
    if vpnbook:
        vpnbook.disconnect()


if __name__ == "__main__":
    main()
