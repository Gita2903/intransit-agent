import os
import sys
import json
from datetime import date
from ftplib import FTP
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── KONFIGURASI DARI ENV ──────────────────────────────────────────────────────
FTP_HOST      = os.environ["FTP_HOST"]
FTP_PORT      = int(os.environ["FTP_PORT"])
FTP_USER      = os.environ["FTP_USER"]
FTP_PASS      = os.environ["FTP_PASS"]
FTP_REMOTE    = os.environ.get("FTP_REMOTE", "/")
DOWNLOAD_BASE = os.environ["DOWNLOAD_BASE"]
BRANCH_CODE   = os.environ.get("BRANCH_CODE", "99")
# ─────────────────────────────────────────────────────────────────────────────

FILE_TYPE_MAP = {
    "001": "SAX2,SLIH,SNH1",  # ← Multi supplier (comma-separated)
    "026": "SAX2,SLIH,SNH1",
    "033": "SAX2,SLIH,SNH1",
    "107": "SAX2,SLIH,SNH1", 
    "113": "SAX2,SLIH,SNH1", 
    "117": "SAX2,SLIH,SNH1",
    "137": "SAX2,SLIH,SNH1", 
    "143": "SHZK",
    "146": "SLG6",
    "157": "SAX2,SLIH,SNH1",
    "165": "SFYR", 
    "174": "SFCQ", 
    "236": "SJ9T",
    "224": "SLG7",
    "219": "SFXC",
    "237": "SJ9S", 
    "260": "SFYR", 
    "263": "SLMU",
    "265": "SMC0",
    "272": "SMC1",
    "273": "SJ9T",
    "295": "SAX2,SLIH,SNH1",
}

today      = date.today()
TODAY_YY   = today.strftime("%y")
TODAY_MM   = today.strftime("%m")
TODAY_DD   = today.strftime("%d")
TODAY_STR  = f"{TODAY_YY}{TODAY_MM}{TODAY_DD}"
TODAY_FULL = today.strftime("%Y%m%d")
DOWNLOAD_DIR = os.path.join(DOWNLOAD_BASE, TODAY_FULL)

EXPECTED_FILES = {
    f"BK{TODAY_STR}{BRANCH_CODE}.{ext}": (ext, toko)
    for ext, toko in FILE_TYPE_MAP.items()
}


def filter_file(raw_bytes, filename, filter_toko):
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = raw_bytes.decode("latin-1")

    lines = content.splitlines()
    if not lines:
        return raw_bytes

    header  = lines[0]
    columns = header.split("|")
    try:
        toko_idx = columns.index("TOKO")
    except ValueError:
        print(f"[WARN] Kolom TOKO tidak ditemukan di {filename}", file=sys.stderr)
        return raw_bytes

    # Parse multiple suppliers (split by comma)
    allowed_tokos = [t.strip().upper() for t in filter_toko.split(",")]
    
    kept    = [header]
    removed = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("|")
        # Fixed: >= instead of >
        if len(cols) >= toko_idx + 1 and cols[toko_idx].strip().upper() in allowed_tokos:
            kept.append(line)
        else:
            removed += 1

    toko_label = "/".join(allowed_tokos)  # Display as "SAX2/SLIH"
    print(f"[FILTER] {filename}: {len(kept)-1} baris [{toko_label}] disimpan, {removed} dibuang", file=sys.stderr)
    return "\n".join(kept).encode("utf-8")


def download_ftp():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print(f"[INFO] Folder download: {DOWNLOAD_DIR}", file=sys.stderr)
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

    for expected, (ext, filter_toko) in EXPECTED_FILES.items():
        match = all_files_upper.get(expected.upper())
        if match:
            buf = bytearray()
            ftp.retrbinary(f"RETR {match}", buf.extend)
            filtered   = filter_file(bytes(buf), expected, filter_toko)
            local_path = os.path.join(DOWNLOAD_DIR, expected)
            with open(local_path, "wb") as f:
                f.write(filtered)
            downloaded.append(local_path)
            
            # Show supplier(s) in log
            suppliers = "/".join([t.strip() for t in filter_toko.split(",")])
            print(f"[DL] ✓ {expected} → filter TOKO=[{suppliers}]", file=sys.stderr)
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
