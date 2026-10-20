"""
Blueprint: Rekap Selisih BK & Intransit
Mount prefix: /api/rekap-selisih-bk

Port dari RekapSelisihBkController.php (beserta skrip insert_rekap_intransit.py dan import_selisih_bk_actions.py).
Menangani:
  - GET  /              -> Query agregasi nota + filter + pagination + detail items + actions + BA
  - GET  /filters       -> Distinct cabang list & options
  - POST /action        -> Update/create tindakan di rekap_selisih_bk_actions
  - POST /sync          -> Upload file Excel rekap intransit/selisih BK dan upsert ke rekap_selisih_bk
  - POST /import-tindakan -> Upload file Excel multi-sheet tindakan dan upsert ke rekap_selisih_bk_actions
  - GET  /export        -> Export streaming file CSV 25 kolom sesuai filter aktif
  - GET  /ba/view       -> Preview file PDF Berita Acara
  - GET  /ba/download   -> Download file PDF Berita Acara
"""

import os
import re
import csv
import io
from datetime import datetime
import warnings

from flask import Blueprint, request, jsonify, Response, send_file
import pandas as pd
import pymysql.cursors

from shared.db import get_connection

bp = Blueprint("rekap_selisih_bk", __name__)

BA_FOLDER = os.environ.get("BA_FOLDER", r"D:\intbk\berita-acara")
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "storage", "temp")
os.makedirs(TEMP_DIR, exist_ok=True)

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")


def _format_date(val):
    if not val:
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime("%Y-%m-%d")
    return str(val)[:10]


# ═════════════════════════════════════════════════════════════════════════
# 1. LIST & QUERY AGGREGATION (MIRROR: RekapSelisihBkController::index)
# ═════════════════════════════════════════════════════════════════════════

@bp.route("/", methods=["GET"])
def index():
    search = request.args.get("search", "").strip()
    tipe = request.args.get("tipe", "").strip()
    tgl_dari = request.args.get("tgl_dari", "").strip()
    tgl_sampai = request.args.get("tgl_sampai", "").strip()
    kdcab = request.args.get("kdcab", "").strip()
    status = request.args.get("status", "").strip()
    tindakan = request.args.get("tindakan", "").strip()
    ba = request.args.get("ba", "").strip()

    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(10, int(request.args.get("per_page", 50))))
    offset = (page - 1) * per_page

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Build base where conditions
            where_clauses = ["kode_fg IS NOT NULL AND TRIM(kode_fg) != ''"]
            params = []

            if search:
                where_clauses.append("(nonota LIKE %s OR nama_toko LIKE %s OR kdcab LIKE %s OR kdtk LIKE %s)")
                p = f"%{search}%"
                params.extend([p, p, p, p])

            if tipe:
                where_clauses.append("tipe = %s")
                params.append(tipe)

            if tgl_dari:
                where_clauses.append("tgl_kirim >= %s")
                params.append(tgl_dari)

            if tgl_sampai:
                where_clauses.append("tgl_kirim <= %s")
                params.append(tgl_sampai)

            if kdcab:
                where_clauses.append("kdcab = %s")
                params.append(kdcab)

            # Filter by status / tindakan via rekap_selisih_bk_actions subquery
            if status or tindakan:
                action_where = []
                action_params = []
                if status:
                    action_where.append("status = %s")
                    action_params.append(status)
                if tindakan and tindakan != "Belum Diproses":
                    action_where.append("tindakan = %s")
                    action_params.append(tindakan)

                act_w_str = (" WHERE " + " AND ".join(action_where)) if action_where else ""
                cur.execute(f"SELECT CONCAT(kdtk, '_', nonota) as k FROM rekap_selisih_bk_actions{act_w_str}", action_params)
                action_keys = {r["k"] for r in cur.fetchall()}

                cur.execute("SELECT CONCAT(kdtk, '_', nonota) as k FROM rekap_selisih_bk_actions")
                all_action_keys = {r["k"] for r in cur.fetchall()}

                need_include_no_action = (not tindakan and status == "Open") or (tindakan == "Belum Diproses")

                if need_include_no_action:
                    conds = []
                    if action_keys:
                        in_p = ",".join(["%s"] * len(action_keys))
                        conds.append(f"CONCAT(kdtk, '_', nonota) IN ({in_p})")
                        params.extend(list(action_keys))
                    if all_action_keys:
                        notin_p = ",".join(["%s"] * len(all_action_keys))
                        conds.append(f"CONCAT(kdtk, '_', nonota) NOT IN ({notin_p})")
                        params.extend(list(all_action_keys))
                    else:
                        conds.append("1=1")
                    where_clauses.append(f"({' OR '.join(conds)})")
                else:
                    if action_keys:
                        in_p = ",".join(["%s"] * len(action_keys))
                        where_clauses.append(f"CONCAT(kdtk, '_', nonota) IN ({in_p})")
                        params.extend(list(action_keys))
                    else:
                        where_clauses.append("1=0")

                where_clauses.append("tipe = 'Selisih BK'")

            # Filter by BA Rekonsiliasi
            if ba:
                cur.execute("SELECT DISTINCT CONCAT(kd_toko, '_', no_sj) as ba_k FROM ba_rekonsiliasi WHERE no_sj IS NOT NULL AND kd_toko IS NOT NULL")
                ba_keys = {r["ba_k"] for r in cur.fetchall()}

                if ba == "ada":
                    if ba_keys:
                        in_p = ",".join(["%s"] * len(ba_keys))
                        where_clauses.append(f"(CONCAT(kdtk, '_', nonota) IN ({in_p}) AND tipe = 'Selisih BK')")
                        params.extend(list(ba_keys))
                    else:
                        where_clauses.append("1=0")
                elif ba == "belum":
                    if ba_keys:
                        notin_p = ",".join(["%s"] * len(ba_keys))
                        where_clauses.append(f"(CONCAT(kdtk, '_', nonota) NOT IN ({notin_p}) OR tipe = 'Intransit')")
                        params.extend(list(ba_keys))

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            # 1. Total distinct nota count & overall metrics
            count_sql = f"""
                SELECT COUNT(*) as total_groups,
                       COALESCE(SUM(sub.total_qty_sj), 0) as grand_qty_sj,
                       COALESCE(SUM(sub.total_nilai_sj), 0) as grand_nilai_sj,
                       COALESCE(SUM(sub.total_qty_selisih), 0) as grand_qty_selisih,
                       COALESCE(SUM(sub.total_nilai_selisih), 0) as grand_nilai_selisih
                FROM (
                    SELECT kdtk, nonota,
                           SUM(qty_sj) as total_qty_sj,
                           SUM(nilai_sj) as total_nilai_sj,
                           SUM(qty_selisih) as total_qty_selisih,
                           SUM(nilai_selisih) as total_nilai_selisih
                    FROM v_rekap_selisih_intransit
                    {where_sql}
                    GROUP BY tipe, periode_start, periode_end, kdcab, kdtk, nama_toko, nonota, tgl_kirim
                ) as sub
            """
            cur.execute(count_sql, params)
            summary_row = cur.fetchone() or {}
            total_records = summary_row.get("total_groups", 0)

            # 2. Paginated rows
            data_sql = f"""
                SELECT tipe, periode_start, periode_end, kdcab, kdtk, nama_toko, nonota, tgl_kirim,
                       SUM(qty_sj) as total_qty_sj,
                       SUM(nilai_sj) as total_nilai_sj,
                       SUM(qty_selisih) as total_qty_selisih,
                       SUM(nilai_selisih) as total_nilai_selisih
                FROM v_rekap_selisih_intransit
                {where_sql}
                GROUP BY tipe, periode_start, periode_end, kdcab, kdtk, nama_toko, nonota, tgl_kirim
                ORDER BY periode_start DESC, nonota ASC
                LIMIT %s OFFSET %s
            """
            cur.execute(data_sql, params + [per_page, offset])
            records = cur.fetchall()

            if not records:
                return jsonify({
                    "data": [],
                    "summary": {
                        "total_records": 0,
                        "grand_qty_sj": 0,
                        "grand_nilai_sj": 0,
                        "grand_qty_selisih": 0,
                        "grand_nilai_selisih": 0,
                    },
                    "pagination": {
                        "page": page,
                        "per_page": per_page,
                        "total_records": 0,
                        "total_pages": 1,
                    }
                })

            # Fetch items, actions, and BA data for the current paginated records
            group_keys = [f"{r['kdtk']}_{r['nonota']}" for r in records]
            record_key_parts = [
                (
                    r["tipe"],
                    r["periode_start"],
                    r["periode_end"],
                    r["kdtk"],
                    r["nonota"],
                )
                for r in records
            ]
            nonotas = list({r["nonota"] for r in records if r.get("nonota")})

            # Items breakdown
            record_conditions = []
            record_params = []
            for record_key in record_key_parts:
                record_conditions.append(
                    "(tipe = %s AND periode_start = %s AND periode_end = %s "
                    "AND kdtk = %s AND nonota = %s)"
                )
                record_params.extend(record_key)

            cur.execute(
                f"""
                SELECT id, tipe, periode_start, periode_end, kdtk, nonota, kode_fg, nama_produk, qty_sj, nilai_sj,
                       qty_bpb, nilai_btb, qty_selisih, nilai_selisih, no_btb
                FROM v_rekap_selisih_intransit
                WHERE ({' OR '.join(record_conditions)})
                  AND kode_fg IS NOT NULL AND TRIM(kode_fg) != ''
                ORDER BY no_urut ASC, id ASC
                """,
                record_params
            )
            raw_items = cur.fetchall()
            items_grouped = {}
            for item in raw_items:
                k = (
                    item["tipe"],
                    item["periode_start"],
                    item["periode_end"],
                    item["kdtk"],
                    item["nonota"],
                )
                if k not in items_grouped:
                    items_grouped[k] = []
                items_grouped[k].append({
                    "id": item["id"],
                    "kode_fg": item["kode_fg"] or "",
                    "nama_produk": item["nama_produk"] or "",
                    "qty_sj": int(item["qty_sj"] or 0),
                    "nilai_sj": float(item["nilai_sj"] or 0),
                    "no_btb": item["no_btb"] or "",
                    "qty_bpb": int(item["qty_bpb"] or 0),
                    "nilai_btb": float(item["nilai_btb"] or 0),
                    "qty_selisih": int(item["qty_selisih"] or 0),
                    "nilai_selisih": float(item["nilai_selisih"] or 0),
                })

            # Actions map
            in_keys_p = ",".join(["%s"] * len(group_keys))
            cur.execute(
                f"""
                SELECT kdtk, nonota, tindakan, keterangan, status, updated_by, updated_at
                FROM rekap_selisih_bk_actions
                WHERE CONCAT(kdtk, '_', nonota) IN ({in_keys_p})
                """,
                group_keys
            )
            actions_map = {f"{r['kdtk']}_{r['nonota']}": r for r in cur.fetchall()}

            # BA map
            ba_map = {}
            if nonotas:
                in_nota_p = ",".join(["%s"] * len(nonotas))
                cur.execute(
                    f"""
                    SELECT kd_toko, no_sj, no_ba, source_file
                    FROM ba_rekonsiliasi
                    WHERE no_sj IN ({in_nota_p})
                    """,
                    nonotas
                )
                for b_row in cur.fetchall():
                    bk_key = f"{b_row['kd_toko']}_{b_row['no_sj']}"
                    if bk_key not in ba_map:
                        ba_map[bk_key] = {
                            "no_ba": b_row.get("no_ba") or "",
                            "source_file": b_row.get("source_file") or "",
                        }

            # Assemble rows
            formatted_data = []
            for r in records:
                action_key = f"{r['kdtk']}_{r['nonota']}"
                item_key = (
                    r["tipe"],
                    r["periode_start"],
                    r["periode_end"],
                    r["kdtk"],
                    r["nonota"],
                )
                action = actions_map.get(action_key, {})
                ba_item = ba_map.get(action_key)

                formatted_data.append({
                    "tipe": r.get("tipe") or "Selisih BK",
                    "periode_start": _format_date(r.get("periode_start")),
                    "periode_end": _format_date(r.get("periode_end")),
                    "tgl_kirim": _format_date(r.get("tgl_kirim")),
                    "kdcab": r.get("kdcab") or "",
                    "kdtk": r.get("kdtk") or "",
                    "nama_toko": r.get("nama_toko") or "",
                    "nonota": r.get("nonota") or "",
                    "total_qty_sj": int(r.get("total_qty_sj") or 0),
                    "total_nilai_sj": float(r.get("total_nilai_sj") or 0),
                    "total_qty_selisih": int(r.get("total_qty_selisih") or 0),
                    "total_nilai_selisih": float(r.get("total_nilai_selisih") or 0),
                    "tindakan": action.get("tindakan") or "Belum Diproses",
                    "status": action.get("status") or "Open",
                    "keterangan": action.get("keterangan") or "",
                    "updated_by": action.get("updated_by") or "",
                    "updated_at": _format_date(action.get("updated_at")),
                    "ba": ba_item,
                    "items": items_grouped.get(item_key, []),
                })

            total_pages = (total_records + per_page - 1) // per_page

            return jsonify({
                "data": formatted_data,
                "summary": {
                    "total_records": total_records,
                    "grand_qty_sj": int(summary_row.get("grand_qty_sj", 0)),
                    "grand_nilai_sj": float(summary_row.get("grand_nilai_sj", 0)),
                    "grand_qty_selisih": int(summary_row.get("grand_qty_selisih", 0)),
                    "grand_nilai_selisih": float(summary_row.get("grand_nilai_selisih", 0)),
                },
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total_records": total_records,
                    "total_pages": total_pages,
                }
            })
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════
# 2. FILTERS (CABANG, STATUS, TINDAKAN)
# ═════════════════════════════════════════════════════════════════════════

@bp.route("/filters", methods=["GET"])
def get_filters():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT kdcab FROM v_rekap_selisih_intransit WHERE kdcab IS NOT NULL AND kdcab != '' ORDER BY kdcab ASC")
            cabang_list = [r["kdcab"] for r in cur.fetchall()]

            return jsonify({
                "cabang": cabang_list,
                "tipe": ["Selisih BK", "Intransit"],
                "status": ["Open", "Resolved"],
                "tindakan": [
                    "Belum Diproses",
                    "Potong Batch",
                    "SJ Ulang",
                    "Beban Karyawan",
                    "Penjualan Tunai",
                ]
            })
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════
# 3. UPDATE ACTION (MIRROR: RekapSelisihBkController::updateAction)
# ═════════════════════════════════════════════════════════════════════════

@bp.route("/action", methods=["POST"])
def update_action():
    data = request.get_json() or {}
    kdtk = str(data.get("kdtk") or "").strip()
    nonota = str(data.get("nonota") or "").strip()
    tindakan = str(data.get("tindakan") or "Belum Diproses").strip()
    status = str(data.get("status") or "Open").strip()
    keterangan = str(data.get("keterangan") or "").strip()
    updated_by = str(data.get("updated_by") or "System").strip()

    valid_tindakan = {
        "Belum Diproses",
        "Potong Batch",
        "SJ Ulang",
        "Beban Karyawan",
        "Penjualan Tunai",
    }
    valid_status = {"Open", "Resolved"}

    if not kdtk or not nonota:
        return jsonify({"message": "kdtk dan nonota wajib diisi."}), 400

    if tindakan not in valid_tindakan:
        return jsonify({"message": f"Tindakan '{tindakan}' tidak valid."}), 400

    if status not in valid_status:
        return jsonify({"message": f"Status '{status}' tidak valid."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                INSERT INTO rekap_selisih_bk_actions (kdtk, nonota, tindakan, keterangan, status, updated_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON DUPLICATE KEY UPDATE
                    tindakan = VALUES(tindakan),
                    keterangan = VALUES(keterangan),
                    status = VALUES(status),
                    updated_by = VALUES(updated_by),
                    updated_at = NOW()
            """
            cur.execute(sql, [kdtk, nonota, tindakan, keterangan, status, updated_by])
            conn.commit()

        return jsonify({
            "message": f"Tindakan untuk nota {nonota} berhasil disimpan.",
            "data": {
                "kdtk": kdtk,
                "nonota": nonota,
                "tindakan": tindakan,
                "status": status,
                "keterangan": keterangan,
                "updated_by": updated_by,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        })
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════
# 4. SYNC EXCEL DATA (MIRROR: insert_rekap_intransit.py)
# ═════════════════════════════════════════════════════════════════════════

def _classify_jenis_masalah(raw_value):
    s = str(raw_value).strip().upper()
    if "INTRANSIT" in s:
        return "Intransit"
    if "SELISIH" in s:
        return "Selisih BK"
    return None

def _parse_custom_date(val, fmt="%d-%B-%Y"):
    if pd.isna(val) or not val:
        return None
    val_str = str(val).strip()
    try:
        return datetime.strptime(val_str, fmt).date()
    except Exception:
        try:
            return pd.to_datetime(val_str).date()
        except Exception:
            return None


@bp.route("/sync", methods=["POST"])
def sync_data():
    file = request.files.get("file_excel")
    if not file or not file.filename:
        return jsonify({"message": "File Excel wajib diunggah."}), 400

    filename = f"{int(datetime.now().timestamp())}_{file.filename}"
    filepath = os.path.join(TEMP_DIR, filename)
    file.save(filepath)

    output_lines = []
    output_lines.append(f"Membaca file: {file.filename}")

    try:
        raw = pd.read_excel(filepath, header=None)
        title_row = str(raw.iat[1, 0])
        m = re.search(r"(\d{2}-\w+-\d{4})\s+s/d\s+(\d{2}-\w+-\d{4})", title_row)
        periode_start = _parse_custom_date(m.group(1)) if m else None
        periode_end = _parse_custom_date(m.group(2)) if m else None

        output_lines.append(f"Periode terdeteksi: {periode_start} s/d {periode_end}")

        rows = []
        kode_fg, nama_produk = None, None

        for _, r in raw.iloc[4:].iterrows():
            col0, col1 = r[0], r[1]

            if pd.isna(col0) and pd.notna(col1):
                s = str(col1).strip()
                parts = s.split(" ", 1)
                kode_fg = parts[0]
                nama_produk = parts[1] if len(parts) > 1 else ""
                continue

            if isinstance(col0, str) and col0.strip().lower() == "total":
                continue

            if pd.notna(col0) and str(col0).strip().replace(".0", "").isdigit():
                tipe = _classify_jenis_masalah(r[5])
                if tipe is None:
                    continue

                # Skip subtotal/summary rows that have no product code
                if not kode_fg or not str(kode_fg).strip():
                    continue

                rows.append({
                    "tipe": tipe,
                    "periode_start": periode_start,
                    "periode_end": periode_end,
                    "no_urut": int(float(col0)),
                    "kode_fg": kode_fg,
                    "nama_produk": nama_produk,
                    "kdcab": str(r[1]).strip() if pd.notna(r[1]) else None,
                    "kdtk": str(r[2]).strip() if pd.notna(r[2]) else None,
                    "nama_toko": str(r[3]).strip() if pd.notna(r[3]) else None,
                    "nonota": str(r[4]).strip().replace(".0", "") if pd.notna(r[4]) else None,
                    "tgl_kirim": _parse_custom_date(r[6]),
                    "qty_sj": int(r[7]) if pd.notna(r[7]) else 0,
                    "nilai_sj": float(r[8]) if pd.notna(r[8]) else 0.0,
                    "no_btb": str(r[9]).strip() if pd.notna(r[9]) else None,
                    "qty_bpb": int(r[10]) if pd.notna(r[10]) else 0,
                    "nilai_btb": float(r[11]) if pd.notna(r[11]) else 0.0,
                    "qty_selisih": int(r[12]) if pd.notna(r[12]) else 0,
                    "nilai_selisih": float(r[13]) if pd.notna(r[13]) else 0.0,
                    "source_file": file.filename,
                })

        if not rows:
            return jsonify({
                "status": "error",
                "message": "Tidak ada baris data yang berhasil diparsing. Periksa kembali struktur file.",
                "pipeline_output": "\n".join(output_lines)
            }), 400

        intransit_count = sum(1 for r in rows if r["tipe"] == "Intransit")
        selisih_count = sum(1 for r in rows if r["tipe"] == "Selisih BK")
        output_lines.append(f"Total baris terbaca: {len(rows)} ({intransit_count} Intransit, {selisih_count} Selisih BK)")

        conn = get_connection()
        try:
            with conn.cursor() as cur:
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
                cur.executemany(insert_sql, rows)
                conn.commit()
                affected = cur.rowcount

            output_lines.append(f"Sukses insert/update {len(rows)} baris (affected: {affected}).")
            return jsonify({
                "status": "success",
                "message": f"Proses import selesai. {len(rows)} baris diproses.",
                "pipeline_output": "\n".join(output_lines)
            })
        finally:
            conn.close()

    except Exception as e:
        output_lines.append(f"Error: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Gagal memproses file Excel: {str(e)}",
            "pipeline_output": "\n".join(output_lines)
        }), 500
    finally:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════
# 5. IMPORT TINDAKAN EXCEL (MIRROR: import_selisih_bk_actions.py)
# ═════════════════════════════════════════════════════════════════════════

SHEET_TO_TINDAKAN = {
    "potong batch": "Potong Batch",
    "sj ulang": "SJ Ulang",
    "beban karyawan": "Beban Karyawan",
    "penjualan tunai": "Penjualan Tunai",
}

@bp.route("/import-tindakan", methods=["POST"])
def import_tindakan():
    file = request.files.get("file_excel")
    if not file or not file.filename:
        return jsonify({"message": "File Excel wajib diunggah."}), 400

    filename = f"{int(datetime.now().timestamp())}_{file.filename}"
    filepath = os.path.join(TEMP_DIR, filename)
    file.save(filepath)

    output_lines = []
    output_lines.append(f"Membuka file tindakan: {file.filename}")

    try:
        xls = pd.ExcelFile(filepath)
        output_lines.append(f"Sheets terdeteksi: {xls.sheet_names}")

        conn = get_connection()
        total_inserted = 0

        try:
            with conn.cursor() as cur:
                upsert_sql = """
                    INSERT INTO rekap_selisih_bk_actions (kdtk, nonota, tindakan, status, updated_by, created_at, updated_at)
                    VALUES (%s, %s, %s, 'Open', 'Import Excel', NOW(), NOW())
                    ON DUPLICATE KEY UPDATE
                        tindakan = VALUES(tindakan),
                        updated_by = 'Import Excel',
                        updated_at = NOW()
                """

                for sheet_name in xls.sheet_names:
                    sheet_lower = sheet_name.strip().lower()
                    tindakan = SHEET_TO_TINDAKAN.get(sheet_lower)
                    if not tindakan:
                        output_lines.append(f"Sheet '{sheet_name}' dilewati (bukan jenis tindakan yang didukung).")
                        continue

                    df = pd.read_excel(filepath, sheet_name=sheet_name, header=0)
                    cols_upper = {str(c).strip().upper(): c for c in df.columns}

                    if "KDTK" not in cols_upper or "NONOTA" not in cols_upper:
                        output_lines.append(f"[WARN] Sheet '{sheet_name}' tidak memiliki kolom KDTK dan NONOTA. SKIP.")
                        continue

                    kdtk_col = cols_upper["KDTK"]
                    nonota_col = cols_upper["NONOTA"]

                    main_rows = df[df[kdtk_col].notna() & df[nonota_col].notna()].copy()
                    kdtk_vals = main_rows[kdtk_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
                    nonota_vals = main_rows[nonota_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)

                    pairs = list(dict.fromkeys(zip(kdtk_vals, nonota_vals)))
                    output_lines.append(f"Sheet '{sheet_name}' ({tindakan}): {len(pairs)} nota unik ditemukan.")

                    for kdtk_val, nonota_val in pairs:
                        if kdtk_val and nonota_val:
                            cur.execute(upsert_sql, [kdtk_val, nonota_val, tindakan])
                            total_inserted += 1

                conn.commit()

            output_lines.append(f"Selesai! Total tindakan diproses: {total_inserted}")
            return jsonify({
                "status": "success",
                "message": f"Import tindakan selesai dijalankan. {total_inserted} tindakan di-update.",
                "pipeline_output": "\n".join(output_lines)
            })
        finally:
            conn.close()

    except Exception as e:
        output_lines.append(f"Error: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Gagal memproses file Excel: {str(e)}",
            "pipeline_output": "\n".join(output_lines)
        }), 500
    finally:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════
# 6. EXPORT CSV (MIRROR: RekapSelisihBkController::export)
# ═════════════════════════════════════════════════════════════════════════

@bp.route("/export", methods=["GET"])
def export_csv():
    search = request.args.get("search", "").strip()
    tipe = request.args.get("tipe", "").strip()
    tgl_dari = request.args.get("tgl_dari", "").strip()
    tgl_sampai = request.args.get("tgl_sampai", "").strip()
    kdcab = request.args.get("kdcab", "").strip()
    status = request.args.get("status", "").strip()
    tindakan = request.args.get("tindakan", "").strip()
    ba = request.args.get("ba", "").strip()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            where_clauses = ["kode_fg IS NOT NULL AND TRIM(kode_fg) != ''"]
            params = []

            if search:
                where_clauses.append("(kode_fg LIKE %s OR nama_produk LIKE %s OR nonota LIKE %s OR nama_toko LIKE %s OR kdcab LIKE %s OR kdtk LIKE %s)")
                p = f"%{search}%"
                params.extend([p, p, p, p, p, p])

            if tipe:
                where_clauses.append("tipe = %s")
                params.append(tipe)

            if tgl_dari:
                where_clauses.append("tgl_kirim >= %s")
                params.append(tgl_dari)

            if tgl_sampai:
                where_clauses.append("tgl_kirim <= %s")
                params.append(tgl_sampai)

            if kdcab:
                where_clauses.append("kdcab = %s")
                params.append(kdcab)

            if status or tindakan:
                action_where = []
                action_params = []
                if status:
                    action_where.append("status = %s")
                    action_params.append(status)
                if tindakan and tindakan != "Belum Diproses":
                    action_where.append("tindakan = %s")
                    action_params.append(tindakan)

                act_w_str = (" WHERE " + " AND ".join(action_where)) if action_where else ""
                cur.execute(f"SELECT CONCAT(kdtk, '_', nonota) as k FROM rekap_selisih_bk_actions{act_w_str}", action_params)
                action_keys = {r["k"] for r in cur.fetchall()}

                cur.execute("SELECT CONCAT(kdtk, '_', nonota) as k FROM rekap_selisih_bk_actions")
                all_action_keys = {r["k"] for r in cur.fetchall()}

                need_include_no_action = (not tindakan and status == "Open") or (tindakan == "Belum Diproses")

                if need_include_no_action:
                    conds = []
                    if action_keys:
                        in_p = ",".join(["%s"] * len(action_keys))
                        conds.append(f"CONCAT(kdtk, '_', nonota) IN ({in_p})")
                        params.extend(list(action_keys))
                    if all_action_keys:
                        notin_p = ",".join(["%s"] * len(all_action_keys))
                        conds.append(f"CONCAT(kdtk, '_', nonota) NOT IN ({notin_p})")
                        params.extend(list(all_action_keys))
                    else:
                        conds.append("1=1")
                    where_clauses.append(f"({' OR '.join(conds)})")
                else:
                    if action_keys:
                        in_p = ",".join(["%s"] * len(action_keys))
                        where_clauses.append(f"CONCAT(kdtk, '_', nonota) IN ({in_p})")
                        params.extend(list(action_keys))
                    else:
                        where_clauses.append("1=0")

                where_clauses.append("tipe = 'Selisih BK'")

            if ba:
                cur.execute("SELECT DISTINCT CONCAT(kd_toko, '_', no_sj) as ba_k FROM ba_rekonsiliasi WHERE no_sj IS NOT NULL AND kd_toko IS NOT NULL")
                ba_keys = {r["ba_k"] for r in cur.fetchall()}
                if ba == "ada":
                    if ba_keys:
                        in_p = ",".join(["%s"] * len(ba_keys))
                        where_clauses.append(f"(CONCAT(kdtk, '_', nonota) IN ({in_p}) AND tipe = 'Selisih BK')")
                        params.extend(list(ba_keys))
                    else:
                        where_clauses.append("1=0")
                elif ba == "belum":
                    if ba_keys:
                        notin_p = ",".join(["%s"] * len(ba_keys))
                        where_clauses.append(f"(CONCAT(kdtk, '_', nonota) NOT IN ({notin_p}) OR tipe = 'Intransit')")
                        params.extend(list(ba_keys))

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            query_sql = f"""
                SELECT tipe, periode_start, periode_end, no_urut, kode_fg, nama_produk,
                       kdcab, kdtk, nama_toko, nonota, tgl_kirim, qty_sj, nilai_sj,
                       no_btb, qty_bpb, nilai_btb, qty_selisih, nilai_selisih, source_file
                FROM v_rekap_selisih_intransit
                {where_sql}
                ORDER BY periode_start DESC, id DESC
            """
            cur.execute(query_sql, params)
            records = cur.fetchall()

            cur.execute("SELECT kdtk, nonota, tindakan, keterangan, status, updated_at, updated_by FROM rekap_selisih_bk_actions")
            all_actions = {f"{r['kdtk']}_{r['nonota']}": r for r in cur.fetchall()}

            cur.execute("SELECT kd_toko, no_sj, no_ba FROM ba_rekonsiliasi WHERE no_sj IS NOT NULL")
            ba_map = {f"{r['kd_toko']}_{r['no_sj']}": r["no_ba"] for r in cur.fetchall()}

        def generate_csv():
            output = io.StringIO()
            writer = csv.writer(output)
            columns = [
                "Tipe", "Periode Start", "Periode End", "No Urut", "Kode FG", "Nama Produk",
                "KdCab", "KdTk", "Nama Toko", "No Nota", "Tgl Kirim", "Qty SJ",
                "Nilai SJ", "No BTB", "Qty BPB", "Nilai BTB", "Qty Selisih", "Nilai Selisih",
                "Tindakan", "Keterangan", "Status", "Tgl Update Tindakan", "Updated By",
                "BA Rekonsiliasi", "Source File"
            ]
            writer.writerow(columns)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

            for row in records:
                action_key = f"{row.get('kdtk')}_{row.get('nonota')}"
                act = all_actions.get(action_key, {})
                ba_num = ba_map.get(action_key, "Belum Ada")

                writer.writerow([
                    row.get("tipe") or "",
                    _format_date(row.get("periode_start")) or "",
                    _format_date(row.get("periode_end")) or "",
                    row.get("no_urut") or "",
                    row.get("kode_fg") or "",
                    row.get("nama_produk") or "",
                    row.get("kdcab") or "",
                    row.get("kdtk") or "",
                    row.get("nama_toko") or "",
                    row.get("nonota") or "",
                    _format_date(row.get("tgl_kirim")) or "",
                    row.get("qty_sj") or 0,
                    row.get("nilai_sj") or 0.0,
                    row.get("no_btb") or "",
                    row.get("qty_bpb") or 0,
                    row.get("nilai_btb") or 0.0,
                    row.get("qty_selisih") or 0,
                    row.get("nilai_selisih") or 0.0,
                    act.get("tindakan") or "Belum Diproses",
                    act.get("keterangan") or "",
                    act.get("status") or "Open",
                    _format_date(act.get("updated_at")) or "",
                    act.get("updated_by") or "",
                    ba_num,
                    row.get("source_file") or "",
                ])
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

        filename = f"rekap_selisih_bk_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
        return Response(
            generate_csv(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════
# 7. BA VIEW & DOWNLOAD (MIRROR: RekapSelisihBkController::viewBa/downloadBa)
# ═════════════════════════════════════════════════════════════════════════

@bp.route("/ba/view", methods=["GET"])
def view_ba():
    kdtk = request.args.get("kdtk", "").strip()
    nonota = request.args.get("nonota", "").strip()

    if not kdtk or not nonota:
        return jsonify({"message": "Parameter kdtk dan nonota diperlukan."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source_file, no_ba FROM ba_rekonsiliasi WHERE no_sj = %s AND kd_toko = %s LIMIT 1", [nonota, kdtk])
            row = cur.fetchone()
            if not row or not row.get("source_file"):
                return jsonify({"message": f"Tidak ada file BA Rekonsiliasi untuk nota {nonota}."}), 404

            file_name = row["source_file"]
            file_path = os.path.join(BA_FOLDER, file_name)
            if not os.path.isfile(file_path):
                return jsonify({"message": f"File PDF ({file_name}) tidak ditemukan di server."}), 404

            return send_file(file_path, mimetype="application/pdf")
    finally:
        conn.close()


@bp.route("/ba/download", methods=["GET"])
def download_ba():
    kdtk = request.args.get("kdtk", "").strip()
    nonota = request.args.get("nonota", "").strip()

    if not kdtk or not nonota:
        return jsonify({"message": "Parameter kdtk dan nonota diperlukan."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source_file, no_ba FROM ba_rekonsiliasi WHERE no_sj = %s AND kd_toko = %s LIMIT 1", [nonota, kdtk])
            row = cur.fetchone()
            if not row or not row.get("source_file"):
                return jsonify({"message": f"Tidak ada file BA Rekonsiliasi untuk nota {nonota}."}), 404

            file_name = row["source_file"]
            file_path = os.path.join(BA_FOLDER, file_name)
            if not os.path.isfile(file_path):
                return jsonify({"message": f"File PDF ({file_name}) tidak ditemukan di server."}), 404

            return send_file(file_path, as_attachment=True, download_name=file_name)
    finally:
        conn.close()
