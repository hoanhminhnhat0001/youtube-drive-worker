# YouTube to Drive worker

This project reads authorized YouTube URLs from the Google Sheet catalog, downloads a small batch with `yt-dlp`, uploads each file to its exact Google Drive destination folder, and writes the result back to the sheet.

## Safety properties

- Three deterministic workers partition work by subject, teacher and chapter, preventing duplicate folder trees.
- Uploads use resumable Google Drive API requests within each running job.
- A SHA-256 marker over URL and destination folder prevents incorrect cross-folder deduplication.
- Files are deleted from the runner immediately after each upload.
- YouTube URLs are redacted from stored error messages.
- Videos are stored under one root folder as `Môn học / Thầy cô / Chương`.
- A workflow run is capped below GitHub's six-hour job limit.

## Required GitHub Actions secrets

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `YOUTUBE_SHEET_ID`
- `YOUTUBE_ROOT_FOLDER_ID`

The OAuth account must be the intended owner of uploaded files and must have edit access to the catalog spreadsheet.
For a personal Gmail account, publish the OAuth consent screen to Production; Testing refresh tokens can expire after seven days.

GitHub can disable schedules in an inactive public repository after 60 days. This transfer is expected to finish well before then; use `workflow_dispatch` to restart it if necessary.

## Local verification

Copy `.env.example` to `.env`, fill it locally, install the requirements, and run `python worker.py`. Start with `MAX_VIDEOS=1`.

Only download and store videos you are authorized to archive.
