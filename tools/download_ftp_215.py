import os
import sys
import json
from datetime import date, timedelta
from ftplib import FTP
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── KONFIGURASI DARI ENV ──────────────────────────────────────────────────────
FTP_HOST      = os.environ["FTP_HOST_215"]
FTP_PORT      = int(os.environ["FTP_PORT_215"])
FTP_USER      = os.environ["FTP_USER_215"]
FTP_PASS      = os.environ["FTP_PASS_215"]
FTP_REMOTE    = os.environ.get("FTP_REMOTE_215", "/")
DOWNLOAD_BASE = os.environ["DOWNLOAD_BASE_215"]
FILE_CODE     = os.environ.get("FILE_CODE_215", "12")
# ─────────────────────────────────────────────────────────────────────────────

FILE_TYPES = [
    "001","026","033","107","113","117",
    "137","143","146","157","165","174",
    "224","219","236","237","260","263","265",
    "272","273","295"
]

today      = date.today() - timedelta(days=1)  # H-1
TODAY_YY   = today.strftime("%y")
TODAY_MM   = today.strftime("%m")
TODAY_DD   = today.strftime("%d")
TODAY_STR  = f"{TODAY_YY}{TODAY_MM}{TODAY_DD}"
TODAY_FULL = today.strftime("%Y%m%d")
DOWNLOAD_DIR = os.path.join(DOWNLOAD_BASE, TODAY_FULL)

EXPECTED_FILES = [f"BK{TODAY_STR}{FILE_CODE}.{ext}" for ext in FILE_TYPES]


def download_ftp():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print(f"[INFO] Folder download (H-1): {DOWNLOAD_DIR}", file=sys.stderr)
    print(f"[INFO] Konek ke FTP {FTP_HOST}:{FTP_PORT}...", file=sys.stderr)

    ftp = FTP()
    ftp.connect(FTP_HOST, FTP_PORT)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    if FTP_REMOTE != "/":
        ftp.cwd(FTP_REMOTE)

    all_files = ftp.nlst()
    print(f"[INFO] Total file di FTP: {len(all_files)}", file=sys.stderr)

    all_files_upper = {f.upper(): f for f in all_files}
    downloaded = []
    not_found  = []

    for expected in EXPECTED_FILES:
        match = all_files_upper.get(expected.upper())
        if match:
            local_path = os.path.join(DOWNLOAD_DIR, expected)
            with open(local_path, "wb") as f:
                ftp.retrbinary(f"RETR {match}", f.write)
            downloaded.append(local_path)
            print(f"[DL] ✓ {expected}", file=sys.stderr)
        else:
            not_found.append(expected)
            print(f"[SKIP] {expected} tidak ada di FTP", file=sys.stderr)

    ftp.quit()
    print(json.dumps({
        "today": TODAY_FULL,
        "download_dir": os.path.abspath(DOWNLOAD_DIR),
        "total_ftp": len(all_files),
        "downloaded": downloaded,
        "not_found": not_found
    }))


if __name__ == "__main__":
    download_ftp()
