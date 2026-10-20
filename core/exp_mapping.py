"""
Mapping umur produk (MAPPING4.xlsx, sheet PRICE_LIST) -> hitung sisa umur barang.

Kolom "UMUR PRODUK" formatnya teks, contoh:
  "H+3 dari produksi"      -> shelf life 3 hari dihitung dari tanggal produksi
  "H+180 dari cetak seal"  -> produk frozen/long-life, tanggal cetak seal tidak diketahui sistem

Asumsi tanggal produksi = tgl_kirim - PRODUCTION_LEAD_DAYS (default 1, barang diproduksi H-1 sebelum kirim).
Sisa umur (hari) = (tgl_produksi + N) - hari_ini.
"""
import os
import re
from datetime import date, timedelta
from typing import Dict, Optional, Any

import pandas as pd

MAPPING_PATH = os.environ.get(
    "EXP_MAPPING_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "MAPPING4.xlsx"),
)
PRODUCTION_LEAD_DAYS = int(os.environ.get("PRODUCTION_LEAD_DAYS", 1))
PENJUALAN_TUNAI_SISA_HARI = int(os.environ.get("PENJUALAN_TUNAI_SISA_HARI", 1))

_UMUR_RE = re.compile(r"H\s*\+\s*(\d+)\s*(.*)", re.IGNORECASE)


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


class ExpMapping:
    def __init__(self, path: str = MAPPING_PATH):
        self.by_item_id: Dict[str, Dict[str, Any]] = {}
        self.by_plu: Dict[str, Dict[str, Any]] = {}
        self.by_name: Dict[str, Dict[str, Any]] = {}
        self.loaded = False
        self.load_error: Optional[str] = None
        try:
            self._load(path)
        except Exception as e:  # mapping rusak/ga ada -> Leader fallback ke logika lama
            self.load_error = str(e)

    def _load(self, path: str):
        df = pd.read_excel(path, sheet_name="PRICE_LIST", dtype={"PLU": str})
        for _, r in df.iterrows():
            m = _UMUR_RE.match(str(r["UMUR PRODUK"]).strip())
            if not m:
                continue
            days = int(m.group(1))
            basis = "seal" if "seal" in m.group(2).lower() else "produksi"
            rec = {
                "item_id": str(r["ITEM ID"]).strip().upper(),
                "plu": str(r["PLU"]).strip().replace(".0", ""),
                "nama": str(r["Nama Item"]).strip(),
                "kategori": str(r["KATEGORI"]).strip().upper(),
                "umur_hari": days,
                "basis": basis,
            }
            self.by_item_id[rec["item_id"]] = rec
            self.by_plu[rec["plu"]] = rec
            self.by_name[_norm_name(rec["nama"])] = rec
        self.loaded = bool(self.by_item_id)

    def lookup(self, kode_fg: str, nama_produk: str = "") -> Optional[Dict[str, Any]]:
        """Cari item lewat ITEM ID (FG00xxxx), lalu PLU 8 digit, lalu nama item."""
        kode = str(kode_fg or "").upper()
        m = re.search(r"FG\d+", kode)
        if m and m.group(0) in self.by_item_id:
            return self.by_item_id[m.group(0)]
        for token in re.findall(r"\d{8}", kode):
            if token in self.by_plu:
                return self.by_plu[token]
        return self.by_name.get(_norm_name(nama_produk))

    def sisa_umur(self, rec: Dict[str, Any], tgl_kirim: date, today: Optional[date] = None) -> Optional[int]:
        """Sisa umur dalam hari. None kalau basis bukan 'produksi' (mis. H+180 cetak seal)."""
        if rec["basis"] != "produksi":
            return None
        today = today or date.today()
        exp_date = tgl_kirim - timedelta(days=PRODUCTION_LEAD_DAYS) + timedelta(days=rec["umur_hari"])
        return (exp_date - today).days

    def analyze_case(self, items_raw: str, tgl_kirim: str, today: Optional[date] = None) -> Dict[str, Any]:
        """
        items_raw: 'kode_fg||nama_produk##kode_fg||nama_produk' (item yang selisih di satu nota).
        """
        res = {
            "mapping_ok": self.loaded,
            "total_items": 0, "known": 0, "unknown_items": [], "long_life": 0,
            "sisa_1": [], "expired": [], "longer": [], "min_sisa": None,
            "penjualan_tunai_all": False, "penjualan_tunai_mixed": False,
        }
        if not self.loaded or not items_raw or not tgl_kirim:
            return res
        try:
            tgl = date.fromisoformat(str(tgl_kirim)[:10])
        except ValueError:
            return res

        for chunk in items_raw.split("##"):
            kode, _, nama = chunk.partition("||")
            if not kode.strip() and not nama.strip():
                continue
            res["total_items"] += 1
            rec = self.lookup(kode, nama)
            if not rec:
                res["unknown_items"].append((nama or kode).strip())
                continue
            res["known"] += 1
            sisa = self.sisa_umur(rec, tgl, today)
            if sisa is None:
                res["long_life"] += 1
                res["longer"].append(rec["nama"])
                continue
            res["min_sisa"] = sisa if res["min_sisa"] is None else min(res["min_sisa"], sisa)
            if sisa == PENJUALAN_TUNAI_SISA_HARI:
                res["sisa_1"].append(rec["nama"])
            elif sisa < PENJUALAN_TUNAI_SISA_HARI:
                res["expired"].append(f"{rec['nama']} ({sisa} hr)")
            else:
                res["longer"].append(rec["nama"])

        n = res["total_items"]
        # Penjualan Tunai penuh: SEMUA item di nota ketemu di mapping & sisa umurnya pas
        res["penjualan_tunai_all"] = n > 0 and len(res["sisa_1"]) == n
        res["penjualan_tunai_mixed"] = bool(res["sisa_1"]) and not res["penjualan_tunai_all"]
        return res


_instance: Optional[ExpMapping] = None


def get_mapping() -> ExpMapping:
    global _instance
    if _instance is None:
        _instance = ExpMapping()
    return _instance
