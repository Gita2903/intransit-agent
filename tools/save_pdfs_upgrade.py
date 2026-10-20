import os
import sys
import io
import re
import unicodedata
import urllib.parse

import requests
from PyPDF2 import PdfReader
from pathlib import Path
from dotenv import load_dotenv
from html.parser import HTMLParser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv(Path(__file__).parent / ".env")

# ============================================================
# Baca noNota dari file .txt (satu noNota per baris)
# Format noNota: YYMMDDXXXX  (contoh: 2601070100)
#   YY   = tahun (26 → 2026)
#   MM   = bulan (01 → January)
#   DD   = tanggal (07)
#   XXXX = nomor SJ
#
# Usage:
#   python save_pdfs_upgrade.py            → baca dari "sosis-happy.txt" (default)
#   python save_pdfs_upgrade.py batch.txt  → baca dari file lain
# Baris kosong & baris diawali '#' diabaikan
# ============================================================

# ── Konfigurasi dari ENV ───────────────────────────────────
BASE_URL   = os.environ["IIA_BASE_URL"]
LOGIN_URL  = os.environ["IIA_LOGIN_URL"]
USERNAME   = os.environ["IIA_USERNAME"]
PASSWORD   = os.environ["IIA_PASSWORD"]
PRINT_URL  = f"{BASE_URL}/deliveryorders/printDeliveryOrders"

MONTH_MAP = {
    "01": "January", "02": "February", "03": "March",
    "04": "April",   "05": "May",      "06": "June",
    "07": "July",    "08": "August",   "09": "September",
    "10": "October", "11": "November", "12": "December"
}


class TokenParser(HTMLParser):
    """Ambil __RequestVerificationToken dari form login."""
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs)
            if d.get("name") == "__RequestVerificationToken":
                self.token = d.get("value")


def create_authenticated_session() -> requests.Session:
    """Login ke IIAWeb pakai CSRF token, return session yang sudah authenticated."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    resp = session.get(LOGIN_URL, timeout=30)
    resp.raise_for_status()
    parser = TokenParser()
    parser.feed(resp.text)
    if not parser.token:
        raise ValueError("CSRF token tidak ditemukan di halaman login IIA!")

    login_resp = session.post(
        LOGIN_URL,
        data={
            "__RequestVerificationToken": parser.token,
            "Username": USERNAME,
            "Password": PASSWORD,
            "RememberMe": "false",
        },
        timeout=30,
        allow_redirects=True,
    )
    if "login" in login_resp.url.lower():
        raise ValueError("Login gagal! Cek IIA_USERNAME/IIA_PASSWORD di .env")

    return session


def nota_to_url(nota):
    nota = nota.strip()
    if len(nota) < 8:
        raise ValueError(f"noNota '{nota}' terlalu pendek, minimal 8 karakter")
    yy    = nota[0:2]   # 26
    mm    = nota[2:4]   # 01
    dd    = nota[4:6]   # 07
    year  = f"20{yy}"
    month = MONTH_MAP.get(mm)
    if not month:
        raise ValueError(f"Bulan '{mm}' ga valid di noNota '{nota}'")
    date_str = f"{dd}-{month}-{year}"
    return f"{PRINT_URL}?_date={date_str}&_type=QR&_noNota={nota}"


# ── PDF Extractor ──────────────────────────────────────────
def extract_store_name_from_pdf(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"

        lines = text.split('\n')

        pattern1 = re.search(r"Kepada\s*:\s*\n\s*:\s*\n\s*:\s*[^\n]*\n\s*([A-Za-z0-9][^\n]+)", text, re.IGNORECASE | re.MULTILINE)
        if pattern1:
            store_name = pattern1.group(1).strip()
            if not re.match(r'^\d{2}-\w+-\d{4}$|^Surat Jalan', store_name):
                return store_name

        pattern2 = re.search(r"Kepada\s*:\s*(.+)", text, re.IGNORECASE)
        if pattern2:
            line = pattern2.group(1).splitlines()[0].strip()
            if line and line not in [':']:
                return line

        pattern3 = re.search(r"([A-Z0-9]{2,}\s*-\s*[A-Za-z0-9\s]+)", text)
        if pattern3:
            return pattern3.group(1).strip()

        for i, line in enumerate(lines):
            if 'Kepada' in line and ':' in line:
                for j in range(i+1, min(i+5, len(lines))):
                    candidate = lines[j].strip()
                    if (candidate and candidate != ':' and len(candidate) > 3 and
                        not re.match(r'^\d{2}-\w+-\d{4}$', candidate) and
                        not re.match(r'^Surat Jalan', candidate) and
                        not re.match(r'^No\s+Nama', candidate)):
                        return candidate

        return None

    except Exception as e:
        print(f"⚠️ Error saat ekstrak nama toko: {e}")
        return None


def sanitize_filename(name):
    if not name:
        return "unnamed_store"
    name = name.strip()
    nkfd = unicodedata.normalize('NFKD', name)
    ascii_name = nkfd.encode('ascii', 'ignore').decode('ascii')
    ascii_name = re.sub(r"[^A-Za-z0-9 _\-\.]+", "", ascii_name)
    ascii_name = re.sub(r"\s+", "_", ascii_name)
    ascii_name = ascii_name.strip("._-")
    if not ascii_name:
        ascii_name = "unnamed_store"
    return ascii_name[:100]


# ── Main ───────────────────────────────────────────────────
if __name__ == "__main__":
    url_file = sys.argv[1] if len(sys.argv) > 1 else "sosis-happy.txt"

    if not os.path.exists(url_file):
        print(f"❌ File '{url_file}' ga ketemu!")
        sys.exit(1)

    with open(url_file, "r") as f:
        raw_lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    urls = []
    for line in raw_lines:
        try:
            urls.append(nota_to_url(line))
        except ValueError as e:
            print(f"⚠️ Skip baris invalid: {e}")

    print(f"📋 Loaded {len(urls)} URL dari '{url_file}'\n")

    session = create_authenticated_session()
    print("🚀 Login berhasil, mulai download PDF dan auto-naming...\n")

    for i, url in enumerate(urls, start=1):
        print(f"[{i}/{len(urls)}] Downloading dari: {url}")
        try:
            response = session.get(url, verify=False, timeout=60)
            if response.status_code == 200:
                print("📄 PDF berhasil didownload, ekstrak nama toko...")
                store_name = extract_store_name_from_pdf(response.content)
                if store_name:
                    clean_store_name = sanitize_filename(store_name)
                    parsed = urllib.parse.urlparse(url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    date_param = qs.get("_date", [""])[0]
                    nota_param = qs.get("_noNota", [""])[0]
                    filename = f"{clean_store_name}_{date_param}_{nota_param}.pdf"
                    print(f"📝 Nama toko ditemukan: '{store_name}'")
                    print(f"📁 Filename: {filename}")
                else:
                    parsed = urllib.parse.urlparse(url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    nota = qs.get("_noNota", [f"unknown_{i}"])[0]
                    filename = f"surat_jalan_{nota}.pdf"
                    print(f"⚠️ Nama toko ga ketemu, pake fallback: {filename}")
                with open(filename, "wb") as f:
                    f.write(response.content)
                print(f"✅ File saved: {filename}")
            else:
                print(f"❌ Download failed - HTTP {response.status_code}")
        except Exception as e:
            print(f"💥 Error pas download {url}: {e}")
        print("-" * 60)

    print("\n🎉 Selesai! Semua PDF udah dicoba didownload dengan auto-naming.")
