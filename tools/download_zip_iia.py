import os
import sys
import json
import requests
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv
from html.parser import HTMLParser

load_dotenv(Path(__file__).parent / ".env")

# ── KONFIGURASI DARI ENV ──────────────────────────────────────────────────────
BASE_URL   = os.environ["IIA_BASE_URL"]
LOGIN_URL  = os.environ["IIA_LOGIN_URL"]
USERNAME   = os.environ["IIA_USERNAME"]
PASSWORD   = os.environ["IIA_PASSWORD"]
DATE1      = os.environ["IIA_DATE1"]
CLASS_ID   = os.environ["IIA_CLASS_ID"]
RESP_ID    = os.environ["IIA_RESP_ID"]
ZIP_DIR    = os.environ["ZIP_DIR"]
# ─────────────────────────────────────────────────────────────────────────────

# H-2 format: 24-March-2026
h2        = date.today() - timedelta(days=1)
DATE2_STR = str(h2.day) + h2.strftime("-%B-%Y")  # cross-platform, no padding


class TokenParser(HTMLParser):
    """Ambil __RequestVerificationToken dari form login."""
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            attrs_dict = dict(attrs)
            if attrs_dict.get("name") == "__RequestVerificationToken":
                self.token = attrs_dict.get("value")


def get_csrf_token(session, url):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    parser = TokenParser()
    parser.feed(resp.text)
    if not parser.token:
        raise ValueError("CSRF token tidak ditemukan di halaman login!")
    return parser.token


def login(session):
    print(f"[INFO] Ambil CSRF token dari {LOGIN_URL}...", file=sys.stderr)
    token = get_csrf_token(session, LOGIN_URL)

    print(f"[INFO] Login sebagai {USERNAME}...", file=sys.stderr)
    payload = {
        "__RequestVerificationToken": token,
        "Username": USERNAME,
        "Password": PASSWORD,
        "RememberMe": "false",
    }
    resp = session.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)

    # Cek login berhasil: kalau masih di halaman login berarti gagal
    if "Login" in resp.url or "login" in resp.url:
        raise ValueError(f"Login gagal! Cek username/password di .env")

    print(f"[INFO] Login berhasil!", file=sys.stderr)


def cleanup_zip_dir():
    """Hapus semua ZIP lama di folder upload sebelum download baru."""
    if not os.path.exists(ZIP_DIR):
        return
    deleted = 0
    for f in os.listdir(ZIP_DIR):
        if f.upper().endswith(".ZIP"):
            os.remove(os.path.join(ZIP_DIR, f))
            print(f"[CLEANUP] Dihapus: {f}", file=sys.stderr)
            deleted += 1
    print(f"[CLEANUP] {deleted} file ZIP lama dihapus", file=sys.stderr)


def download_zip(session):
    os.makedirs(ZIP_DIR, exist_ok=True)

    url = (
        f"{BASE_URL}/deliveryOrders/downloadNPDInTransit2"
        f"?_date1={DATE1}&_date2={DATE2_STR}"
        f"&_classId={CLASS_ID}&_respId={RESP_ID}"
    )
    print(f"[INFO] Download ZIP dari: {url}", file=sys.stderr)

    resp = session.get(url, stream=True, timeout=120)

    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code} saat download ZIP")

    # Nama file dari header atau fallback
    content_disp = resp.headers.get("Content-Disposition", "")
    if "filename=" in content_disp:
        zip_name = content_disp.split("filename=")[-1].strip().strip('"')
    else:
        zip_name = f"INTBK20260101-{h2.strftime('%Y%m%d')}.zip"

    zip_path = os.path.join(ZIP_DIR, zip_name)

    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    size_kb = os.path.getsize(zip_path) / 1024
    print(f"[OK] ZIP tersimpan: {zip_path} ({size_kb:.1f} KB)", file=sys.stderr)
    return zip_path, zip_name, size_kb


if __name__ == "__main__":
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        cleanup_zip_dir()
        login(session)
        zip_path, zip_name, size_kb = download_zip(session)

        print(json.dumps({
            "date2": DATE2_STR,
            "zip_path": zip_path,
            "zip_name": zip_name,
            "size_kb": round(size_kb, 1)
        }))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
