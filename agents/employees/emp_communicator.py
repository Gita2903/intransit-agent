import os
import base64
import re
import time
import random
import requests
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

load_dotenv()


class CommunicatorEmployee:
    """
    EMPLOYEE 3: WhatsApp Evolution API Handler & Multimodal Vision Parser.
    Menangani komunikasi masuk/keluar dengan PIC Cabang/Toko dan Bos Rendra.
    """

    CABANG_ALIASES = {
        "JKT1": "G001", "JAKARTA 1": "G001", "G001": "G001",
        "TGR1": "G026", "TANGERANG 1": "G026", "G026": "G026",
        "TGR2": "G033", "TANGERANG 2": "G033", "G033": "G033",
        "PRG": "G107",  "PARUNG": "G107",      "G107": "G107",
        "BGR1": "G113", "BOGOR 1": "G113",     "G113": "G113",
        "BGR2": "G117", "BOGOR 2": "G117",     "G117": "G117",
        "JKT2": "G137", "JAKARTA 2": "G137",   "G137": "G137",
    }

    def __init__(self):
        self.evo_base_url = os.environ.get("EVOLUTION_API_URL", "http://localhost:8080").rstrip("/")
        self.evo_instance = os.environ.get("EVOLUTION_INSTANCE", "rendra-agent")
        self.evo_apikey = os.environ.get("EVOLUTION_API_KEY", "")
        self.owner_wa = os.environ.get("RENDRA_WA_NUMBER", "")

    def send_whatsapp_text(self, remote_jid_or_number: str, text: str) -> Dict[str, Any]:
        url = f"{self.evo_base_url}/message/sendText/{self.evo_instance}"
        headers = {"apikey": self.evo_apikey, "Content-Type": "application/json"}
        payload = {
            "number": remote_jid_or_number,
            "text": text,
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            return resp.json() if resp.ok else {"error": resp.text, "status_code": resp.status_code}
        except Exception as e:
            return {"error": str(e)}

    def send_whatsapp_document(self, remote_jid_or_number: str, file_path: str, caption: str = "") -> Dict[str, Any]:
        url = f"{self.evo_base_url}/message/sendMedia/{self.evo_instance}"
        headers = {"apikey": self.evo_apikey, "Content-Type": "application/json"}
        try:
            with open(file_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            payload = {
                "number": remote_jid_or_number,
                "mediatype": "document",
                "mimetype": "application/pdf",
                "media": b64,
                "fileName": os.path.basename(file_path),
                "caption": caption,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
            return resp.json() if resp.ok else {"error": resp.text, "status_code": resp.status_code}
        except Exception as e:
            return {"error": str(e)}

    def infer_kdcab_from_sender(self, sender_name: str) -> Optional[str]:
        s = (sender_name or "").upper()
        for alias, kdcab in self.CABANG_ALIASES.items():
            if alias in s:
                return kdcab
        return None

    def register_pic_wa_to_waiting_list(
        self,
        office_conn,
        pic_sender: str,
        kdtk: str,
        nonota: Optional[str],
        tgl_kirim: str,
        item_dilaporkan: str,
        klaim_pic: str,
        bukti_foto_path: str = "",
    ) -> int:
        inferred_cab = self.infer_kdcab_from_sender(pic_sender)
        with office_conn.cursor() as cur:
            sql = """
                INSERT INTO agent_waiting_list
                    (pic_sender, kdcab, kdtk, nonota, tgl_kirim, item_dilaporkan,
                     klaim_pic, bukti_foto_path, status_antrean)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, 'WAITING_H1_DATA')
            """
            cur.execute(sql, [
                pic_sender,
                inferred_cab,
                kdtk.strip().upper(),
                (nonota or "").strip(),
                tgl_kirim,
                item_dilaporkan,
                klaim_pic,
                bukti_foto_path,
            ])
            office_conn.commit()
            return cur.lastrowid

    # ═════════════════════════════════════════════════════════════════════
    # BLAST PIC (Penagihan otomatis ke PIC Cabang — kasus "gelap")
    # ═════════════════════════════════════════════════════════════════════
    BLAST_COOLDOWN_HOURS = int(os.environ.get("BLAST_COOLDOWN_HOURS", 20))
    BLAST_TOP_N = int(os.environ.get("BLAST_TOP_N", 10))
    BLAST_DELAY_MIN = float(os.environ.get("BLAST_DELAY_MIN", 6))
    BLAST_DELAY_MAX = float(os.environ.get("BLAST_DELAY_MAX", 14))

    def _ensure_blast_tables(self, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `agent_pic_contacts` (
              `id` int UNSIGNED NOT NULL AUTO_INCREMENT,
              `kdcab` varchar(10) NOT NULL,
              `wa_target` varchar(60) NOT NULL,
              `nama` varchar(100) DEFAULT NULL,
              `active` tinyint(1) NOT NULL DEFAULT 1,
              `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              UNIQUE KEY `uk_pic_target` (`kdcab`, `wa_target`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `agent_blast_log` (
              `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
              `kdcab` varchar(10) NOT NULL,
              `wa_target` varchar(60) NOT NULL,
              `total_nota` int DEFAULT 0,
              `total_nilai` decimal(15,2) DEFAULT 0,
              `status` enum('SENT','FAILED') NOT NULL,
              `detail` text DEFAULT NULL,
              `sent_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_blast_cab` (`kdcab`, `wa_target`, `sent_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

    @staticmethod
    def normalize_wa_target(raw: str) -> Optional[str]:
        """Nomor HP (08xx / +62xx / 62xx) -> 62xx. JID grup (xxx@g.us) dibiarkan apa adanya."""
        raw = (raw or "").strip()
        if raw.endswith("@g.us"):
            return raw
        if "@" in raw:
            raw = raw.split("@")[0]
        digits = re.sub(r"\D", "", raw)
        if digits.startswith("0"):
            digits = "62" + digits[1:]
        if not digits.startswith("62") or not (10 <= len(digits) <= 15):
            return None
        return digits

    def register_pic_contact(self, office_conn, kdcab: str, wa_target_raw: str, nama: str = "") -> str:
        kdcab = (kdcab or "").strip().upper()
        if not re.fullmatch(r"G\d{3}", kdcab):
            return f"⚠️ Kode cabang `{kdcab}` ga valid. Formatnya `G` + 3 digit, contoh `G026`."
        target = self.normalize_wa_target(wa_target_raw)
        if not target:
            return f"⚠️ Nomor/JID `{wa_target_raw}` ga valid. Pakai 08xx / 62xx, atau JID grup `...@g.us`."
        with office_conn.cursor() as cur:
            self._ensure_blast_tables(cur)
            cur.execute("""
                INSERT INTO agent_pic_contacts (kdcab, wa_target, nama, active)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE nama = VALUES(nama), active = 1
            """, [kdcab, target, (nama or "").strip()[:100]])
            office_conn.commit()
        jenis = "grup" if target.endswith("@g.us") else "japri"
        return f"✅ Kontak {jenis} cabang *{kdcab}* tersimpan: `{target}` {('(' + nama.strip() + ')') if nama.strip() else ''}".strip()

    def list_pic_contacts(self, office_conn) -> str:
        with office_conn.cursor() as cur:
            self._ensure_blast_tables(cur)
            cur.execute("SELECT kdcab, wa_target, nama FROM agent_pic_contacts WHERE active = 1 ORDER BY kdcab, id")
            rows = cur.fetchall()
        if not rows:
            return "ℹ️ Belum ada kontak PIC. Daftarin dulu: `REG PIC <KDCAB> <NOMOR/JID> <NAMA>`"
        lines = [f"📇 *Kontak PIC terdaftar ({len(rows)}):*"]
        for r in rows:
            lines.append(f"• {r['kdcab']} → `{r['wa_target']}` {r['nama'] or ''}".rstrip())
        return "\n".join(lines)

    def fetch_chaser_cases(self, office_conn) -> Dict[str, List[Dict[str, Any]]]:
        """
        Kasus 'Follow Up PIC' dari review Leader yang MASIH belum diproses (belum ada tindakan),
        dikelompokkan per KDCAB. Aging dihitung ulang dari tgl_kirim, bukan angka lama di review.
        """
        with office_conn.cursor() as cur:
            cur.execute("""
                SELECT r.kdcab, r.kdtk, r.nonota, r.tipe_masalah AS tipe,
                       r.nilai_selisih AS total_nilai_selisih,
                       v.nama_toko, v.tgl_kirim,
                       DATEDIFF(CURDATE(), v.tgl_kirim) AS aging_days
                FROM agent_case_reviews r
                JOIN (
                    SELECT kdtk, nonota, MAX(nama_toko) AS nama_toko, MAX(tgl_kirim) AS tgl_kirim
                    FROM v_rekap_selisih_intransit
                    WHERE kode_fg IS NOT NULL AND TRIM(kode_fg) != ''
                    GROUP BY kdtk, nonota
                ) v ON v.kdtk = r.kdtk AND v.nonota = r.nonota
                LEFT JOIN rekap_selisih_bk_actions act
                       ON act.kdtk = r.kdtk AND act.nonota = r.nonota
                WHERE r.leader_recommendation = 'Follow Up PIC'
                  AND r.kdcab IS NOT NULL AND r.kdcab != ''
                  AND COALESCE(act.tindakan, 'Belum Diproses') = 'Belum Diproses'
                  AND COALESCE(act.status, 'Open') = 'Open'
                ORDER BY r.kdcab, r.nilai_selisih DESC
            """)
            rows = cur.fetchall()
        by_cab: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            r["total_nilai_selisih"] = float(r["total_nilai_selisih"] or 0)
            r["aging_days"] = int(r["aging_days"] or 0)
            by_cab.setdefault(r["kdcab"], []).append(r)
        return by_cab

    def _get_targets(self, cur, kdcab: str) -> List[str]:
        cur.execute("SELECT wa_target FROM agent_pic_contacts WHERE kdcab = %s AND active = 1 ORDER BY id", [kdcab])
        return [r["wa_target"] for r in cur.fetchall()]

    def _recently_blasted(self, cur, kdcab: str, target: str) -> bool:
        cur.execute("""
            SELECT 1 FROM agent_blast_log
            WHERE kdcab = %s AND wa_target = %s AND status = 'SENT'
              AND sent_at >= NOW() - INTERVAL %s HOUR
            LIMIT 1
        """, [kdcab, target, self.BLAST_COOLDOWN_HOURS])
        return cur.fetchone() is not None

    def build_blast_preview(self, office_conn) -> str:
        """Pesan preview buat Rendra. TIDAK mengirim apa pun ke PIC."""
        with office_conn.cursor() as cur:
            self._ensure_blast_tables(cur)
            office_conn.commit()
            cur.execute("SELECT MAX(updated_at) AS last_review FROM agent_case_reviews")
            last_review = (cur.fetchone() or {}).get("last_review")
        by_cab = self.fetch_chaser_cases(office_conn)
        if not by_cab:
            return "✅ Ga ada kasus 'Follow Up PIC' yang masih nyangkut. Aman Bos, ga ada yang perlu di-blast."

        lines = ["📢 *PREVIEW BLAST PIC* (belum ada yang terkirim)", ""]
        if last_review and last_review.date() < __import__("datetime").date.today():
            lines += [f"⚠️ Audit Leader terakhir {last_review:%d %b %H:%M}. Ketik `RUN MORNING` dulu kalau mau data hari ini.", ""]

        total_nota = total_nilai = 0
        sendable = no_contact = on_cooldown = 0
        biggest = None
        with office_conn.cursor() as cur:
            for kdcab in sorted(by_cab, key=lambda k: -sum(c["total_nilai_selisih"] for c in by_cab[k])):
                cases = by_cab[kdcab]
                nilai = sum(c["total_nilai_selisih"] for c in cases)
                targets = self._get_targets(cur, kdcab)
                if not targets:
                    status = "❌ belum ada kontak"
                    no_contact += 1
                elif all(self._recently_blasted(cur, kdcab, t) for t in targets):
                    status = f"⏳ cooldown ({self.BLAST_COOLDOWN_HOURS} jam)"
                    on_cooldown += 1
                else:
                    status = f"✅ {len(targets)} kontak"
                    sendable += 1
                    if biggest is None:
                        biggest = (kdcab, cases)
                total_nota += len(cases)
                total_nilai += nilai
                lines.append(f"• *{kdcab}*: {len(cases)} nota, Rp {nilai:,.0f} → {status}")

        lines += ["", f"Total: {total_nota} nota (Rp {total_nilai:,.0f}) di {len(by_cab)} cabang.",
                  f"Siap kirim: {sendable} | Belum ada kontak: {no_contact} | Cooldown: {on_cooldown}"]
        if biggest:
            lines += ["", f"📝 *Contoh pesan ({biggest[0]}):*", "────────────", self.format_chaser_message_for_cabang(biggest[0], biggest[1], self.BLAST_TOP_N)]
        lines += ["", "👉 Ketik `BLAST PIC KONFIRM` buat kirim beneran."]
        if no_contact:
            lines.append("👉 Daftarin kontak: `REG PIC <KDCAB> <NOMOR/JID> <NAMA>`")
        return "\n".join(lines)

    def execute_blast(self, office_conn) -> Dict[str, Any]:
        """Kirim beneran ke semua target yang siap. Dipanggil dari background thread."""
        by_cab = self.fetch_chaser_cases(office_conn)
        result = {"sent": 0, "failed": 0, "skipped_no_contact": [], "skipped_cooldown": [], "cabang_sent": []}
        first = True
        for kdcab in sorted(by_cab, key=lambda k: -sum(c["total_nilai_selisih"] for c in by_cab[k])):
            cases = by_cab[kdcab]
            nilai = sum(c["total_nilai_selisih"] for c in cases)
            with office_conn.cursor() as cur:
                targets = self._get_targets(cur, kdcab)
            if not targets:
                result["skipped_no_contact"].append(kdcab)
                continue
            msg = self.format_chaser_message_for_cabang(kdcab, cases, self.BLAST_TOP_N)
            sent_any = False
            for target in targets:
                with office_conn.cursor() as cur:
                    if self._recently_blasted(cur, kdcab, target):
                        continue
                if not first:
                    time.sleep(random.uniform(self.BLAST_DELAY_MIN, self.BLAST_DELAY_MAX))
                first = False
                try:
                    resp = self.send_whatsapp_text(target, msg)
                    ok = isinstance(resp, dict) and "error" not in resp
                    detail = "" if ok else str(resp)[:500]
                except Exception as e:
                    ok, detail = False, str(e)[:500]
                with office_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO agent_blast_log (kdcab, wa_target, total_nota, total_nilai, status, detail)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, [kdcab, target, len(cases), nilai, "SENT" if ok else "FAILED", detail])
                    office_conn.commit()
                if ok:
                    result["sent"] += 1
                    sent_any = True
                else:
                    result["failed"] += 1
            if sent_any:
                result["cabang_sent"].append(kdcab)
            else:
                result["skipped_cooldown"].append(kdcab)
        return result

    def format_chaser_message_for_cabang(self, kdcab: str, cases: List[Dict[str, Any]], top_n: int = 10) -> str:
        total_nota = len(cases)
        total_nilai = sum(c["total_nilai_selisih"] for c in cases)
        top_cases = sorted(cases, key=lambda x: x["total_nilai_selisih"], reverse=True)[:top_n]

        lines = [
            f"Halo Tim PIC Cabang *{kdcab}* 🙏,",
            f"Berikut update pantauan *Intransit & Selisih BK* yang masih berstatus *OPEN* dan belum terdapat di data Kurang Kirim gudang:",
            f"",
            f"📌 *Total Menggantung:* {total_nota} Nota (Rp {total_nilai:,.0f})",
            f"🔥 *Top {len(top_cases)} Prioritas Konfirmasi:*",
        ]
        for i, c in enumerate(top_cases, 1):
            lines.append(
                f"{i}. *{c['kdtk']}* ({(c.get('nama_toko') or '-')[:20]}) | Nota: `{c['nonota']}` | "
                f"Tgl: {c['tgl_kirim']} ({c['aging_days']} hr) | *{c['tipe']}* Rp {c['total_nilai_selisih']:,.0f}"
            )

        lines.extend([
            "",
            "Mohon dibantu konfirmasi status penerimaan toko / bukti pendukungnya ya Pak/Bu, agar segera kami proses penyelesaiannya.",
            "_Cara balas: kirim kode toko + nomor nota + keterangan (foto layar NPB/BPB boleh), nanti otomatis tercatat._",
            "Terima kasih! 🙏"
        ])
        return "\n".join(lines)
