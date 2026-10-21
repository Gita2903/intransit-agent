"""
MCP server untuk sistem intbk-agent & Hermes Customer Service.

Database utama: intbk_agent_db (mandiri, clean, tanpa admin_dashboard2).
Database pembanding: kurang_kirim (Homelab MariaDB).

Fitur & Toolset untuk Hermes CS:
  1. cari_kasus: Lookup kasus intransit/selisih BK berdasarkan kode toko / nonota + status komplain.
  2. input_komplain: Catat tiket keluhan/komplain baru dari toko/PIC.
  3. daftar_komplain: Cek antrean komplain aktif (BARU, PROSES, SELESAI, DITOLAK).
  4. update_status_komplain: Update progres penanganan komplain CS.
  5. sync_kurang_kirim_homelab: Tarik data kurang_kirim terbaru dari Homelab ke DB lokal.
  6. proses_potong_batch: Reconcile nota dengan data gudang & eksekusi tindakan Potong Batch.
  7. tarik_rekap_intransit: Pull rekap intransit/selisih dari IIA Web langsung ke rekap_intransit.
  8. ringkasan_kasus: Overview metrik rekap, status tindakan, dan antrean komplain.
  9. cek_preseden: Cari preseden keputusan/SOP penanganan kasus khusus.
 10. daftar_review: Daftar audit review Leader.

Tes mandiri: python mcp_intransit.py --selftest
"""
import contextlib
import functools
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

import pymysql
import pymysql.cursors
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from mcp.server.fastmcp import FastMCP  # noqa: E402

MAX_CHARS = int(os.environ.get("MCP_MAX_CHARS", "6000"))
VALID_DECISIONS = (
    "PENDING_APPROVAL",
    "AUTO_APPROVED",
    "APPROVED_BY_RENDRA",
    "REJECTED_BY_LEADER",
)
VALID_KOMPLAIN_STATUS = ("BARU", "PROSES", "SELESAI", "DITOLAK")

mcp = FastMCP(
    "intransit",
    instructions=(
        "Tool Customer Service & Operasional Intransit/Selisih BK. "
        "Digunakan untuk pencarian nota/toko, pencatatan komplain, "
        "sinkronisasi data gudang (kurang_kirim), dan verifikasi Potong Batch."
    ),
)


# ── helper database & format ──────────────────────────────────────────────────
@contextlib.contextmanager
def _conn(read_only: bool = False):
    """Koneksi MySQL Agent (intbk_agent_db)."""
    conn = pymysql.connect(
        host=os.environ.get("OFFICE_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("OFFICE_DB_PORT", 3306)),
        user=os.environ.get("OFFICE_DB_USER", "root"),
        password=os.environ.get("OFFICE_DB_PASS", ""),
        database=os.environ.get("OFFICE_DB_NAME", "intbk_agent_db"),
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
    )
    try:
        if read_only:
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
        yield conn
    finally:
        conn.close()


def _v(x):
    if isinstance(x, Decimal):
        return int(x) if x == x.to_integral() else float(x)
    if isinstance(x, (date, datetime)):
        return x.strftime("%Y-%m-%d")
    return x


def _rp(x) -> str:
    try:
        return "Rp " + f"{float(x or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "Rp 0"


def _cut(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + "\n… (dipotong, persempit filter kalau perlu)"


def _tool(fn):
    """Bungkus tool: stdout -> stderr, error jadi teks pendek, output dipotong."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            with contextlib.redirect_stdout(sys.stderr):
                out = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            out = f"ERROR {type(e).__name__}: {str(e)[:250]}"
        return _clip(out)

    return wrapper


_investigator = None
_pipeline = None


def _get_investigator():
    global _investigator
    if _investigator is None:
        with contextlib.redirect_stdout(sys.stderr):
            from agents.employees.emp_investigator import InvestigatorEmployee

            _investigator = InvestigatorEmployee()
    return _investigator


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        with contextlib.redirect_stdout(sys.stderr):
            from agents.employees.emp_pipeline import PipelineEmployee

            _pipeline = PipelineEmployee()
    return _pipeline


# ── tools: CUSTOMER SERVICE & OPERASIONAL ─────────────────────────────────────

@mcp.tool()
@_tool
def cari_kasus(kueri: str = "", nota: str = "", hanya_open: bool = True) -> str:
    """Cari kasus intransit/selisih BK di DB agent.
    kueri = kode toko 4 huruf (misal TVKZ) atau nama toko.
    nota = 4 atau 10 digit nomor nota/SJ. Minimal salah satu diisi."""
    if not (kueri or "").strip() and not (nota or "").strip():
        return "Isi kueri (kode/nama toko) atau nota (4/10 digit)."
    with _conn(read_only=True) as conn:
        rows = _get_investigator().resolve_store_or_nota(
            kdtk_or_query=kueri,
            nonota_or_last4=nota,
            only_open=hanya_open,
            conn=conn,
        )
        if not rows:
            return "Tidak ada kasus intransit yang cocok di DB agent."

        # Cek apakah ada komplain terkait di data_komplain
        kdtks = list({r.get("kdtk") for r in rows if r.get("kdtk")})
        komplain_map = {}
        if kdtks:
            with conn.cursor() as cur:
                in_kdtk = ",".join(["%s"] * len(kdtks))
                cur.execute(
                    f"SELECT kdtk, nonota, status_komplain, keluhan FROM data_komplain WHERE kdtk IN ({in_kdtk}) ORDER BY id DESC LIMIT 20",
                    kdtks,
                )
                for c in cur.fetchall() or []:
                    key = (c["kdtk"], c.get("nonota") or "")
                    if key not in komplain_map:
                        komplain_map[key] = c
                    komplain_map[(c["kdtk"], "")] = c

    lines = [f"{len(rows)} kasus ditemukan:"]
    for r in rows:
        rec = r.get("leader_recommendation") or "-"
        conf = r.get("confidence_score")
        kdtk = r.get("kdtk") or ""
        nonota = r.get("nonota") or ""
        complaint = komplain_map.get((kdtk, nonota)) or komplain_map.get((kdtk, ""))
        c_str = f" | Komplain: [{complaint['status_komplain']}] {_cut(complaint['keluhan'], 50)}" if complaint else ""

        lines.append(
            f"- {kdtk} {_cut(r.get('nama_toko'), 25)} | nota {nonota} | "
            f"kirim {_v(r.get('tgl_kirim'))} | aging {_v(r.get('aging_days'))} hr | "
            f"{_rp(r.get('total_nilai_selisih'))} | {r.get('tipe')} | "
            f"tindakan: {r.get('tindakan')} ({r.get('status')}) | "
            f"Leader: {rec}{f' {conf}%' if conf is not None else ''}{c_str}\n"
            f"  item: {_cut(r.get('items_str'), 140)}\n"
            f"  info: {_cut(r.get('leader_reasoning'), 180)}"
        )
    return "\n".join(lines)


@mcp.tool()
@_tool
def input_komplain(
    kdtk: str,
    keluhan: str,
    nonota: str = "",
    item: str = "",
    pelapor: str = "",
    tgl_kirim: str = "",
) -> str:
    """Catat tiket komplain baru dari toko atau PIC ke database agent.
    kdtk: kode toko 4 karakter (misal TVKZ, wajib).
    keluhan: isi laporan/keluhan toko (wajib).
    nonota: nomor nota jika ada (opsional).
    item: nama/kode item yang dikomplainkan (opsional).
    pelapor: nama atau no WhatsApp pelapor (opsional).
    tgl_kirim: format YYYY-MM-DD jika diketahui (opsional)."""
    kdtk = (kdtk or "").strip().upper()
    keluhan = (keluhan or "").strip()
    nonota = re.sub(r"\D", "", str(nonota or "").strip())
    if not kdtk:
        return "Gagal: kode toko (kdtk) wajib diisi."
    if not keluhan:
        return "Gagal: isi keluhan wajib diisi."

    with _conn(read_only=False) as conn, conn.cursor() as cur:
        # Auto-detect kdcab dari rekap_intransit jika ada
        cur.execute(
            "SELECT kdcab, nama_toko, tgl_kirim FROM rekap_intransit WHERE kdtk = %s ORDER BY id DESC LIMIT 1",
            [kdtk],
        )
        row = cur.fetchone() or {}
        kdcab = row.get("kdcab")
        if not tgl_kirim and row.get("tgl_kirim"):
            tgl_kirim = str(row["tgl_kirim"])[:10]

        sql = """
            INSERT INTO data_komplain
                (kdtk, kdcab, nonota, tgl_kirim, item_komplain, keluhan, pelapor, status_komplain)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, 'BARU')
        """
        cur.execute(
            sql,
            [
                kdtk,
                kdcab,
                nonota or None,
                tgl_kirim or None,
                item.strip() or None,
                keluhan,
                pelapor.strip() or None,
            ],
        )
        tiket_id = cur.lastrowid

    return (
        f"✅ Komplain #{tiket_id} berhasil dicatat!\n"
        f"• Toko: {kdtk} (Cabang: {kdcab or '-'})\n"
        f"• Nota: {nonota or '-'}\n"
        f"• Keluhan: {_cut(keluhan, 120)}\n"
        f"• Status: BARU"
    )


@mcp.tool()
@_tool
def daftar_komplain(status: str = "BARU", kdtk: str = "", batas: int = 15) -> str:
    """Lihat daftar komplain toko/PIC di DB agent.
    status: BARU | PROSES | SELESAI | DITOLAK | KOSONGKAN untuk semua status.
    kdtk opsional (filter kode toko). batas 1-30."""
    batas = max(1, min(int(batas), 30))
    where = []
    params = []

    st = (status or "").strip().upper()
    if st:
        if st not in VALID_KOMPLAIN_STATUS:
            return f"Status tidak valid. Pilihan: {', '.join(VALID_KOMPLAIN_STATUS)}"
        where.append("status_komplain = %s")
        params.append(st)

    kd = (kdtk or "").strip().upper()
    if kd:
        where.append("kdtk = %s")
        params.append(kd)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, kdtk, kdcab, nonota, tgl_kirim, item_komplain, keluhan,
               pelapor, status_komplain, tindakan_diambil, catatan_cs, created_at
        FROM data_komplain
        {where_sql}
        ORDER BY id DESC LIMIT %s
    """
    params.append(batas)

    with _conn(read_only=True) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() or []

    if not rows:
        return f"Tidak ada data komplain yang cocok (filter status: {status or 'Semua'}, toko: {kdtk or 'Semua'})."

    lines = [f"{len(rows)} komplain ditemukan:"]
    for r in rows:
        lines.append(
            f"- #{r['id']} | Toko {r['kdtk']} ({r['kdcab'] or '-'}) | nota {r['nonota'] or '-'} | "
            f"status: {r['status_komplain']} | pelapor: {r['pelapor'] or '-'}\n"
            f"  keluhan: {_cut(r['keluhan'], 140)}\n"
            f"  tindakan: {r['tindakan_diambil'] or '-'} | catatan: {_cut(r['catatan_cs'] or '-', 100)}"
        )
    return "\n".join(lines)


@mcp.tool()
@_tool
def update_status_komplain(
    id_komplain: int,
    status: str,
    tindakan: str = "",
    catatan: str = "",
) -> str:
    """Update status dan tindakan pada tiket komplain.
    id_komplain: nomor ID komplain.
    status: BARU | PROSES | SELESAI | DITOLAK.
    tindakan: tindakan yang diputuskan (misal Potong Batch, SJ Ulang, dll).
    catatan: catatan penjelasan CS."""
    st = (status or "").strip().upper()
    if st not in VALID_KOMPLAIN_STATUS:
        return f"Status harus salah satu dari: {', '.join(VALID_KOMPLAIN_STATUS)}"

    with _conn(read_only=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, kdtk, nonota, status_komplain FROM data_komplain WHERE id = %s", [id_komplain])
        row = cur.fetchone()
        if not row:
            return f"Komplain #{id_komplain} tidak ditemukan."

        cur.execute(
            """
            UPDATE data_komplain
            SET status_komplain = %s,
                tindakan_diambil = COALESCE(NULLIF(%s, ''), tindakan_diambil),
                catatan_cs = COALESCE(NULLIF(%s, ''), catatan_cs),
                updated_at = NOW()
            WHERE id = %s
            """,
            [st, tindakan.strip() or None, catatan.strip() or None, id_komplain],
        )

    return f"✅ Komplain #{id_komplain} berhasil di-update: status={st}, tindakan={tindakan or '-'}, catatan={catatan or '-'}"


@mcp.tool()
@_tool
def sync_kurang_kirim_homelab(full_sync: bool = False) -> str:
    """Tarik data kurang_kirim terbaru dari database Homelab MariaDB ke DB agent.
    full_sync: True jika ingin sinkronisasi total, False untuk sinkronisasi inkremental."""
    res = _get_investigator().sync_kurang_kirim_from_homelab(full_sync=full_sync)
    if res.get("status") == "ok":
        return f"✅ Sinkronisasi Homelab OK: {res.get('synced_rows', 0)} baris disinkronkan (Max ID: {res.get('max_id', res.get('last_id'))})."
    return f"Gagal sinkronisasi: {res}"


@mcp.tool()
@_tool
def proses_potong_batch(kdtk: str, nonota: str, keterangan: str = "") -> str:
    """Verifikasi nota terhadap data Kurang Kirim Gudang di Homelab.
    Jika barang terbukti tidak dikirim dari gudang, otomatis set tindakan 'Potong Batch'
    di rekap_actions dan update tiket komplain terkait ke SELESAI."""
    kdtk = (kdtk or "").strip().upper()
    nota_digits = re.sub(r"\D", "", str(nonota or "").strip())
    if not kdtk or not nota_digits:
        return "Isi kdtk (kode toko) dan nonota."

    last4 = nota_digits[-4:].zfill(4)

    with _conn(read_only=False) as conn, conn.cursor() as cur:
        # Cek data kurang_kirim_sync
        cur.execute(
            """
            SELECT id, kode_toko, tgl_kirim, nomor_surat_jalan, sj_last4, lampiran
            FROM kurang_kirim_sync
            WHERE UPPER(kode_toko) = %s AND (sj_last4 = %s OR nomor_surat_jalan LIKE %s)
            ORDER BY id DESC LIMIT 1
            """,
            [kdtk, last4, f"%{last4}%"],
        )
        kk = cur.fetchone()

        if not kk:
            return (
                f"❌ [TIDAK DITEMUKAN DI GUDANG]\n"
                f"Nota {nonota} (4 digit: {last4}) untuk Toko {kdtk} TIDAK ADA di data kurang_kirim gudang.\n"
                f"SOP: Tidak boleh Potong Batch jika belum ada konfirmasi kurang kirim gudang. "
                f"Gunakan 'SJ Ulang' jika PLU mati, atau 'Beban Karyawan' jika salah kirim/hilang di jalan."
            )

        # Cocok: lakukan tindakan Potong Batch
        ket = keterangan.strip() or f"Match kurang_kirim gudang (NoSJ: {kk.get('nomor_surat_jalan')}, Lampiran: {kk.get('lampiran')})"
        cur.execute(
            """
            INSERT INTO rekap_actions
                (kdtk, nonota, tindakan, status, keterangan, updated_by)
            VALUES
                (%s, %s, 'Potong Batch', 'Open', %s, 'Hermes-CS')
            ON DUPLICATE KEY UPDATE
                tindakan = 'Potong Batch',
                status = 'Open',
                keterangan = VALUES(keterangan),
                updated_by = 'Hermes-CS'
            """,
            [kdtk, nonota, ket],
        )

        # Jika ada tiket komplain terkait di data_komplain, tandai SELESAI
        cur.execute(
            """
            UPDATE data_komplain
            SET status_komplain = 'SELESAI',
                tindakan_diambil = 'Potong Batch',
                catatan_cs = %s,
                updated_at = NOW()
            WHERE kdtk = %s AND (nonota = %s OR nonota LIKE %s) AND status_komplain IN ('BARU', 'PROSES')
            """,
            [f"Auto-resolved via Potong Batch. {ket}", kdtk, nonota, f"%{last4}"],
        )

    return (
        f"✅ [SUKSES POTONG BATCH]\n"
        f"• Toko: {kdtk} | Nota: {nonota}\n"
        f"• Bukti Gudang: NoSJ {kk.get('nomor_surat_jalan')}, Lampiran: {kk.get('lampiran')}\n"
        f"• Tindakan: 'Potong Batch' tercatat di rekap_actions (Status: Open)\n"
        f"• Komplain terkait otomatis di-update ke 'SELESAI'."
    )


@mcp.tool()
@_tool
def tarik_rekap_intransit(hari_mundur: int = 2) -> str:
    """Tarik data rekap intransit dan selisih BK dari IIA Web langsung ke tabel rekap_intransit.
    hari_mundur: jumlah hari ke belakang untuk query (default 2 hari / H-2)."""
    with _conn(read_only=False) as conn:
        res = _get_pipeline().pull_detail_rekap_and_sync_to_db(conn, days_back=hari_mundur)

    if res.get("status") == "ok":
        return f"✅ Tarik Rekap Intransit Berhasil! {res.get('rows_upserted', 0)} baris data dimasukkan ke DB agent (Periode: {res.get('periode_end')})."
    return f"Gagal tarik data IIA: {res.get('error', res)}"


@mcp.tool()
@_tool
def sync_pagi(hari_mundur: int = 2) -> str:
    """Tarik data rekap intransit dari IIA (H-2) dan sync data kurang_kirim gudang dari Homelab sekaligus.
    Sangat cocok untuk cron job / Task Scheduler / n8n tiap pagi hari."""
    lines = ["=== SINKRONISASI PAGI (HOMELAB & REKAP INTRANSIT) ==="]

    # 1. Sync data kurang kirim dari Homelab MariaDB
    kk_res = sync_kurang_kirim_homelab(full_sync=False)
    lines.append(f"1. Homelab Sync: {kk_res}")

    # 2. Tarik detail rekap intransit dari portal IIA Web (H-2)
    iia_res = tarik_rekap_intransit(hari_mundur=hari_mundur)
    lines.append(f"2. Rekap IIA: {iia_res}")

    return "\n".join(lines)


@mcp.tool()
@_tool
def ringkasan_kasus() -> str:
    """Ringkasan angka: status tindakan rekap intransit, antrean komplain toko,
    dan data gudang kurang_kirim."""
    out = []
    with _conn(read_only=True) as conn, conn.cursor() as cur:
        # 1. Rekap Tindakan
        try:
            cur.execute(
                """
                SELECT tindakan, status, COUNT(*) AS n
                FROM rekap_actions
                GROUP BY tindakan, status
                ORDER BY n DESC
                """
            )
            rows = cur.fetchall() or []
            out.append("Tindakan Rekap Tercatat (tindakan | status | jumlah):")
            out += [f"- {r['tindakan']} | {r['status']} | {r['n']}" for r in rows] or ["- (kosong)"]
        except pymysql.MySQLError as e:
            out.append(f"Tindakan rekap: error ({e.args[0] if e.args else 'err'})")

        # 2. Antrean Komplain Toko
        cur.execute(
            """
            SELECT status_komplain, COUNT(*) AS n
            FROM data_komplain
            GROUP BY status_komplain
            ORDER BY n DESC
            """
        )
        rows = cur.fetchall() or []
        out.append("Data Komplain CS (status | jumlah):")
        out += [f"- {r['status_komplain']}: {r['n']} komplain" for r in rows] or ["- (kosong/belum ada)"]

        # 3. Total Kurang Kirim Gudang
        cur.execute("SELECT COUNT(*) AS total, MAX(tgl_kirim) AS tgl_max FROM kurang_kirim_sync")
        kk = cur.fetchone() or {}
        out.append(f"Gudang Kurang Kirim (Homelab Sync): {kk.get('total', 0)} data (terakhir: {kk.get('tgl_max') or '-'})")

        # 4. Total baris di rekap_intransit
        cur.execute("SELECT COUNT(*) AS total, MAX(tgl_kirim) AS max_tgl FROM rekap_intransit")
        ri = cur.fetchone() or {}
        out.append(f"Rekap Intransit Lokal: {ri.get('total', 0)} baris item (tgl data terakhir: {ri.get('max_tgl') or '-'})")

    return "\n".join(out)


@mcp.tool()
@_tool
def cek_preseden(kata_kunci: str) -> str:
    """Cari preseden SOP kasus khusus yang pernah diputuskan (agent_precedents).
    Cocokkan ke kata kunci atau alasan. Maks 10."""
    kw = (kata_kunci or "").strip()
    if not kw:
        return "Isi kata_kunci."
    like = f"%{kw}%"
    with _conn(read_only=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT kata_kunci, kategori, tindakan, alasan, contoh_kasus, created_at
            FROM agent_precedents
            WHERE kata_kunci LIKE %s OR alasan LIKE %s
            ORDER BY id DESC LIMIT 10
            """,
            (like, like),
        )
        rows = cur.fetchall() or []
    if not rows:
        return f"Belum ada preseden untuk '{kw}'."
    lines = [f"{len(rows)} preseden:"]
    for r in rows:
        lines.append(
            f"- [{r['kata_kunci']}] ({r['kategori']}) -> {r['tindakan']} | "
            f"{_cut(r['alasan'], 200)} | contoh: {r['contoh_kasus'] or '-'} | {_v(r['created_at'])}"
        )
    return "\n".join(lines)


@mcp.tool()
@_tool
def daftar_review(status: str = "PENDING_APPROVAL", kdcab: str = "", batas: int = 15) -> str:
    """Daftar hasil review Leader dari agent_case_reviews, urut nilai selisih terbesar.
    status: PENDING_APPROVAL | AUTO_APPROVED | APPROVED_BY_RENDRA | REJECTED_BY_LEADER."""
    status = (status or "").strip().upper()
    if status not in VALID_DECISIONS:
        return "status harus salah satu: " + ", ".join(VALID_DECISIONS)
    batas = max(1, min(int(batas), 30))
    sql = """
        SELECT kdcab, kdtk, nonota, tipe_masalah, aging_days, nilai_selisih,
               in_kurang_kirim, leader_recommendation, confidence_score, leader_reasoning
        FROM agent_case_reviews
        WHERE manager_decision = %s
    """
    params = [status]
    if (kdcab or "").strip():
        sql += " AND kdcab = %s"
        params.append(kdcab.strip().upper())
    sql += " ORDER BY nilai_selisih DESC LIMIT %s"
    params.append(batas)
    with _conn(read_only=True) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() or []
    if not rows:
        return f"Tidak ada review berstatus {status}."
    lines = [f"{len(rows)} review ({status}):"]
    for r in rows:
        kk = "ada di kurang_kirim" if r.get("in_kurang_kirim") else "tidak di kurang_kirim"
        lines.append(
            f"- {r['kdcab']} {r['kdtk']} nota {r['nonota']} | {r['tipe_masalah']} | "
            f"aging {_v(r['aging_days'])} hr | {_rp(r['nilai_selisih'])} | {kk} | "
            f"Leader: {r['leader_recommendation']} {r['confidence_score']}%\n"
            f"  alasan: {_cut(r['leader_reasoning'], 200)}"
        )
    return "\n".join(lines)


# ── selftest & entrypoint ─────────────────────────────────────────────────────
def _selftest() -> int:
    checks = [
        ("ringkasan_kasus", lambda: ringkasan_kasus()),
        ("daftar_komplain", lambda: daftar_komplain(status="", batas=3)),
        ("cek_preseden", lambda: cek_preseden("a")),
        ("input_komplain (uji)", lambda: input_komplain("TEST", "Uji coba komplain otomatis", nonota="9999", pelapor="Tester")),
    ]
    bad = 0
    for name, fn in checks:
        res = fn()
        ok = not str(res).startswith("ERROR")
        bad += 0 if ok else 1
        print(f"[{'OK' if ok else 'GAGAL'}] {name}\n{_cut(res, 300)}\n")

    # Bersihkan komplain uji coba
    with _conn(read_only=False) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM data_komplain WHERE kdtk = 'TEST' AND nonota = '9999'")

    print("Semua lolos." if bad == 0 else f"{bad} cek gagal.")
    return 1 if bad else 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if any(arg in sys.argv for arg in ("--sync-pagi", "--sync", "--sync-all", "--cron")):
        print(sync_pagi())
        sys.exit(0)
    if "--tarik-rekap" in sys.argv:
        print(tarik_rekap_intransit())
        sys.exit(0)
    if "--sync-kk" in sys.argv:
        print(sync_kurang_kirim_homelab())
        sys.exit(0)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
