import zipfile
import os
import re
import sys
import json
from datetime import date
from ftplib import FTP
from pathlib import Path
from dotenv import load_dotenv

# Load .env dari folder yang sama dengan script
load_dotenv(Path(__file__).parent / ".env")

# ── KONFIGURASI DARI ENV ──────────────────────────────────────────────────────
ZIP_DIR     = os.environ["ZIP_DIR"]
ZIP_PREFIX  = os.environ["ZIP_PREFIX"]
OUTPUT_BASE = os.environ["OUTPUT_BASE"]
FTP_HOST    = os.environ["FTP_HOST"]
FTP_PORT    = int(os.environ["FTP_PORT"])
FTP_USER    = os.environ["FTP_USER"]
FTP_PASS    = os.environ["FTP_PASS"]
FTP_REMOTE  = os.environ.get("FTP_REMOTE", "/")
# ─────────────────────────────────────────────────────────────────────────────

TODAY_FULL = date.today().strftime("%Y%m%d")
TODAY      = TODAY_FULL
PATTERN    = re.compile(r"(\d{8})(\.csv)$", re.IGNORECASE)
OUTPUT_DIR = os.path.join(OUTPUT_BASE, TODAY_FULL)


def find_latest_zip(zip_dir, prefix):
    candidates = [
        f for f in os.listdir(zip_dir)
        if f.upper().startswith(prefix.upper()) and f.upper().endswith(".ZIP")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda f: os.path.getmtime(os.path.join(zip_dir, f)), reverse=True)
    return os.path.join(zip_dir, candidates[0])


def extract_and_rename(zip_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    print(f"[INFO] Folder output: {output_dir}", file=sys.stderr)
    renamed_files = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        print(f"[INFO] {len(namelist)} file ditemukan di ZIP: {zip_path}", file=sys.stderr)

        for name in namelist:
            zf.extract(name, output_dir)
            old_path = os.path.join(output_dir, name)
            match = PATTERN.search(name)
            if match:
                old_date = match.group(1)
                new_name = PATTERN.sub(f"{TODAY}\\2", name)
                new_path = os.path.join(output_dir, new_name)
                if os.path.exists(new_path):
                    os.remove(new_path)
                os.rename(old_path, new_path)
                renamed_files.append(os.path.abspath(new_path))
                print(f"[OK] {name} → {new_name}  [{old_date} → {TODAY}]", file=sys.stderr)
            else:
                print(f"[SKIP] {name} (pola tanggal tidak ditemukan)", file=sys.stderr)

    return renamed_files


def upload_ftp(files):
    print(f"[INFO] Konek ke FTP {FTP_HOST}:{FTP_PORT}...", file=sys.stderr)
    ftp = FTP()
    ftp.connect(FTP_HOST, FTP_PORT)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    if FTP_REMOTE != "/":
        ftp.cwd(FTP_REMOTE)
    for file_path in files:
        file_name = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            ftp.storbinary(f"STOR {file_name}", f)
        print(f"[FTP] ✓ {file_name}", file=sys.stderr)
    ftp.quit()
    print(f"[INFO] Semua file berhasil diupload!", file=sys.stderr)


if __name__ == "__main__":
    zip_path = find_latest_zip(ZIP_DIR, ZIP_PREFIX)
    if not zip_path:
        print(json.dumps({"error": f"Tidak ada ZIP dengan prefix '{ZIP_PREFIX}' di: {ZIP_DIR}", "files": []}))
        sys.exit(1)

    print(f"[INFO] ZIP ditemukan: {zip_path}", file=sys.stderr)
    renamed_files = extract_and_rename(zip_path, OUTPUT_DIR)
    upload_ftp(renamed_files)

    print(json.dumps({
        "today": TODAY_FULL,
        "zip_used": zip_path,
        "output_dir": os.path.abspath(OUTPUT_DIR),
        "files": renamed_files,
        "uploaded": len(renamed_files)
    }))
