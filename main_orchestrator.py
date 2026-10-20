import os
import sys
import re
import json
import argparse
import threading
from datetime import date, datetime
from typing import Dict, Any, Optional, Tuple

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
}


def _find_kdtk(caption_text: str):
    """Cari kode toko 4 karakter. Prioritas pola 'KODE [TOKO] : XXXX', lalu 'TOKO XXXX', lalu token toko T/F/O/R."""
    up = caption_text.upper()
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


def parse_pic_whatsapp_with_llm(sender_name: str, caption_text: str, image_base64: Optional[str] = None) -> Dict[str, Any]:
    kdtk_match = _find_kdtk(caption_text)
    nota_match = re.search(r"\b(26\d{8})\b", caption_text)

    if not llm_enabled():  # LLM_DISABLED=1 -> parser regex sederhana
        return {
            "is_claim_report": bool(kdtk_match or nota_match or "intransit" in caption_text.lower() or "selisih" in caption_text.lower()),
            "kdtk": kdtk_match.group(1) if kdtk_match else "UNKNOWN",
            "nonota": nota_match.group(1) if nota_match else "",
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
    - "kdtk": string (4 karakter kode toko, contoh: "TVKZ")
    - "nonota": string (10 digit nomor nota / NO NPB yang di-highlight di layar monitor, contoh: "2610171500", atau kosongkan jika tidak terlihat)
    - "tgl_kirim": string (format YYYY-MM-DD dari TGL NPB atau tanggal hari ini {date.today().strftime('%Y-%m-%d')})
    - "item_dilaporkan": string (nama produk/PLU yang bermasalah/diedit 0, contoh: "Y/M DIMSUM AYAM (PLU: 20110796)")
    - "klaim_pic": string (ringkasan klaim toko/PIC, misal: "Barang tidak datang, diedit 0 di BPB, minta hilangkan intransit")
    """

    try:
        result = ask_json(prompt, image_base64, role="worker")  # Employee 3 (WA parser) = model worker
        if result.pop("_image_ignored", False):
            result["klaim_pic"] = f"{result.get('klaim_pic', '')} [foto tidak terbaca model, hanya teks]".strip()
        return result
    except Exception as e:
        return {
            "is_claim_report": True,
            "kdtk": kdtk_match.group(1) if kdtk_match else "UNKNOWN",
            "nonota": nota_match.group(1) if nota_match else "",
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


def _handle_sj_request(cmd: str, target: str) -> str:
    """Kalau pesan (uppercase) minta SJ/PDF + nomor nota 10 digit: kirim PDF-nya ke target. Return laporan, atau "" kalau bukan permintaan SJ."""
    notas = list(dict.fromkeys(re.findall(r"\b\d{10}\b", cmd)))
    if not notas or not re.search(r"\b(SJ|PDF|SURAT JALAN)\b", cmd) or cmd.startswith(("SET ", "PRESEDEN ")):
        return ""
    out = []
    for nota in notas[:10]:  # shortcut: max 10 per pesan, naikkan kalau perlu
        try:
            pdf = manager.emp_pipeline.download_sj_pdf_by_nota(nota)
        except Exception as e:
            out.append(f"❌ `{nota}` gagal diambil: {str(e)[:120]}")
            continue
        res = manager.emp_comm.send_whatsapp_document(target, pdf["file_path"], f"Surat Jalan {nota}")
        out.append(f"❌ `{nota}` gagal dikirim: {str(res['error'])[:120]}" if "error" in res else f"✅ `{nota}` terkirim")
    return "\n".join(out)


def _is_registered_pic(conn, sender_digits: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agent_pic_contacts WHERE active = 1 AND wa_target = %s LIMIT 1", [sender_digits])
        return cur.fetchone() is not None


def handle_owner_command(text: str, owner_jid: str = "") -> str:
    cmd = text.strip().upper()
    conn = manager.emp_investigator.get_office_conn()

    try:
        if cmd == "BLAST PIC":
            return manager.emp_comm.build_blast_preview(conn)

        if cmd == "BLAST PIC KONFIRM":
            if _blast_lock.locked():
                return "⏳ Blast lagi jalan Bos, tunggu laporan selesainya dulu."
            threading.Thread(target=_run_blast_in_background, args=(owner_jid or manager.emp_comm.owner_wa,), daemon=True).start()
            return "🚀 Blast PIC dimulai. Ada jeda antar pesan biar aman dari banned, laporan menyusul kalau udah kelar."

        if cmd == "LIST PIC":
            return manager.emp_comm.list_pic_contacts(conn)

        sj_reply = _handle_sj_request(cmd, owner_jid or manager.emp_comm.owner_wa)
        if sj_reply:
            return sj_reply

        if cmd.startswith("REG PIC "):
            parts = text.strip().split(maxsplit=4)
            if len(parts) < 4:
                return "⚠️ Format: `REG PIC <KDCAB> <NOMOR/JID> <NAMA>`\nContoh: `REG PIC G026 08123456789 Robby`"
            _, _, kdcab, target = parts[:4]
            nama = parts[4] if len(parts) > 4 else ""
            return manager.emp_comm.register_pic_contact(conn, kdcab, target, nama)

        if cmd == "ACC PB":
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

        if cmd == "CEK REVIEW":
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
                lines.append("💡 Ketik: `SET <KDTK> <NONOTA> <Potong Batch|SJ Ulang|Beban Karyawan|Penjualan Tunai>` untuk eksekusi.")
                return "\n".join(lines)

        if cmd.startswith("SET "):
            parts = text.strip().split(maxsplit=3)
            if len(parts) < 4:
                return "⚠️ Format salah Bos. Gunakan: `SET <KDTK> <NONOTA> <Tindakan>`\nContoh: `SET TVKZ 2610171500 SJ Ulang`"

            _, kdtk, nonota, sisa = parts
            tindakan, _ = _parse_tindakan_and_rest(sisa)
            if not tindakan:
                return f"❌ Tindakan `{sisa}` ga valid. Pilih: Potong Batch, SJ Ulang, Beban Karyawan, atau Penjualan Tunai."

            prev_rec = None
            is_override = False
            with conn.cursor() as cur:
                manager.emp_investigator._ensure_agent_tables(cur)
                cur.execute("""
                    SELECT leader_recommendation, confidence_score, leader_reasoning
                    FROM agent_case_reviews
                    WHERE kdtk = %s AND nonota = %s
                """, [kdtk.upper(), nonota])
                prev = cur.fetchone()

                if prev:
                    prev_rec = prev["leader_recommendation"]
                    if prev_rec != tindakan:
                        is_override = True
                        cur.execute("""
                            INSERT INTO agent_overrides
                                (kdtk, nonota, leader_recommendation, confidence_score, leader_reasoning, rendra_decision, override_source)
                            VALUES (%s, %s, %s, %s, %s, %s, 'WA_SET')
                        """, [kdtk.upper(), nonota, prev_rec, prev["confidence_score"] or 0, prev["leader_reasoning"] or "", tindakan])

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
                "leader_reasoning": f"Approved manual via WA oleh Rendra ({datetime.now().strftime('%d/%m %H:%M')})"
            }], conn)

            if is_override:
                return (
                    f"✅ Mantap Bos! Nota `{nonota}` (Toko *{kdtk.upper()}*) di-set ke *`{tindakan}`*.\n"
                    f"📝 Catatan: Override dari saran Leader (`{prev_rec}`) telah dicatat untuk evaluasi mingguan."
                )
            return f"✅ Mantap Bos! Nota `{nonota}` (Toko *{kdtk.upper()}*) berhasil di-set ke *`{tindakan}`* (Status: `Open`)."

        if cmd.startswith("PRESEDEN "):
            parts = text.strip().split(maxsplit=3)
            if len(parts) < 4:
                return (
                    "⚠️ Format: `PRESEDEN <KDTK> <NONOTA> <Tindakan> <Kata Kunci / Alasan>`\n"
                    "Contoh: `PRESEDEN TVKZ 2610171500 SJ Ulang Toko tutup renovasi 3 hari`"
                )

            _, kdtk, nonota, sisa = parts
            tindakan, alasan = _parse_tindakan_and_rest(sisa)
            if not tindakan or not (alasan or "").strip():
                return (
                    "❌ Tindakan ga valid atau alasan masih kosong.\n"
                    "Format: `PRESEDEN <KDTK> <NONOTA> <Tindakan> <Alasan>`\n"
                    "Contoh: `PRESEDEN TVKZ 2610171500 SJ Ulang Toko tutup renovasi 3 hari`"
                )

            kw = alasan.split()[0:3]
            kw_clean = " ".join(kw).lower().strip()

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
                """, [kw_clean, tindakan, alasan, f"{kdtk.upper()} / {nonota}"])
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
                "leader_reasoning": f"Preseden #{prec_id} via WA Rendra: {alasan}"[:250]
            }], conn)

            return (
                f"✅ *PRESEDEN #{prec_id} TERCATAT!*\n"
                f"• Kasus: Toko *{kdtk.upper()}* (Nota `{nonota}`)\n"
                f"• Tindakan: *`{tindakan}`* (Status: `Open`)\n"
                f"• Kata Kunci Rujukan: *'{kw_clean}'*\n"
                f"• Alasan: _{alasan}_\n"
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

        return (
            "🤖 *Menu Komando Manager Agent:*\n"
            "• `RUN MORNING` — Jalankan sync & audit sekarang\n"
            "• `ACC PB` — Eksekusi semua nota valid Potong Batch\n"
            "• `CEK REVIEW` — Lihat nota yang butuh keputusan manual\n"
            "• `SET <KDTK> <NONOTA> <Tindakan>` — Set tindakan manual\n"
            "• `PRESEDEN <KDTK> <NONOTA> <Tindakan> <Alasan>` — Set & simpan preseden kasus baru\n"
            "• `LIST PRESEDEN` — Lihat daftar preseden kasus khusus\n"
            "• `EVALUASI [HARI]` — Laporan evaluasi mingguan & override rate\n"
            "• `BLAST PIC` — Preview tagihan ke PIC Cabang (belum kirim)\n"
            "• `BLAST PIC KONFIRM` — Kirim tagihan beneran\n"
            "• `REG PIC <KDCAB> <NOMOR/JID> <NAMA>` — Daftarin kontak PIC\n"
            "• `LIST PIC` — Lihat kontak PIC terdaftar\n"
            "• `SJ <NONOTA>` — Kirim PDF Surat Jalan ke WA lo"
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
    if is_owner:
        if text_msg.strip().upper() == "RUN MORNING":
            res = manager.run_morning_workflow(auto_execute_high_confidence=False)
            return jsonify({"status": "morning_triggered", "summary": res["leader_summary"]})

        reply = handle_owner_command(text_msg, remote_jid)
        manager.emp_comm.send_whatsapp_text(remote_jid, reply)
        return jsonify({"status": "owner_command_executed"})

    if sender_digits and text_msg:
        conn = manager.emp_investigator.get_office_conn()
        try:
            known_pic = _is_registered_pic(conn, sender_digits)
        finally:
            conn.close()
        if known_pic:
            sj_reply = _handle_sj_request(text_msg.strip().upper(), remote_jid)
            if sj_reply:
                manager.emp_comm.send_whatsapp_text(remote_jid, sj_reply)
                return jsonify({"status": "sj_sent_to_pic"})

    extracted = parse_pic_whatsapp_with_llm(push_name, text_msg, image_b64)
    if not extracted.get("is_claim_report"):
        return jsonify({"status": "not_a_claim_report"}), 200

    conn = manager.emp_investigator.get_office_conn()
    try:
        wl_id = manager.emp_comm.register_pic_wa_to_waiting_list(
            office_conn=conn,
            pic_sender=push_name,
            kdtk=extracted.get("kdtk", "UNKNOWN"),
            nonota=extracted.get("nonota", ""),
            tgl_kirim=extracted.get("tgl_kirim", date.today().strftime("%Y-%m-%d")),
            item_dilaporkan=extracted.get("item_dilaporkan", ""),
            klaim_pic=extracted.get("klaim_pic", text_msg),
            bukti_foto_path=f"wa_media_{int(datetime.now().timestamp())}.jpg" if image_b64 else "",
        )
    finally:
        conn.close()

    ack_msg = (
        f"Siap Pak/Bu *{push_name}* 🙏,\n"
        f"Laporan untuk Toko *{extracted.get('kdtk')}* "
        f"(Nota: `{extracted.get('nonota') or 'Terlampir'}` | Item: _{extracted.get('item_dilaporkan')}_) "
        f"sudah kami terima dan tercatat di sistem kami.\n\n"
        f"Akan segera kami kroscek dan proses setelah sinkronisasi data H-1. Terima kasih infonya!"
    )
    manager.emp_comm.send_whatsapp_text(remote_jid, ack_msg)
    return jsonify({"status": "registered_to_waiting_list", "waiting_list_id": wl_id, "extracted": extracted})


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
        app.run(host="0.0.0.0", port=args.port, debug=False)
