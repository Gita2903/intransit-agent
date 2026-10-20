import os
import sys
import io
import re
import json
import subprocess
import requests
import pandas as pd
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any
from html.parser import HTMLParser
from PyPDF2 import PdfReader
from dotenv import load_dotenv

load_dotenv()


class CSRFTokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs)
            if d.get("name") == "__RequestVerificationToken":
                self.token = d.get("value")


class PipelineEmployee:
    """
    EMPLOYEE 1: Pipeline Executor & Document Downloader.
    Menjalankan script eksisting (IIA -> CSV -> FTP -> Rekap Excel -> Auto-Sync DB -> PDF SJ).
    """

    MONTH_MAP = {
        "01": "January", "02": "February", "03": "March",
        "04": "April",   "05": "May",      "06": "June",
        "07": "July",    "08": "August",   "09": "September",
        "10": "October", "11": "November", "12": "December"
    }

    def __init__(self, scripts_dir: str = "./tools"):
        self.scripts_dir = Path(scripts_dir)
        self.base_url = os.environ.get("IIA_BASE_URL", "http://10.254.137.47/IIAWeb").rstrip("/")
        self.login_url = os.environ.get("IIA_LOGIN_URL", f"{self.base_url}/Account/Login")
        self.username = os.environ.get("IIA_USERNAME", "")
        self.password = os.environ.get("IIA_PASSWORD", "")
        self.resp_id = os.environ.get("IIA_RESP_ID", "91")

    def create_authenticated_iia_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        resp = session.get(self.login_url, timeout=30)
        resp.raise_for_status()
        parser = CSRFTokenParser()
        parser.feed(resp.text)
        if not parser.token:
            raise ValueError("CSRF token tidak ditemukan di halaman login IIA!")

        login_resp = session.post(
            self.login_url,
            data={
                "__RequestVerificationToken": parser.token,
                "Username": self.username,
                "Password": self.password,
                "RememberMe": "false",
            },
            timeout=30,
            allow_redirects=True,
        )
        if "login" in login_resp.url.lower():
            raise ValueError("Login otomatis ke IIAWeb gagal! Periksa IIA_USERNAME/IIA_PASSWORD di .env")

        return session

    def _run_script(self, script_name: str) -> Dict[str, Any]:
        script_path = (self.scripts_dir / script_name).resolve()
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            cwd=str(self.scripts_dir.resolve()),
            env=os.environ.copy()
        )
        stdout_clean = (proc.stdout or "").strip()
        try:
            data = json.loads(stdout_clean.splitlines()[-1]) if stdout_clean else {}
        except Exception:
            data = {"raw_stdout": stdout_clean}

        return {
            "success": proc.returncode == 0 and "error" not in data,
            "returncode": proc.returncode,
            "data": data,
            "stderr": (proc.stderr or "").strip()
        }

    def run_morning_ftp_and_excel_pipeline(self) -> Dict[str, Any]:
        report = {}

        step1 = self._run_script("download_zip_iia.py")
        report["step1_download_zip_iia"] = step1
        if not step1["success"]:
            return {
                "pipeline_ok": False,
                "csv_sync_ok": False,
                "uploaded_csv_count": 0,
                "failed_at": "download_zip_iia.py",
                "details": report,
            }

        step2 = self._run_script("rename_csv_zip.py")
        report["step2_rename_and_upload_ftp"] = step2
        if not step2["success"]:
            return {
                "pipeline_ok": False,
                "csv_sync_ok": False,
                "uploaded_csv_count": 0,
                "failed_at": "rename_csv_zip.py",
                "details": report,
            }

        # FTP 214 = server tujuan upload CSV (FTP_HOST), file BK-nya ditarik hari ini + difilter per supplier (TOKO).
        # Kegagalan 214 tidak menghentikan pipeline; dilaporkan di briefing, sama seperti 215.
        step3a = self._run_script("download_ftp.py")
        report["step3a_download_ftp_214"] = step3a
        not_found_214 = step3a["data"].get("not_found", [])

        # FTP 215 = file BK cabang H-1.
        step3 = self._run_script("download_ftp_215.py")
        report["step3_download_ftp_215"] = step3
        not_found_branches = step3["data"].get("not_found", [])

        step4 = self._run_script("download_rekap_intransit.py")
        report["step4_rekap_excel_pic"] = step4

        return {
            "pipeline_ok": step1["success"] and step2["success"] and step3a["success"] and step3["success"] and step4["success"],
            "csv_sync_ok": step1["success"] and step2["success"],
            "ftp_214_ok": step3a["success"],
            "ftp_214_missing_files": not_found_214,
            "ftp_214_downloaded_count": len(step3a["data"].get("downloaded", [])),
            "ftp_215_ok": step3["success"],
            "ftp_215_downloaded_count": len(step3["data"].get("downloaded", [])),
            "ftp_215_missing_files": not_found_branches,
            "uploaded_csv_count": step2["data"].get("uploaded", 0),
            "details": report,
        }

    def pull_detail_rekap_and_sync_to_db(self, office_conn) -> Dict[str, Any]:
        h1 = date.today() - timedelta(days=1)
        end_str = f"{h1.day}-{h1.strftime('%B-%Y')}"

        # Ambil tgl_kirim terakhir di DB agar query inkremental per hari (hindari IIS timeout multi-hari)
        start_date = None
        try:
            with office_conn.cursor() as cur:
                cur.execute("SELECT MAX(tgl_kirim) AS max_tgl FROM rekap_selisih_bk")
                row = cur.fetchone()
                if row and row.get("max_tgl"):
                    start_date = row["max_tgl"]
        except Exception:
            start_date = None

        if not start_date or start_date > h1:
            start_date = max(date(2026, 1, 1), h1 - timedelta(days=3))
        if (h1 - start_date).days > 7:
            start_date = h1 - timedelta(days=7)

        insert_sql = """
            INSERT INTO rekap_selisih_bk
                (tipe, periode_start, periode_end, no_urut, kode_fg, nama_produk, kdcab, kdtk,
                 nama_toko, nonota, tgl_kirim, qty_sj, nilai_sj, no_btb,
                 qty_bpb, nilai_btb, qty_selisih, nilai_selisih, source_file)
            VALUES
                (%(tipe)s, %(periode_start)s, %(periode_end)s, %(no_urut)s, %(kode_fg)s, %(nama_produk)s,
                 %(kdcab)s, %(kdtk)s, %(nama_toko)s, %(nonota)s, %(tgl_kirim)s,
                 %(qty_sj)s, %(nilai_sj)s, %(no_btb)s, %(qty_bpb)s, %(nilai_btb)s, %(qty_selisih)s,
                 %(nilai_selisih)s, %(source_file)s)
            ON DUPLICATE KEY UPDATE
                tipe=VALUES(tipe), periode_start=VALUES(periode_start), periode_end=VALUES(periode_end),
                no_urut=VALUES(no_urut), nama_produk=VALUES(nama_produk), kdcab=VALUES(kdcab),
                nama_toko=VALUES(nama_toko), tgl_kirim=VALUES(tgl_kirim),
                qty_sj=VALUES(qty_sj), nilai_sj=VALUES(nilai_sj), no_btb=VALUES(no_btb),
                qty_bpb=VALUES(qty_bpb), nilai_btb=VALUES(nilai_btb),
                qty_selisih=VALUES(qty_selisih), nilai_selisih=VALUES(nilai_selisih),
                source_file=VALUES(source_file)
        """

        total_upserted = 0
        session = None
        curr = start_date
        errors = []

        while curr <= h1:
            day_str = f"{curr.day}-{curr.strftime('%B-%Y')}"
            url = (
                f"{self.base_url}/reports/printRekapIntransit"
                f"?_startDate={day_str}&_endDate={day_str}"
                f"&_respId={self.resp_id}&_isDetail=true"
            )
            try:
                if not session:
                    session = self.create_authenticated_iia_session()
                resp = session.get(url, timeout=45)
                resp.raise_for_status()
                raw = pd.read_excel(io.BytesIO(resp.content), header=None)

                rows = []
                kode_fg, nama_produk = None, None
                for _, r in raw.iloc[4:].iterrows():
                    col0, col1 = r[0], r[1]
                    if pd.isna(col0) and pd.notna(col1):
                        parts = str(col1).strip().split(" ", 1)
                        kode_fg = parts[0]
                        nama_produk = parts[1] if len(parts) > 1 else ""
                        continue

                    if isinstance(col0, str) and col0.strip().lower() == "total":
                        continue

                    if pd.notna(col0) and str(col0).strip().replace(".0", "").isdigit():
                        raw_jenis = str(r[5] or "").strip().upper()
                        tipe = "Intransit" if "INTRANSIT" in raw_jenis else ("Selisih BK" if "SELISIH" in raw_jenis else None)
                        if not tipe or not kode_fg:
                            continue

                        tgl_val = r[6]
                        try:
                            tgl_kirim = datetime.strptime(str(tgl_val).strip(), "%d-%B-%Y").date()
                        except Exception:
                            tgl_kirim = pd.to_datetime(tgl_val).date() if pd.notna(tgl_val) else None

                        rows.append({
                            "tipe": tipe,
                            "periode_start": curr,
                            "periode_end": curr,
                            "no_urut": int(float(col0)),
                            "kode_fg": kode_fg,
                            "nama_produk": nama_produk,
                            "kdcab": str(r[1]).strip() if pd.notna(r[1]) else None,
                            "kdtk": str(r[2]).strip() if pd.notna(r[2]) else None,
                            "nama_toko": str(r[3]).strip() if pd.notna(r[3]) else None,
                            "nonota": str(r[4]).strip().replace(".0", "") if pd.notna(r[4]) else None,
                            "tgl_kirim": tgl_kirim,
                            "qty_sj": int(r[7]) if pd.notna(r[7]) else 0,
                            "nilai_sj": float(r[8]) if pd.notna(r[8]) else 0.0,
                            "no_btb": str(r[9]).strip() if pd.notna(r[9]) else None,
                            "qty_bpb": int(r[10]) if pd.notna(r[10]) else 0,
                            "nilai_btb": float(r[11]) if pd.notna(r[11]) else 0.0,
                            "qty_selisih": int(r[12]) if pd.notna(r[12]) else 0,
                            "nilai_selisih": float(r[13]) if pd.notna(r[13]) else 0.0,
                            "source_file": f"AutoSync_Detail_{day_str}.xlsx",
                        })

                if rows:
                    with office_conn.cursor() as cur:
                        cur.executemany(insert_sql, rows)
                        office_conn.commit()
                    total_upserted += len(rows)
            except Exception as e:
                errors.append(f"{day_str}: {e}")
                session = None
            curr += timedelta(days=1)

        if errors and total_upserted == 0:
            return {
                "status": "error",
                "error": f"Timeout/Gagal pull detail IIA ({'; '.join(errors)})",
                "rows_upserted": 0,
                "periode_range": f"{start_date} s/d {h1}",
            }

        return {"status": "ok", "rows_upserted": total_upserted, "periode_end": end_str}

    def download_sj_pdf_by_nota(self, nonota: str, save_dir: str = r"D:\intbk\pdf_sj") -> Dict[str, Any]:
        nota = str(nonota).strip()
        yy, mm, dd = nota[0:2], nota[2:4], nota[4:6]
        month_name = self.MONTH_MAP.get(mm)
        if not month_name:
            raise ValueError(f"Format bulan tidak valid pada nonota: {nota}")

        date_str = f"{dd}-{month_name}-20{yy}"
        url = f"{self.base_url}/deliveryorders/printDeliveryOrders?_date={date_str}&_type=QR&_noNota={nota}"

        session = self.create_authenticated_iia_session()
        resp = session.get(url, verify=False, timeout=60)
        resp.raise_for_status()

        reader = PdfReader(io.BytesIO(resp.content))
        full_text = "\n".join(page.extract_text() or "" for page in reader.pages)

        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, f"SJ_{date_str}_{nota}.pdf")
        with open(file_path, "wb") as f:
            f.write(resp.content)

        return {"nonota": nota, "file_path": file_path, "pdf_text_preview": full_text[:500]}
