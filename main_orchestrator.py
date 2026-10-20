import os
import sys
import re
import json
import argparse
import threading
from datetime import date, datetime
from typing import Dict, Any, Optional, Tuple, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, Blueprint, request, jsonify
from dotenv import load_dotenv

from agents.manager import ManagerAgent
from core.llm import ask_json, llm_enabled

load_dotenv()

agent_bp = Blueprint("intbk_agent", __name__)
manager = ManagerAgent(scripts_dir=os.environ.get("SCRIPTS_DIR", "./tools"))

_blast_lock = threading.Lock()


def _run_blast_in_background(owner_jid: str):
    """Blast bisa makan beberapa menit (ada jeda antar kirim), jadi jalan di thread terpisah."""
    if not _blast_lock.acquire(blocking=False):
        return
    conn = None
    try:
        conn = manager.emp_investigator.get_office_conn()
        res = manager.emp_comm.execute_blast(conn)
        lines = [
            "📢 *BLAST PIC SELESAI*",
            f"✅ Terkirim: {res['sent']} pesan ({len(res['cabang_sent'])} cabang: {', '.join(res['cabang_sent']) or '-'})",
        ]
        if res["failed"]:
            lines.append(f"❌ Gagal: {res['failed']} pesan (cek tabel agent_blast_log)")
        if res["skipped_no_contact"]:
            lines.append(f"⚠️ Belum ada kontak: {', '.join(res['skipped_no_contact'])}")
        if res["skipped_cooldown"]:
            lines.append(f"⏳ Dilewati (cooldown): {', '.join(res['skipped_cooldown'])}")
        manager.emp_comm.send_whatsapp_text(owner_jid, "\n".join(lines))
    except Exception as e:
        try:
            manager.emp_comm.send_whatsapp_text(owner_jid, f"💥 Blast PIC error: {e}")
        except Exception:
            pass
    finally:
        if conn:
            conn.close()
        _blast_lock.release()


_KDTK_STOPWORDS = {
    "TOKO", "TAPI", "TADI", "TAHU", "TUNAI", "OKEE", "OKAY", "FOTO", "RETUR", "ROTI",
    "DARI", "KODE", "NAMA", "ASAL", "TUJU", "ITEM", "SAMA", "BEDA", "KARE", "SUSA",
    "MINTA", "KIRIM", "TOLONG", "NOTA", "SURAT", "JALAN", "CEK", "LIHAT", "INFO", "PAGI",
    "SIANG", "SORE", "MALAM", "BISA", "BARU", "LAMA", "BUAT", "UDAH", "SUDAH", "BELUM",
    "YANG", "KALO", "KALAU", "ATAU", "SAYA", "KAMI", "KITA", "PAK", "MAS", "BRO", "BOS",
}

_ID_STOPWORDS = {
    "karena", "karna", "sebab", "kondisi", "info", "infonya", "ada", "adalah", "yaitu",
    "untuk", "buat", "yang", "di", "ke", "dari", "pada", "dalam", "ini", "itu", "tersebut",
    "sudah", "udah", "belum", "masih", "sedang", "lagi", "akan", "bisa", "tidak", "ga", "gak",
    "enggak", "tolong", "mohon", "minta", "pak", "bu", "mas", "bro", "bos", "ya", "yaa", "dong",
}


def _extract_smart_keyword(alasan: str) -> str:
    """
    Ekstrak kata kunci preseden yang bersih:
      - Jika ada delimiter '|', ambil bagian sebelum '|' sebagai kata kunci eksplisit,
        contoh: 'toko renovasi | Karena toko sedang tutup renovasi 3 hari' -> 'toko renovasi'.
      - Jika tanpa '|', buang kata sambung/stopwords umum di awal kalimat lalu ambil 3 kata inti,
        contoh: 'Karena toko sedang tutup renovasi 3 hari' -> 'toko tutup renovasi'.
    """
    raw = (alasan or "").strip()
    if "|" in raw:
        kw_part, _ = raw.split("|", 1)
        if kw_part.strip():
            return kw_part.strip().lower()[:100]

    tokens = re.findall(r"[a-zA-Z0-9]+", raw.lower())
    meaningful = [t for t in tokens if t not in _ID_STOPWORDS]
    chosen = meaningful[:3] if meaningful else tokens[:3]
    return " ".join(chosen).strip()[:100]


def _find_kdtk(caption_text: str):
    """Cari kode toko 4 karakter. Prioritas pola 'KODE [TOKO] : XXXX', lalu 'TOKO XXXX', lalu token toko T/F/O/R."""
    up = (caption_text or "").upper()
    for m in re.finditer(r"\bKODE(?:\s+TOKO)?\s*[:=]\s*([A-Z0-9]{4})\b", up):
        if m.group(1) not in _KDTK_STOPWORDS:
            return m
    for m in re.finditer(r"\bTOKO[ \t]+([A-Z0-9]{4})\b", up):
        if m.group(1) not in _KDTK_STOPWORDS:
            return m
    for m in re.finditer(r"\b([TFOR][A-Z0-9]{3})\b", up):
        if m.group(1) not in _KDTK_STOPWORDS:
            return m
    return None


def _extract_store_name_query(text: str) -> str:
    """Jika user menyebut 'toko Puri Junction' atau 'cek manggarai' tanpa kode 4 huruf, ambil nama tokonya."""
    m = re.search(r"\btoko\s+([a-zA-Z0-9 ]{4,30})", text, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
        # Buang kata komando di ujung
        candidate = re.sub(r"\b(dong|ya|pak|bro|bos|minta|sj|pdf|surat jalan|intransit|selisih|potong batch|sj ulang|beban karyawan|penjualan tunai).*$", "", candidate, flags=re.IGNORECASE).strip()
        if len(candidate) >= 4 and candidate.upper() not in _KDTK_STOPWORDS:
            return candidate
    return ""


def format_store_lookup_reply(query_label: str, rows: List[Dict[str, Any]], is_owner: bool = False) -> str:
    """Format balasan saat orang kirim kode toko / cek status toko di DB."""
    if not rows:
        return (
            f"🔍 *Cek Data Toko `{query_label}`:*\n"
            f"Saat ini tidak ditemukan nota *Intransit / Selisih BK* yang masih berstatus *Open* di database untuk `{query_label}`."
        )

    first = rows[0]
    header = f"🏪 *DATA AKTIF TOKO {first['kdtk']} ({first.get('nama_toko') or '-'})*"
    if len( {r['kdtk'] for r in rows} ) > 1:
        header = f"🔍 *HASIL PENCARIAN `{query_label}` ({len(rows)} Nota Aktif)*"

    lines = [header, f"📍 Cabang: *{first.get('kdcab') or '-'}* | Total: *{len(rows)} Nota Open*", ""]
    for i, r in enumerate(rows, 1):
        rec_str = f" | 🤖 Saran: `{r['leader_recommendation']}`" if (is_owner and r.get("leader_recommendation")) else ""
        lines.append(
            f"{i}. Nota: *`{r['nonota']}`* ({r['tgl_kirim']} • {r.get('aging_days', 0)} hr)\n"
            f"   📌 Status: *{r['tipe']}* | Nilai: *Rp {float(r.get('total_nilai_selisih') or 0):,.0f}* | Tindakan: `{r.get('tindakan') or 'Belum Diproses'}`{rec_str}\n"
            f"   📦 Item: _{(r.get('items_str') or '-')[:120]}_"
        )

    lines.append("")
    if is_owner:
        lines.append("💡 *Tips:* Balas santai aja, misal: `minta sj " + first['kdtk'] + "` atau `" + first['kdtk'] + " potong batch`")
    else:
        lines.append("💡 _Butuh PDF Surat Jalan? Cukup balas: `minta sj " + first['kdtk'] + "` atau langsung infokan kendalanya di sini._")
    return "\n".join(lines)


def _handle_sj_request(text: str, target: str, conn=None) -> str:
    """
    Smart SJ Downloader & Sender:
    Bisa dipicu dengan cara santai:
      1. Sebut 10 digit nota: 'minta sj 2610171500'
      2. Sebut kode toko saja tanpa hafal nomor nota: 'minta sj TVKZ dong' -> auto-lookup DB!
      3. Sebut kode toko + 4 digit akhir SJ: 'sj 1500 toko TVKZ' -> auto-lookup DB!
      4. Sebut nama toko: 'minta pdf surat jalan toko puri junction' -> auto-lookup DB!
    """
    cmd = text.strip().upper()
    if cmd.startswith(("SET ", "PRESEDEN ", "REG PIC")):
        return ""

    wants_sj = bool(re.search(r"\b(SJ|PDF|SURAT\s+JALAN)\b", cmd)) and not bool(re.search(r"\bSJ\s+ULANG\b", cmd))
    if not wants_sj:
        return ""

    # 1. Cek apakah ada 10 digit nonota langsung di teks
    notas_10 = list(dict.fromkeys(re.findall(r"\b(2[56]\d{8})\b", cmd)))

    # 2. Kalau tidak ada 10 digit, auto-lookup ke database dari Kode Toko / Nama Toko / 4 digit akhir SJ!
    resolved_rows = []
    if not notas_10:
        kdtk_m = _find_kdtk(text)
        kdtk_or_name = kdtk_m.group(1) if kdtk_m else _extract_store_name_query(text)
        last4_candidates = [d for d in re.findall(r"\b(\d{4})\b", cmd) if d not in {"2025", "2026"}]
        last4 = last4_candidates[0] if last4_candidates else ""

        if kdtk_or_name or last4:
            resolved_rows = manager.emp_investigator.resolve_store_or_nota(
                kdtk_or_query=kdtk_or_name,
                nonota_or_last4=last4,
                only_open=False,
                conn=conn,
            )
            notas_10 = list(dict.fromkeys(r["nonota"] for r in resolved_rows if r.get("nonota")))

    if not notas_10:
        return (
            "⚠️ Gue udah cek database tapi belum nemu nomor nota yang cocok buat diambil PDF Surat Jalannya.\n"
            "Coba sebutin *Kode Toko* (contoh: `minta sj TVKZ`) atau *Nomor Nota*-nya ya!"
        )

    out = []
    if resolved_rows:
        toko_info = f"{resolved_rows[0]['kdtk']} ({resolved_rows[0].get('nama_toko') or '-'})"
        out.append(f"🔍 Auto-lookup DB untuk *{toko_info}* → ditemukan *{len(notas_10[:5])} nota*:")

    for nota in notas_10[:5]:
        try:
            pdf = manager.emp_pipeline.download_sj_pdf_by_nota(nota)
        except Exception as e:
            out.append(f"❌ `{nota}` gagal diambil dari IIA: {str(e)[:120]}")
            continue
        res = manager.emp_comm.send_whatsapp_document(target, pdf["file_path"], f"Surat Jalan {nota}")
        out.append(f"❌ `{nota}` gagal dikirim: {str(res['error'])[:120]}" if "error" in res else f"✅ PDF SJ `{nota}` terkirim")
    return "\n".join(out)


def parse_pic_whatsapp_with_llm(sender_name: str, caption_text: str, image_base64: Optional[str] = None) -> Dict[str, Any]:
    kdtk_match = _find_kdtk(caption_text)
    nota_match = re.search(r"\b(2[56]\d{8})\b", caption_text)
    last4_match = re.search(r"\b(?:sj|nota|npb)\s*[:#-]?\s*(\d{4})\b", caption_text, re.IGNORECASE)

    if not llm_enabled():
        return {
            "is_claim_report": bool(kdtk_match or nota_match or "intransit" in caption_text.lower() or "selisih" in caption_text.lower() or "kurang" in caption_text.lower() or "tidak datang" in caption_text.lower()),
            "kdtk": kdtk_match.group(1) if kdtk_match else "UNKNOWN",
            "nonota": nota_match.group(1) if nota_match else (last4_match.group(1) if last4_match else ""),
            "tgl_kirim": date.today().strftime("%Y-%m-%d"),
            "item_dilaporkan": "Lihat lampiran/teks",
            "klaim_pic": caption_text,
        }

    prompt = f"""
    Kamu adalah AI Data Extractor untuk sistem logistik & rekonsiliasi toko Indomaret (IDM).
    Pengirim WA: "{sender_name}"
    Pesan Teks: "{caption_text}"

    Tugasmu: Analisis teks dan gambar terlampir (bisa berupa foto layar komputer Daftar NPB/BPB toko atau kertas Surat Jalan).
    Balas HANYA dengan JSON murni (tanpa penjelasan, tanpa markdown) dengan key berikut:
    - "is_claim_report": boolean (true jika ini laporan barang kurang datang / minta hilangkan intransit / selisih / PLU mati / salah toko / relokasi)
    - "kdtk": string (4 karakter kode toko, contoh: "TVKZ", atau kosongkan jika hanya ada nama toko)
    - "nama_toko_disebut": string (nama toko jika disebut di teks/gambar, contoh: "Puri Junction")
    - "nonota": string (10 digit nomor nota / NO NPB yang terlihat di layar monitor/teks, atau 4 digit terakhirnya jika hanya 4 digit, atau kosongkan)
    - "tgl_kirim": string (format YYYY-MM-DD dari TGL NPB atau tanggal hari ini {date.today().strftime('%Y-%m-%d')})
    - "item_dilaporkan": string (nama produk/PLU yang bermasalah/diedit 0, contoh: "Y/M DIMSUM AYAM (PLU: 20110796)")
    - "klaim_pic": string (ringkasan klaim toko/PIC, misal: "Barang tidak datang, diedit 0 di BPB, minta hilangkan intransit")
    """

    try:
        result = ask_json(prompt, image_base64, role="worker")
        if result.pop("_image_ignored", False):
            result["klaim_pic"] = f"{result.get('klaim_pic', '')} [foto tidak terbaca model, hanya teks]".strip()
        if (not result.get("kdtk") or result.get("kdtk") == "UNKNOWN") and kdtk_match:
            result["kdtk"] = kdtk_match.group(1)
        if not result.get("nonota") and nota_match:
            result["nonota"] = nota_match.group(1)
        return result
    except Exception as e:
        return {
            "is_claim_report": True,
            "kdtk": kdtk_match.group(1) if kdtk_match else "UNKNOWN",
            "nonota": nota_match.group(1) if nota_match else (last4_match.group(1) if last4_match else ""),
            "tgl_kirim": date.today().strftime("%Y-%m-%d"),
            "item_dilaporkan": f"Fallback Parser ({e})",
            "klaim_pic": caption_text,
        }


def _parse_tindakan_and_rest(raw: str) -> Tuple[Optional[str], str]:
    """Parse tindakan (Potong Batch/SJ Ulang/Beban Karyawan/Penjualan Tunai/PB/BK/PT/SJ) dan sisa teks alasan."""
    raw_s = raw.strip()
    upper = raw_s.upper()
    for prefix, target in [
        ("POTONG BATCH", "Potong Batch"),
        ("PENJUALAN TUNAI", "Penjualan Tunai"),
        ("BEBAN KARYAWAN", "Beban Karyawan"),
        ("SJ ULANG", "SJ Ulang"),
        ("PB", "Potong Batch"),
        ("PT", "Penjualan Tunai"),
        ("BK", "Beban Karyawan"),
        ("SJ", "SJ Ulang"),
    ]:
        if upper == prefix or upper.startswith(prefix + " ") or upper.startswith(prefix + "|") or upper.startswith(prefix + ":"):
            remainder = raw_s[len(prefix):].lstrip(" |:-")
            return target, remainder
    return None, raw_s


def _detect_natural_tindakan(text: str) -> Optional[str]:
    """Deteksi tindakan dari kalimat santai (misal 'tvkz potong batch aja bro')."""
    up = text.upper()
    if "POTONG BATCH" in up or re.search(r"\bPB\b", up):
        return "Potong Batch"
    if "SJ ULANG" in up or "BUATKAN SJ" in up or "NOTA ULANG" in up:
        return "SJ Ulang"
    if "BEBAN KARYAWAN" in up or "BEBAN DRIVER" in up or "BEBAN PICKING" in up:
        return "Beban Karyawan"
    if "PENJUALAN TUNAI" in up or "JUAL TUNAI" in up:
        return "Penjualan Tunai"
    return None


def _execute_single_set_action(conn, kdtk: str, nonota: str, tindakan: str) -> str:
    """Helper untuk mengeksekusi tindakan + mencatat override jika berbeda dari saran Leader."""
    kdtk = kdtk.upper().strip()
    nonota = nonota.strip()
    prev_rec = None
    is_override = False

    with conn.cursor() as cur:
        manager.emp_investigator._ensure_agent_tables(cur)
        cur.execute("""
            SELECT leader_recommendation, confidence_score, leader_reasoning
            FROM agent_case_reviews
            WHERE kdtk = %s AND nonota = %s
        """, [kdtk, nonota])
        prev = cur.fetchone()

        if prev:
            prev_rec = prev["leader_recommendation"]
            if prev_rec != tindakan:
                is_override = True
                cur.execute("""
                    INSERT INTO agent_overrides
                        (kdtk, nonota, leader_recommendation, confidence_score, leader_reasoning, rendra_decision, override_source)
                    VALUES (%s, %s, %s, %s, %s, %s, 'WA_SET')
                """, [kdtk, nonota, prev_rec, prev["confidence_score"] or 0, prev["leader_reasoning"] or "", tindakan])

        cur.execute("""
            UPDATE agent_case_reviews
            SET manager_decision = 'APPROVED_BY_RENDRA'
            WHERE kdtk = %s AND nonota = %s
        """, [kdtk, nonota])
        conn.commit()

    manager.leader.execute_approved_actions_to_db([{
        "kdtk": kdtk,
        "nonota": nonota,
        "tindakan": tindakan,
        "leader_reasoning": f"Approved manual via WA oleh Rendra ({datetime.now().strftime('%d/%m %H:%M')})"
    }], conn)

    if is_override:
        return (
            f"✅ Mantap Bos! Nota `{nonota}` (Toko *{kdtk}*) di-set ke *`{tindakan}`*.\n"
            f"📝 Catatan: Override dari saran Leader (`{prev_rec}`) telah dicatat untuk evaluasi mingguan."
        )
    return f"✅ Mantap Bos! Nota `{nonota}` (Toko *{kdtk}*) berhasil di-set ke *`{tindakan}`* (Status: `Open`)."


def _is_registered_pic(conn, sender_digits: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agent_pic_contacts WHERE active = 1 AND wa_target = %s LIMIT 1", [sender_digits])
        return cur.fetchone() is not None


def handle_owner_command(text: str, owner_jid: str = "") -> str:
    cmd = text.strip().upper()
    conn = manager.emp_investigator.get_office_conn()

    try:
        # ── 1. KOMANDO EKSPLISIT CEPAT ───────────────────────────────────────
        if cmd == "BLAST PIC":
            return manager.emp_comm.build_blast_preview(conn)

        if cmd == "BLAST PIC KONFIRM":
            if _blast_lock.locked():
                return "⏳ Blast lagi jalan Bos, tunggu laporan selesainya dulu."
            threading.Thread(target=_run_blast_in_background, args=(owner_jid or manager.emp_comm.owner_wa,), daemon=True).start()
            return "🚀 Blast PIC dimulai. Ada jeda antar pesan biar aman dari banned, laporan menyusul kalau udah kelar."

        if cmd == "LIST PIC":
            return manager.emp_comm.list_pic_contacts(conn)

        # Cek permintaan PDF SJ (baik pakai 10 digit nota, kode toko saja, atau 4 digit akhir)
        sj_reply = _handle_sj_request(text, owner_jid or manager.emp_comm.owner_wa, conn=conn)
        if sj_reply:
            return sj_reply

        if cmd.startswith("REG PIC "):
            parts = text.strip().split(maxsplit=4)
            if len(parts) < 4:
                return "⚠️ Format: `REG PIC <KDCAB> <NOMOR/JID> <NAMA>`\nContoh: `REG PIC G026 08123456789 Robby`"
            _, _, kdcab, target = parts[:4]
            nama = parts[4] if len(parts) > 4 else ""
            return manager.emp_comm.register_pic_contact(conn, kdcab, target, nama)

        if cmd in ("ACC PB", "GAS PB", "APPROVE PB"):
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT kdtk, nonota, leader_recommendation, leader_reasoning
                    FROM agent_case_reviews
                    WHERE leader_recommendation = 'Potong Batch'
                      AND confidence_score >= 90
                      AND manager_decision IN ('AUTO_APPROVED', 'PENDING_APPROVAL')
                """)
                rows = cur.fetchall()

                if not rows:
                    return "ℹ️ Bos, saat ini tidak ada antrean nota `Potong Batch` baru yang menunggu eksekusi."

                executed = manager.leader.execute_approved_actions_to_db(rows, conn)
                cur.execute("""
                    UPDATE agent_case_reviews
                    SET manager_decision = 'APPROVED_BY_RENDRA'
                    WHERE leader_recommendation = 'Potong Batch' AND confidence_score >= 90
                """)
                conn.commit()
                return (
                    f"✅ *SIAP BOS! EKSEKUSI SELESAI*\n"
                    f"Sebanyak *{executed} Nota* yang terbukti ada di `kurang_kirim` sudah di-set ke tindakan "
                    f"*`Potong Batch`* dengan status *`Open`* di tabel `rekap_selisih_bk_actions`."
                )

        if cmd in ("CEK REVIEW", "REVIEW", "LIST REVIEW"):
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT kdcab, kdtk, nonota, nilai_selisih, leader_recommendation, leader_reasoning
                    FROM agent_case_reviews
                    WHERE manager_decision = 'PENDING_APPROVAL'
                      AND leader_recommendation != 'Follow Up PIC'
                    ORDER BY nilai_selisih DESC
                    LIMIT 15
                """)
                rows = cur.fetchall()

                if not rows:
                    return "✅ Semua kasus anomali/review khusus sudah bersih Bos!"

                lines = [f"🕵️‍♂️ *DAFTAR KASUS BUTUH KEPUTUSAN LO ({len(rows)} Nota):*", ""]
                for i, r in enumerate(rows, 1):
                    lines.append(
                        f"{i}. *{r['kdtk']}* (`{r['nonota']}`) | Cab: {r['kdcab']} | Rp {float(r['nilai_selisih']):,.0f}\n"
                        f"   👉 *Saran Leader:* `{r['leader_recommendation']}`\n"
                        f"   📝 *Alasan:* {r['leader_reasoning']}\n"
                    )
                lines.append("💡 Balas santai aja, misal: `<KDTK> potong batch` atau `SET <KDTK> <NONOTA> <Tindakan>`")
                return "\n".join(lines)

        if cmd.startswith("SET "):
            parts = text.strip().split(maxsplit=3)
            if len(parts) < 3:
                return "⚠️ Gunakan: `SET <KDTK> [NONOTA] <Tindakan>`\nContoh: `SET TVKZ 2610171500 SJ Ulang` atau `SET TVKZ SJ Ulang`"

            # Support baik `SET TVKZ 2610171500 SJ Ulang` maupun `SET TVKZ SJ Ulang` (tanpa nonota!)
            kdtk = parts[1].upper()
            if len(parts) == 4 and re.fullmatch(r"\d{4,12}", parts[2]):
                nonota_arg = parts[2]
                sisa = parts[3]
            else:
                nonota_arg = ""
                sisa = " ".join(parts[2:])

            tindakan, _ = _parse_tindakan_and_rest(sisa)
            if not tindakan:
                return f"❌ Tindakan `{sisa}` ga valid. Pilih: Potong Batch, SJ Ulang, Beban Karyawan, atau Penjualan Tunai."

            if not nonota_arg or len(nonota_arg) < 8:
                matched = manager.emp_investigator.resolve_store_or_nota(kdtk_or_query=kdtk, nonota_or_last4=nonota_arg, only_open=True, conn=conn)
                if not matched:
                    return f"⚠️ Gue cek di DB ga ada nota Open untuk toko *{kdtk}* {('dengan buntut ' + nonota_arg) if nonota_arg else ''}."
                if len(matched) > 1 and not nonota_arg:
                    nota_list = ", ".join(f"`{m['nonota']}` ({m['tgl_kirim']})" for m in matched[:5])
                    return f"🧐 Bos, toko *{kdtk}* punya *{len(matched)} nota Open*: {nota_list}.\nSebutin 4 digit buntut notanya ya, misal: `{kdtk} {matched[0]['nonota'][-4:]} {tindakan}`"
                nonota_arg = matched[0]["nonota"]

            return _execute_single_set_action(conn, kdtk, nonota_arg, tindakan)

        if cmd.startswith("PRESEDEN "):
            parts = text.strip().split(maxsplit=3)
            if len(parts) < 4:
                return (
                    "⚠️ Format: `PRESEDEN <KDTK> <NONOTA> <Tindakan> <Kata Kunci | Alasan>`\n"
                    "Contoh: `PRESEDEN TVKZ 2610171500 SJ Ulang toko renovasi | Toko tutup renovasi 3 hari`"
                )

            _, kdtk, nonota, sisa = parts
            tindakan, alasan = _parse_tindakan_and_rest(sisa)
            if not tindakan or not (alasan or "").strip():
                return (
                    "❌ Tindakan ga valid atau alasan masih kosong.\n"
                    "Format: `PRESEDEN <KDTK> <NONOTA> <Tindakan> <Alasan>`\n"
                    "Contoh: `PRESEDEN TVKZ 2610171500 SJ Ulang Toko tutup renovasi 3 hari`"
                )

            # Jika nonota yang diketik hanya 4 digit, auto-resolve ke 10 digit dari DB
            if len(re.sub(r"\D", "", nonota)) < 8:
                matched = manager.emp_investigator.resolve_store_or_nota(kdtk_or_query=kdtk, nonota_or_last4=nonota, only_open=False, conn=conn)
                if matched:
                    nonota = matched[0]["nonota"]

            kw_clean = _extract_smart_keyword(alasan)
            alasan_clean = alasan.split("|", 1)[-1].strip() if "|" in alasan else alasan.strip()

            with conn.cursor() as cur:
                manager.emp_investigator._ensure_agent_tables(cur)

                cur.execute("""
                    SELECT leader_recommendation, confidence_score, leader_reasoning
                    FROM agent_case_reviews
                    WHERE kdtk = %s AND nonota = %s
                """, [kdtk.upper(), nonota])
                prev = cur.fetchone()

                if prev and prev["leader_recommendation"] != tindakan:
                    cur.execute("""
                        INSERT INTO agent_overrides
                            (kdtk, nonota, leader_recommendation, confidence_score, leader_reasoning, rendra_decision, override_source)
                        VALUES (%s, %s, %s, %s, %s, %s, 'WA_PRESEDEN')
                    """, [kdtk.upper(), nonota, prev["leader_recommendation"], prev["confidence_score"] or 0, prev["leader_reasoning"] or "", tindakan])

                cur.execute("""
                    INSERT INTO agent_precedents
                        (kata_kunci, kategori, tindakan, alasan, contoh_kasus, created_by)
                    VALUES (%s, 'kasus_khusus', %s, %s, %s, 'Rendra')
                """, [kw_clean, tindakan, alasan_clean, f"{kdtk.upper()} / {nonota}"])
                prec_id = cur.lastrowid

                cur.execute("""
                    UPDATE agent_case_reviews
                    SET manager_decision = 'APPROVED_BY_RENDRA'
                    WHERE kdtk = %s AND nonota = %s
                """, [kdtk.upper(), nonota])
                conn.commit()

            manager.leader.execute_approved_actions_to_db([{
                "kdtk": kdtk.upper(),
                "nonota": nonota,
                "tindakan": tindakan,
                "leader_reasoning": f"Preseden #{prec_id} via WA Rendra: {alasan_clean}"[:250]
            }], conn)

            return (
                f"✅ *PRESEDEN #{prec_id} TERCATAT!*\n"
                f"• Kasus: Toko *{kdtk.upper()}* (Nota `{nonota}`)\n"
                f"• Tindakan: *`{tindakan}`* (Status: `Open`)\n"
                f"• Kata Kunci Rujukan: *'{kw_clean}'*\n"
                f"• Alasan: _{alasan_clean}_\n"
                f"📌 Lain kali klaim serupa muncul, Leader akan merekomendasikan `{tindakan}` sesuai preseden ini."
            )

        if cmd == "LIST PRESEDEN":
            with conn.cursor() as cur:
                manager.emp_investigator._ensure_agent_tables(cur)
                cur.execute("SELECT id, kata_kunci, tindakan, alasan, contoh_kasus FROM agent_precedents ORDER BY id DESC LIMIT 15")
                rows = cur.fetchall()

            if not rows:
                return "ℹ️ Belum ada preseden tercatat Bos. Tambah preseden: `PRESEDEN <KDTK> <NONOTA> <Tindakan> <Alasan>`"

            lines = [f"📚 *DAFTAR PRESEDEN KASUS KHUSUS ({len(rows)} Aturan):*", ""]
            for r in rows:
                lines.append(
                    f"#{r['id']} Pola: *'{r['kata_kunci']}'* → *`{r['tindakan']}`*\n"
                    f"   Alasan: {r['alasan']} (Kasus: {r['contoh_kasus'] or '-'})\n"
                )
            return "\n".join(lines)

        if cmd.startswith("EVALUASI"):
            days = 7
            sub_parts = text.strip().split()
            if len(sub_parts) > 1 and sub_parts[1].isdigit():
                days = max(1, min(90, int(sub_parts[1])))
            return manager.build_weekly_evaluation(conn, days=days)

        # ── 2. CONVERSATIONAL & NATURAL LANGUAGE MODE (TANPA FORMAT KAKU!) ──
        kdtk_m = _find_kdtk(text)
        kdtk_code = kdtk_m.group(1) if kdtk_m else ""
        store_name_q = "" if kdtk_code else _extract_store_name_query(text)
        nota_10_m = re.search(r"\b(2[56]\d{8})\b", cmd)
        last4_m = re.search(r"\b(\d{4})\b", cmd) if not nota_10_m else None
        nota_hint = nota_10_m.group(1) if nota_10_m else (last4_m.group(1) if (last4_m and last4_m.group(1) not in {"2025", "2026"}) else "")
        natural_action = _detect_natural_tindakan(text)

        # 2A. Kalau Bos Rendra nyebut Toko/Nota + Tindakan secara santai (misal: "tvkz potong batch aja" atau "nota 1500 tvkz set sj ulang")
        if natural_action and (kdtk_code or store_name_q or nota_hint):
            matched = manager.emp_investigator.resolve_store_or_nota(
                kdtk_or_query=kdtk_code or store_name_q,
                nonota_or_last4=nota_hint,
                only_open=True,
                conn=conn,
            )
            if not matched:
                return f"⚠️ Gue udah cari di DB untuk `{kdtk_code or store_name_q or nota_hint}`, tapi ga ada nota Open yang cocok buat di-set ke *{natural_action}*."
            if len(matched) > 1 and not nota_10_m:
                nota_list = "\n".join(f"• `{m['nonota']}` (Tgl {m['tgl_kirim']} • Rp {float(m.get('total_nilai_selisih') or 0):,.0f})" for m in matched[:5])
                return (
                    f"🧐 Bos, untuk toko *{matched[0]['kdtk']}* ada *{len(matched)} nota Open* di DB:\n{nota_list}\n\n"
                    f"Mau yang mana yang di-set ke *{natural_action}*? Balas aja: `{matched[0]['kdtk']} {matched[0]['nonota'][-4:]} {natural_action}`"
                )
            target_row = matched[0]
            return _execute_single_set_action(conn, target_row["kdtk"], target_row["nonota"], natural_action)

        # 2B. Kalau Bos Rendra cuma ngetik kode toko ("TVKZ"), nama toko ("cek toko puri junction"), atau nomor nota ("2610171500")
        if kdtk_code or store_name_q or nota_hint:
            matched = manager.emp_investigator.resolve_store_or_nota(
                kdtk_or_query=kdtk_code or store_name_q,
                nonota_or_last4=nota_hint,
                only_open=True,
                conn=conn,
            )
            return format_store_lookup_reply(kdtk_code or store_name_q or nota_hint, matched, is_owner=True)

        return (
            "🤖 *Asisten Operasional Rendra Siap!*\n"
            "Lo bisa langsung chat santai (ga perlu format kaku), contoh:\n"
            "• Ketik kode toko aja: `TVKZ` → auto-cek semua nota intransit/selisih di DB\n"
            "• Minta PDF SJ pakai kode toko: `minta sj TVKZ` → auto-cari nonota & kirim PDF\n"
            "• Eksekusi langsung: `TVKZ potong batch` atau `TVKZ 1500 sj ulang`\n\n"
            "📌 *Shortcut Komando Cepat:*\n"
            "• `ACC PB` | `CEK REVIEW` | `BLAST PIC` | `EVALUASI` | `RUN MORNING`"
        )
    finally:
        conn.close()


@agent_bp.route("/run-morning", methods=["POST"])
def api_run_morning():
    body = request.get_json(silent=True) or {}
    auto_exec = bool(body.get("auto_execute_high_confidence", False))
    result = manager.run_morning_workflow(auto_execute_high_confidence=auto_exec)
    return jsonify(result)


@agent_bp.route("/reviews", methods=["GET"])
def api_get_reviews():
    conn = manager.emp_investigator.get_office_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM agent_case_reviews ORDER BY nilai_selisih DESC LIMIT 200")
            rows = cur.fetchall()
            return jsonify({"data": rows, "count": len(rows)})
    finally:
        conn.close()


@agent_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    payload = request.get_json(silent=True) or {}
    event_type = payload.get("event")
    if event_type and event_type != "messages.upsert":
        return jsonify({"status": "ignored_event"}), 200

    data = payload.get("data", {})
    if isinstance(data, list) and data:
        data = data[0]
    elif isinstance(data, dict) and "messages" in data and isinstance(data["messages"], list) and data["messages"]:
        data = data["messages"][0]

    key = data.get("key", {}) if isinstance(data, dict) else {}
    if key.get("fromMe"):
        return jsonify({"status": "ignored_from_me"}), 200

    remote_jid = key.get("remoteJid", "")
    sender_number = remote_jid.split("@")[0]
    push_name = data.get("pushName", "PIC") if isinstance(data, dict) else "PIC"

    msg_obj = data.get("message", {}) if isinstance(data, dict) else {}
    text_msg = (
        msg_obj.get("conversation")
        or msg_obj.get("extendedTextMessage", {}).get("text")
        or msg_obj.get("imageMessage", {}).get("caption")
        or ""
    )
    image_b64 = msg_obj.get("base64") or payload.get("base64")

    owner_digits = re.sub(r"\D", "", manager.emp_comm.owner_wa)
    if owner_digits.startswith("0"):
        owner_digits = "62" + owner_digits[1:]
    sender_digits = re.sub(r"\D", "", sender_number)
    if sender_digits.startswith("0"):
        sender_digits = "62" + sender_digits[1:]

    is_owner = bool(owner_digits and (owner_digits == sender_digits or owner_digits in sender_digits))
    print(f"[WA] from={remote_jid} owner={is_owner} text={text_msg[:60]!r}")

    # ── KASUS A: PESAN DARI OWNER (BOS RENDRA) ───────────────────────────────
    if is_owner:
        if text_msg.strip().upper() == "RUN MORNING":
            # skip_wa_send=True karena kita kirim sendiri dari sini (1x), bukan dari dalam workflow
            res = manager.run_morning_workflow(auto_execute_high_confidence=False, skip_wa_send=True)
            manager.emp_comm.send_whatsapp_text(remote_jid, res["briefing_message"])
            return jsonify({"status": "morning_triggered", "summary": res["leader_summary"]})

        reply = handle_owner_command(text_msg, remote_jid)
        manager.emp_comm.send_whatsapp_text(remote_jid, reply)
        return jsonify({"status": "owner_command_executed"})

    # ── KASUS B: PESAN DARI PIC CABANG / TOKO (FLEKSIBEL & AUTO-LOOKUP DB) ───
    conn = manager.emp_investigator.get_office_conn()
    try:
        known_pic = _is_registered_pic(conn, sender_digits) if sender_digits else False

        # B1. Jika PIC minta PDF Surat Jalan (bisa cuma sebut kode toko: 'minta sj TVKZ' atau nomor nota)
        if text_msg and not image_b64:
            sj_reply = _handle_sj_request(text_msg, remote_jid, conn=conn)
            if sj_reply:
                manager.emp_comm.send_whatsapp_text(remote_jid, sj_reply)
                return jsonify({"status": "sj_sent_to_pic", "known_pic": known_pic})

        # B2. Jika PIC hanya mengirim Kode Toko saja (misal: "TVKZ" atau "cek TVKZ") tanpa klaim masalah
        clean_words = re.findall(r"[A-Za-z0-9]+", text_msg.strip())
        kdtk_m = _find_kdtk(text_msg)
        is_pure_store_query = (
            not image_b64
            and kdtk_m is not None
            and len(clean_words) <= 3
            and not any(w.lower() in {"kurang", "selisih", "hilangkan", "intransit", "nol", "0", "rusak", "basi", "mati", "tidak", "ga", "gak"} for w in clean_words)
        )
        if is_pure_store_query:
            matched_rows = manager.emp_investigator.resolve_store_or_nota(
                kdtk_or_query=kdtk_m.group(1),
                only_open=True,
                conn=conn,
            )
            lookup_reply = format_store_lookup_reply(kdtk_m.group(1), matched_rows, is_owner=False)
            manager.emp_comm.send_whatsapp_text(remote_jid, lookup_reply)
            return jsonify({"status": "store_lookup_replied", "kdtk": kdtk_m.group(1), "matches": len(matched_rows)})

        # B3. Ekstrak laporan klaim PIC (teks bebas / foto layar NPB)
        extracted = parse_pic_whatsapp_with_llm(push_name, text_msg, image_b64)
        if not extracted.get("is_claim_report"):
            return jsonify({"status": "not_a_claim_report"}), 200

        kdtk_val = (extracted.get("kdtk") or "").strip().upper()
        nama_toko_hint = (extracted.get("nama_toko_disebut") or "").strip() or _extract_store_name_query(text_msg)
        nonota_val = str(extracted.get("nonota") or "").strip()
        tgl_val = extracted.get("tgl_kirim") or date.today().strftime("%Y-%m-%d")

        # AUTO-ENRICH DARI DB: Kalau PIC cuma sebut Kode Toko / Nama Toko tanpa nomor nota (atau cuma 4 digit SJ),
        # otomatis cari nomor nota 10 digit & tanggal kirim aslinya di tabel v_rekap_selisih_intransit!
        db_matched = []
        if (kdtk_val and kdtk_val != "UNKNOWN") or nama_toko_hint or nonota_val:
            q_store = kdtk_val if (kdtk_val and kdtk_val != "UNKNOWN") else nama_toko_hint
            db_matched = manager.emp_investigator.resolve_store_or_nota(
                kdtk_or_query=q_store,
                nonota_or_last4=nonota_val if len(nonota_val) < 10 else "",
                only_open=True,
                conn=conn,
            )
            if db_matched:
                best = db_matched[0]
                if not kdtk_val or kdtk_val == "UNKNOWN":
                    kdtk_val = best["kdtk"]
                if len(nonota_val) < 8:
                    nonota_val = best["nonota"]
                if best.get("tgl_kirim"):
                    tgl_val = str(best["tgl_kirim"])[:10]
                if not extracted.get("item_dilaporkan") or extracted.get("item_dilaporkan") == "Lihat lampiran/teks":
                    extracted["item_dilaporkan"] = (best.get("items_str") or "")[:150]

        wl_id = manager.emp_comm.register_pic_wa_to_waiting_list(
            office_conn=conn,
            pic_sender=push_name,
            kdtk=kdtk_val or "UNKNOWN",
            nonota=nonota_val,
            tgl_kirim=tgl_val,
            item_dilaporkan=extracted.get("item_dilaporkan", ""),
            klaim_pic=extracted.get("klaim_pic", text_msg),
            bukti_foto_path=f"wa_media_{int(datetime.now().timestamp())}.jpg" if image_b64 else "",
        )
    finally:
        conn.close()

    try:
        tgl_human = datetime.strptime(str(tgl_val)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        tgl_human = str(tgl_val)

    nota_part = f" (Nota `{nonota_val}`)" if nonota_val else ""
    item_part = extracted.get("item_dilaporkan") or "item terkait"
    toko_label = kdtk_val if (kdtk_val and kdtk_val != "UNKNOWN") else (nama_toko_hint or "terkait")

    ack_msg = (
        f"Siap Kak/Pak/Bu *{push_name}*, terima kasih infonya 🙏\n\n"
        f"Untuk laporan toko *{toko_label}* kiriman tanggal *{tgl_human}*{nota_part} "
        f"terkait kendala _{item_part}_ sudah kami catat ya. "
        f"Segera kami bantu kroscek dan tindak lanjuti."
    )
    manager.emp_comm.send_whatsapp_text(remote_jid, ack_msg)
    return jsonify({
        "status": "registered_to_waiting_list",
        "waiting_list_id": wl_id,
        "auto_resolved_nonota": nonota_val,
        "extracted": extracted,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Argon Multi-Agent Orchestrator for Rendra")
    parser.add_argument("--mode", choices=["server", "morning", "sync-kk"], default="server")
    parser.add_argument("--port", type=int, default=5055)
    args = parser.parse_args()

    if args.mode == "morning":
        print("🚀 Menjalankan Morning Workflow secara langsung...")
        out = manager.run_morning_workflow(auto_execute_high_confidence=False)
        print(out["briefing_message"])
    elif args.mode == "sync-kk":
        print("🔄 Menjalankan Auto-Sync tabel kurang_kirim dari Home Lab...")
        res = manager.emp_investigator.sync_kurang_kirim_from_homelab(full_sync=True)
        print(json.dumps(res, indent=2))
    else:
        app = Flask(__name__)
        app.register_blueprint(agent_bp, url_prefix="/api/agent")
        print(f"🏢 Server Multi-Agent berjalan di port {args.port}...")
        if manager.emp_comm.owner_wa:
            manager.emp_comm.send_whatsapp_text(
                manager.emp_comm.owner_wa,
                f"🤖 *Argon Agent ONLINE* ✅\n"
                f"📅 {datetime.now().strftime('%d %b %Y %H:%M WIB')}\n"
                f"Port: {args.port}\n\n"
                f"Siap terima komando, Bos. Ketik apa aja buat mulai."
            )
        app.run(host="0.0.0.0", port=args.port, debug=False)
