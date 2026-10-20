import os
import sys
import io
import json
import requests
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
from html.parser import HTMLParser
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

load_dotenv(Path(__file__).parent / ".env")

# ── KONFIGURASI ───────────────────────────────────────────────────────────────
BASE_URL  = os.environ["IIA_BASE_URL"]
LOGIN_URL = os.environ["IIA_LOGIN_URL"]
USERNAME  = os.environ["IIA_USERNAME"]
PASSWORD  = os.environ["IIA_PASSWORD"]
RESP_ID   = os.environ.get("IIA_RESP_ID", "91")

today      = date.today()
h2         = today - timedelta(days=1)
start_date = date(2026, 1, 1)

MONTHS = ["January","February","March","April","May","June",
          "July","August","September","October","November","December"]

def fmt_date(d):
    return f"{d.day}-{d.strftime('%B-%Y')}"

START_DATE_STR = fmt_date(start_date)
END_DATE_STR   = fmt_date(h2)

# ── CABANG MASTER ─────────────────────────────────────────────────────────────
CABANG_MASTER = [
    ("G001","Jakarta 1 (G001)"),   ("G026","Tangerang 1 (G026)"),
    ("G033","Tangerang 2 (G033)"), ("G107","Parung (G107)"),
    ("G113","Bogor 1 (G113)"),     ("G117","Bogor 2 (G117)"),
    ("G137","Jakarta 2 (G137)"),   ("G143","Jambi (G143)"),
    ("G146","Lombok (G146)"),      ("G157","Lebak (G157)"),
    ("G165","Bengkulu (G165)"),    ("G174","Ternate (G174)"),
    ("G219","Bintan (G219)"),
    ("G224","Bima (G224)"),
    ("G236","Sorong (G236)"),      ("G237","Kupang (G237)"),
    ("G260","Bangka Belitung (G260)"), ("G263","Jayapura (G263)"),
    ("G265","Flores (G265)"),      ("G272","Sumba (G272)"),
    ("G273","Manokwari (G273)"),   ("G283","Merauke (G283)"),
    ("G295","Sukabumi (G295)"),    ("G298","Alor (G298)"),      
]
MASTER_KDCAB = {k for k,_ in CABANG_MASTER}

# ── STYLES ────────────────────────────────────────────────────────────────────
FONT_NAME  = "Calibri"
CLR_HEADER = "1B2A4A"   # deep navy
CLR_SUBHDR = "2C3E6B"   # medium navy
CLR_BULAN  = "D4DFEF"   # soft periwinkle
CLR_TOTAL  = "E8ECF4"   # light lavender-grey
CLR_ALT    = "F4F6FA"   # very light grey-blue
CLR_ROW    = "FFFFFF"   # white (normal rows)
CLR_BORDER = "C5CCD6"   # muted border

def sc(cell, bold=False, bg=None, fg="000000", align="center", size=10, wrap=False):
    cell.font      = Font(name=FONT_NAME, bold=bold, color=fg, size=size)
    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    if bg:
        cell.fill = PatternFill("solid", start_color=bg)

def thin_border():
    s = Side(style="thin", color=CLR_BORDER)
    return Border(left=s, right=s, top=s, bottom=s)

def apply_border(ws, row, c1, c2):
    for c in range(c1, c2+1):
        ws.cell(row=row, column=c).border = thin_border()

# ── LOGIN ─────────────────────────────────────────────────────────────────────
class TokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None
    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs)
            if d.get("name") == "__RequestVerificationToken":
                self.token = d.get("value")

def login(session):
    print("[INFO] Ambil CSRF token...", file=sys.stderr)
    resp = session.get(LOGIN_URL, timeout=30)
    resp.raise_for_status()
    p = TokenParser(); p.feed(resp.text)
    if not p.token:
        raise ValueError("CSRF token tidak ditemukan!")
    print(f"[INFO] Login sebagai {USERNAME}...", file=sys.stderr)
    resp = session.post(LOGIN_URL, data={
        "__RequestVerificationToken": p.token,
        "Username": USERNAME, "Password": PASSWORD, "RememberMe": "false",
    }, timeout=30, allow_redirects=True)
    if "Login" in resp.url or "login" in resp.url:
        raise ValueError("Login gagal!")
    print("[INFO] Login berhasil!", file=sys.stderr)

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def download_excel(session):
    url = (
        f"{BASE_URL}/reports/printRekapIntransit"
        f"?_startDate={START_DATE_STR}&_endDate={END_DATE_STR}"
        f"&_respId={RESP_ID}&_isDetail=false"
    )
    print(f"[INFO] Download: {url}", file=sys.stderr)
    resp = session.get(url, timeout=120)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    print(f"[OK] Download selesai ({len(resp.content)/1024:.1f} KB)", file=sys.stderr)
    return resp.content

# ── PARSE DATA ────────────────────────────────────────────────────────────────
def parse_rows(content):
    """
    - HEADER_ROW : baris kolom (NO, KDCAB, dst) — dicari dinamis
    - raw        : semua baris data (di bawah header)
    - agg        : agregat [year][month][kdcab] = {nilai, nota}
    """
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    ws = wb.active

    header_row_idx = None
    col_headers    = []

    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=20, values_only=True), start=1):
        if row[0] == "NO":          # baris header kolom ketemu
            header_row_idx = i
            col_headers    = list(row)
            break

    if header_row_idx is None:
        raise ValueError("Baris header kolom (NO, KDCAB, ...) tidak ditemukan!")

    # Index kolom berdasarkan nama header
    def ci(name):
        return col_headers.index(name)

    raw = []
    agg = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {"nilai": 0, "nota": set()})))

    for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
        if not row[0] or not isinstance(row[0], int):
            continue
        raw.append(row)

        if row[ci("JENIS MASALAH")] != "INTRANSIT":
            continue

        kdcab  = str(row[ci("KDCAB")]).strip() if row[ci("KDCAB")] else ""
        nonota = row[ci("NONOTA")]
        nilai  = row[ci("NILAI SJ")] or 0

        tgl = row[ci("TGL KIRIM")]
        if hasattr(tgl, "year"):
            yr, mo = tgl.year, tgl.month
        else:
            from datetime import datetime
            try:
                dt = datetime.strptime(str(tgl), "%d-%B-%Y")
                yr, mo = dt.year, dt.month
            except:
                continue

        if kdcab not in MASTER_KDCAB:
            continue

        agg[yr][mo][kdcab]["nilai"] += nilai
        agg[yr][mo][kdcab]["nota"].add(nonota)

    wb.close()
    return col_headers, raw, agg

# ── BUILD EXCEL ───────────────────────────────────────────────────────────────
def build_output(content, col_headers, raw_rows, agg):
    out_wb = openpyxl.Workbook()

    # ── Sheet 1: Data Intransit ───────────────────────────────────────────────
    ws1 = out_wb.active
    ws1.title = "Data Intransit"

    # Filter hanya KDCAB yang ada di MASTER, lalu sort
    kdcab_idx = col_headers.index("KDCAB")
    filtered_rows = [r for r in raw_rows if str(r[kdcab_idx] or "").strip() in MASTER_KDCAB]
    sorted_rows   = sorted(filtered_rows, key=lambda r: str(r[kdcab_idx] or ""))

    # Tulis header kolom
    ws1.append(col_headers)
    for cell in ws1[1]:
        sc(cell, bold=True, bg=CLR_HEADER, fg="FFFFFF", align="center", size=11)
        cell.border = thin_border()
    ws1.row_dimensions[1].height = 22

    # Tulis data (sorted) dengan zebra striping
    num_cols = len(col_headers)
    nilai_cols = set()  # kolom numerik (currency)
    for ci2, h in enumerate(col_headers, 1):
        if h and "NILAI" in str(h).upper():
            nilai_cols.add(ci2)

    for ri, row in enumerate(sorted_rows, start=2):
        ws1.append(list(row))
        bg = CLR_ALT if ri % 2 == 0 else CLR_ROW
        for c in range(1, num_cols + 1):
            cell = ws1.cell(row=ri, column=c)
            sc(cell, bg=bg, align="left" if c <= 6 else "right", size=10)
            cell.border = thin_border()
            if c in nilai_cols:
                cell.number_format = '#,##0'
                cell.alignment = Alignment(horizontal="right", vertical="center")

    # Auto filter
    ws1.auto_filter.ref = ws1.dimensions

    # Lebar kolom sheet1
    col_widths = {"A":5, "B":8, "C":8, "D":35, "E":14, "F":13, "G":13,
                  "H":8, "I":14, "J":10, "K":8, "L":14, "M":12, "N":14, "O":60, "P":30}
    for col_letter, w in col_widths.items():
        ws1.column_dimensions[col_letter].width = w
    ws1.freeze_panes = "A2"
    ws1.sheet_properties.tabColor = "1B2A4A"

    # ── Sheet per tahun ───────────────────────────────────────────────────────
    years = sorted(agg.keys())

    for year in years:
        # Tampilkan semua bulan Jan-Des, tapi stop di bulan H-2 kalau tahun berjalan
        max_month = h2.month if year == h2.year else 12
        all_months = list(range(1, max_month + 1))   # semua bulan muncul, meski kosong

        ws = out_wb.create_sheet(title=str(year))
        total_cols = 1 + len(all_months) * 2

        ws.sheet_properties.tabColor = "2C3E6B"

        # Baris 1: judul tahun
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_cols)
        c = ws.cell(row=1, column=1, value=f"REKAP INTRANSIT — {year}")
        sc(c, bold=True, bg=CLR_HEADER, fg="FFFFFF", align="center", size=13)
        c.border = thin_border()
        ws.row_dimensions[1].height = 28

        # Baris 2: CABANG + nama bulan (merged 2 kolom per bulan)
        ws.merge_cells(start_row=2, start_column=1, end_row=3, end_column=1)
        c = ws.cell(row=2, column=1, value="CABANG")
        sc(c, bold=True, bg=CLR_BULAN, align="center", size=11)
        c.border = thin_border()
        # also style row 3 col 1 for the merged cell border
        ws.cell(row=3, column=1).border = thin_border()

        col = 2
        for mo in all_months:
            ws.merge_cells(start_row=2, start_column=col, end_row=2, end_column=col+1)
            c = ws.cell(row=2, column=col, value=MONTHS[mo-1].upper())
            sc(c, bold=True, bg=CLR_BULAN, align="center", size=10)
            c.border = thin_border()
            ws.cell(row=2, column=col+1).border = thin_border()
            col += 2

        # Baris 3: Nilai SJ | Nota
        col = 2
        for mo in all_months:
            c1 = ws.cell(row=3, column=col,   value="Nilai SJ")
            c2 = ws.cell(row=3, column=col+1, value="Nota")
            sc(c1, bold=True, bg=CLR_SUBHDR, fg="FFFFFF", align="center", size=10)
            sc(c2, bold=True, bg=CLR_SUBHDR, fg="FFFFFF", align="center", size=10)
            c1.border = thin_border()
            c2.border = thin_border()
            col += 2

        ws.row_dimensions[2].height = 20
        ws.row_dimensions[3].height = 18

        # Data cabang
        DR = 4
        total_nilai = defaultdict(int)
        total_nota  = defaultdict(int)

        for i, (kdcab, label) in enumerate(CABANG_MASTER):
            r  = DR + i
            row_bg = CLR_ALT if i % 2 == 0 else CLR_ROW

            c = ws.cell(row=r, column=1, value=label)
            sc(c, bg=row_bg, align="left", size=10)
            c.border = thin_border()

            col = 2
            for mo in all_months:
                d     = agg[year][mo][kdcab]
                nilai = d["nilai"]
                nota  = len(d["nota"])
                total_nilai[mo] += nilai
                total_nota[mo]  += nota

                cn = ws.cell(row=r, column=col,   value=nilai if nilai else None)
                ck = ws.cell(row=r, column=col+1, value=nota  if nota  else None)
                sc(cn, bg=row_bg, align="right", size=10)
                sc(ck, bg=row_bg, align="center", size=10)
                cn.number_format = '#,##0'
                cn.border = thin_border()
                ck.border = thin_border()
                col += 2

        # Baris TOTAL
        TR = DR + len(CABANG_MASTER)
        c = ws.cell(row=TR, column=1, value="TOTAL")
        sc(c, bold=True, bg=CLR_TOTAL, align="left", size=11)
        c.border = thin_border()

        col = 2
        for mo in all_months:
            cn = ws.cell(row=TR, column=col,   value=total_nilai[mo] if total_nilai[mo] else None)
            ck = ws.cell(row=TR, column=col+1, value=total_nota[mo]  if total_nota[mo]  else None)
            sc(cn, bold=True, bg=CLR_TOTAL, align="right", size=11)
            sc(ck, bold=True, bg=CLR_TOTAL, align="center", size=11)
            cn.number_format = '#,##0'
            cn.border = thin_border()
            ck.border = thin_border()
            col += 2

        # Lebar kolom
        ws.column_dimensions["A"].width = 28
        col = 2
        for mo in all_months:
            ws.column_dimensions[get_column_letter(col)].width   = 17
            ws.column_dimensions[get_column_letter(col+1)].width = 6
            col += 2

        ws.freeze_panes = "B4"

    # ── Sheet Say Burger ──────────────────────────────────────────────────────
    build_say_burger_sheet(out_wb, col_headers, raw_rows)

    # ── Sheet Pre-Order ───────────────────────────────────────────────────────
    build_preorder_sheet(out_wb, col_headers, raw_rows)

    return out_wb

# ── SHEET SAY BURGER ──────────────────────────────────────────────────────────
# ── SHEET PRE-ORDER ───────────────────────────────────────────────────────────
def is_preorder_row(nama_item_str):
    """Return True kalau ada minimal 1 item yang namanya diawali 'PO '."""
    if not nama_item_str:
        return False
    items = [x.strip().lower() for x in str(nama_item_str).split(",")]
    return any(item.startswith("po ") for item in items)

def build_preorder_sheet(out_wb, col_headers, raw_rows):
    CLR_PO_HDR = "1A5276"   # biru tua
    CLR_PO_SUB = "2471A3"   # biru medium

    nama_item_idx = None
    for i, h in enumerate(col_headers):
        if h and "NAMA ITEM" in str(h).upper():
            nama_item_idx = i
            break

    if nama_item_idx is None:
        print("[WARN] Kolom 'NAMA ITEM YG SELISIH' tidak ditemukan, sheet Pre-Order dilewati.", file=sys.stderr)
        return

    kdcab_idx = col_headers.index("KDCAB")

    po_rows = [
        r for r in raw_rows
        if str(r[kdcab_idx] or "").strip() in MASTER_KDCAB
        and is_preorder_row(r[nama_item_idx])
    ]
    po_rows = sorted(po_rows, key=lambda r: (str(r[kdcab_idx] or ""), str(r[col_headers.index("TGL KIRIM")] or "")))

    ws = out_wb.create_sheet(title="Pre-Order")
    ws.sheet_properties.tabColor = "1A5276"

    num_cols = len(col_headers)

    # Header
    ws.append(col_headers)
    for cell in ws[1]:
        sc(cell, bold=True, bg=CLR_PO_HDR, fg="FFFFFF", align="center", size=11)
        cell.border = thin_border()
    ws.row_dimensions[1].height = 22

    nilai_cols = {i+1 for i, h in enumerate(col_headers) if h and "NILAI" in str(h).upper()}

    for ri, row in enumerate(po_rows, start=2):
        ws.append(list(row))
        bg = CLR_ALT if ri % 2 == 0 else CLR_ROW
        for c in range(1, num_cols + 1):
            cell = ws.cell(row=ri, column=c)
            sc(cell, bg=bg, align="left" if c <= 6 else "right", size=10)
            cell.border = thin_border()
            if c in nilai_cols:
                cell.number_format = '#,##0'
                cell.alignment = Alignment(horizontal="right", vertical="center")
            if c == nama_item_idx + 1:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # Baris TOTAL
    if po_rows:
        tr = len(po_rows) + 2
        ws.cell(row=tr, column=1, value="TOTAL")
        sc(ws.cell(row=tr, column=1), bold=True, bg=CLR_TOTAL, align="left", size=11)
        ws.cell(row=tr, column=1).border = thin_border()

        for ci_idx, h in enumerate(col_headers, 1):
            if ci_idx == 1:
                continue
            if h and "NILAI" in str(h).upper():
                total = sum(r[ci_idx-1] or 0 for r in po_rows if isinstance(r[ci_idx-1], (int, float)))
                c = ws.cell(row=tr, column=ci_idx, value=total if total else None)
                sc(c, bold=True, bg=CLR_TOTAL, align="right", size=11)
                c.number_format = '#,##0'
                c.border = thin_border()
            elif h and "QTY" in str(h).upper():
                total = sum(r[ci_idx-1] or 0 for r in po_rows if isinstance(r[ci_idx-1], (int, float)))
                c = ws.cell(row=tr, column=ci_idx, value=total if total else None)
                sc(c, bold=True, bg=CLR_TOTAL, align="center", size=11)
                c.border = thin_border()
            else:
                c = ws.cell(row=tr, column=ci_idx)
                sc(c, bg=CLR_TOTAL)
                c.border = thin_border()

    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"

    col_widths = {"A":5, "B":8, "C":8, "D":35, "E":14, "F":13, "G":13,
                  "H":8, "I":14, "J":10, "K":8, "L":14, "M":12, "N":14, "O":50, "P":30}
    for col_letter, w in col_widths.items():
        ws.column_dimensions[col_letter].width = w

    print(f"[OK] Sheet 'Pre-Order': {len(po_rows)} baris ditemukan.", file=sys.stderr)


SAY_BURGER_KEYWORDS = [
    "bun burger", "beef patty", "chicken patty",
    "paper wrap say burger", "box mika say burger",
    "say burger", "vegetable cbn", "pickle timun",
    "signature sauce", "caramelized onion", "cheese slice cbn",
]

def is_say_burger_row(nama_item_str):
    """Return True kalau salah satu item di kolom NAMA ITEM YG SELISIH match keyword."""
    if not nama_item_str:
        return False
    items = [x.strip().lower() for x in str(nama_item_str).split(",")]
    return any(
        any(kw in item for kw in SAY_BURGER_KEYWORDS)
        for item in items
    )

def build_say_burger_sheet(out_wb, col_headers, raw_rows):
    CLR_SB_HDR  = "C0392B"   # merah gelap
    CLR_SB_SUB  = "E74C3C"   # merah terang

    nama_item_idx = None
    if "NAMA ITEM YG SELISIH" in col_headers:
        nama_item_idx = col_headers.index("NAMA ITEM YG SELISIH")
    else:
        # fallback: cari kolom yang mengandung "NAMA ITEM"
        for i, h in enumerate(col_headers):
            if h and "NAMA ITEM" in str(h).upper():
                nama_item_idx = i
                break

    if nama_item_idx is None:
        print("[WARN] Kolom 'NAMA ITEM YG SELISIH' tidak ditemukan, sheet Say Burger dilewati.", file=sys.stderr)
        return

    kdcab_idx = col_headers.index("KDCAB")

    # Filter baris yang ada item Say Burger & KDCAB di master
    sb_rows = [
        r for r in raw_rows
        if str(r[kdcab_idx] or "").strip() in MASTER_KDCAB
        and is_say_burger_row(r[nama_item_idx])
    ]
    sb_rows = sorted(sb_rows, key=lambda r: (str(r[kdcab_idx] or ""), str(r[col_headers.index("TGL KIRIM")] or "")))

    ws = out_wb.create_sheet(title="Say Burger")
    ws.sheet_properties.tabColor = "C0392B"

    num_cols = len(col_headers)

    # Header
    ws.append(col_headers)
    for cell in ws[1]:
        sc(cell, bold=True, bg=CLR_SB_HDR, fg="FFFFFF", align="center", size=11)
        cell.border = thin_border()
    ws.row_dimensions[1].height = 22

    nilai_cols = {i+1 for i, h in enumerate(col_headers) if h and "NILAI" in str(h).upper()}

    for ri, row in enumerate(sb_rows, start=2):
        ws.append(list(row))
        bg = CLR_ALT if ri % 2 == 0 else CLR_ROW
        for c in range(1, num_cols + 1):
            cell = ws.cell(row=ri, column=c)
            sc(cell, bg=bg, align="left" if c <= 6 else "right", size=10)
            cell.border = thin_border()
            if c in nilai_cols:
                cell.number_format = '#,##0'
                cell.alignment = Alignment(horizontal="right", vertical="center")
            # kolom NAMA ITEM wrap text & left align
            if c == nama_item_idx + 1:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # Baris TOTAL di bawah
    if sb_rows:
        tr = len(sb_rows) + 2
        ws.cell(row=tr, column=1, value="TOTAL")
        sc(ws.cell(row=tr, column=1), bold=True, bg=CLR_TOTAL, align="left", size=11)
        ws.cell(row=tr, column=1).border = thin_border()

        for ci_idx, h in enumerate(col_headers, 1):
            if ci_idx == 1:
                continue
            if h and "NILAI" in str(h).upper():
                col_vals = [r[ci_idx - 1] or 0 for r in sb_rows]
                total    = sum(v for v in col_vals if isinstance(v, (int, float)))
                c = ws.cell(row=tr, column=ci_idx, value=total if total else None)
                sc(c, bold=True, bg=CLR_TOTAL, align="right", size=11)
                c.number_format = '#,##0'
                c.border = thin_border()
            elif h and "QTY" in str(h).upper():
                col_vals = [r[ci_idx - 1] or 0 for r in sb_rows]
                total    = sum(v for v in col_vals if isinstance(v, (int, float)))
                c = ws.cell(row=tr, column=ci_idx, value=total if total else None)
                sc(c, bold=True, bg=CLR_TOTAL, align="center", size=11)
                c.border = thin_border()
            else:
                c = ws.cell(row=tr, column=ci_idx)
                sc(c, bg=CLR_TOTAL)
                c.border = thin_border()

    # Auto filter & freeze
    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"

    # Lebar kolom (sama kayak sheet1)
    col_widths = {"A":5, "B":8, "C":8, "D":35, "E":14, "F":13, "G":13,
                  "H":8, "I":14, "J":10, "K":8, "L":14, "M":12, "N":14, "O":50, "P":30}
    for col_letter, w in col_widths.items():
        ws.column_dimensions[col_letter].width = w

    print(f"[OK] Sheet 'Say Burger': {len(sb_rows)} baris ditemukan.", file=sys.stderr)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[INFO] Periode: {START_DATE_STR} s/d {END_DATE_STR}", file=sys.stderr)

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        login(session)
        content = download_excel(session)

        print("[INFO] Parsing data...", file=sys.stderr)
        col_headers, raw_rows, agg = parse_rows(content)

        print("[INFO] Build Excel output...", file=sys.stderr)
        out_wb = build_output(content, col_headers, raw_rows, agg)

        out_dir  = Path(r"D:\intbk\rekap_intransit")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"Rekap_Intransit_{END_DATE_STR}.xlsx"
        out_path = out_dir / out_name
        try:
            out_wb.save(out_path)
        except PermissionError:
            import time
            fallback_name = f"Rekap_Intransit_{END_DATE_STR}_{int(time.time())}.xlsx"
            out_path = out_dir / fallback_name
            out_wb.save(out_path)
            print(f"[WARN] File asli terkunci (sedang dibuka di Excel). Disimpan ke: {out_path}", file=sys.stderr)

        print(f"[OK] File output: {out_path}", file=sys.stderr)
        print(json.dumps({"status": "ok", "file_path": str(out_path)}))

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
