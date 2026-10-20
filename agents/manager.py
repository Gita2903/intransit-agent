from typing import Dict, Any
from datetime import datetime
from agents.employees.emp_pipeline import PipelineEmployee
from agents.employees.emp_investigator import InvestigatorEmployee
from agents.employees.emp_communicator import CommunicatorEmployee
from agents.leader import LeaderAgent


class ManagerAgent:
    """
    THE MANAGER: Chief of Operations untuk Rendra.
    Mengorkestrasi Employee 1, 2, 3 dan Leader Agent, lalu mengirim Executive Summary ke WA Rendra.
    """

    def __init__(self, scripts_dir: str = "./tools"):
        self.emp_pipeline = PipelineEmployee(scripts_dir=scripts_dir)
        self.emp_investigator = InvestigatorEmployee()
        self.emp_comm = CommunicatorEmployee()
        self.leader = LeaderAgent()

    def run_morning_workflow(self, auto_execute_high_confidence: bool = False, skip_wa_send: bool = False) -> Dict[str, Any]:
        pipe_res = self.emp_pipeline.run_morning_ftp_and_excel_pipeline()
        kk_sync_res = self.emp_investigator.sync_kurang_kirim_from_homelab(full_sync=False)

        office_conn = self.emp_investigator.get_office_conn()
        try:
            db_sync_res = self.emp_pipeline.pull_detail_rekap_and_sync_to_db(office_conn)
            enriched_cases = self.emp_investigator.fetch_and_enrich_open_cases()
            leader_summary = self.leader.review_all_cases_and_save(enriched_cases, office_conn)

            executed_count = 0
            if auto_execute_high_confidence and leader_summary["ready_potong_batch"]:
                executed_count = self.leader.execute_approved_actions_to_db(
                    leader_summary["ready_potong_batch"], office_conn
                )

            briefing_text = self._build_wa_briefing(
                pipe_res, kk_sync_res, db_sync_res, leader_summary, executed_count
            )
            if not skip_wa_send and self.emp_comm.owner_wa:
                self.emp_comm.send_whatsapp_text(self.emp_comm.owner_wa, briefing_text)

            return {
                "pipeline": pipe_res,
                "kk_sync": kk_sync_res,
                "db_sync": db_sync_res,
                "leader_summary": {
                    "total_evaluated": leader_summary["total_evaluated"],
                    "ready_potong_batch_count": len(leader_summary["ready_potong_batch"]),
                    "need_attention_count": len(leader_summary["need_manager_attention"]),
                    "executed_to_actions_table": executed_count,
                },
                "briefing_message": briefing_text,
            }
        finally:
            office_conn.close()

    def _build_wa_briefing(self, pipe_res, kk_sync, db_sync, summary, executed_count) -> str:
        ready_pb = summary["ready_potong_batch"]
        need_att = summary["need_manager_attention"]
        chaser = summary["chaser_by_cabang"]

        pb_val = sum(c["total_nilai_selisih"] for c in ready_pb)
        att_val = sum(c["total_nilai_selisih"] for c in need_att)
        chaser_count = sum(len(v) for v in chaser.values())
        chaser_val = sum(sum(c["total_nilai_selisih"] for c in v) for v in chaser.values())
        kasus_baru_count = sum(1 for c in need_att if "[KASUS BARU]" in c.get("leader_reasoning", ""))
        kb_tag = f" [🔥 {kasus_baru_count} KASUS BARU]" if kasus_baru_count > 0 else ""
        if db_sync.get("status") == "error":
            db_sync_str = f"⚠️ {db_sync.get('error', 'Gagal update')} (Audit pakai data lokal)"
        else:
            db_sync_str = f"{db_sync.get('rows_upserted', 0)} baris di-update"

        def _ftp_status(ok_key: str, missing_key: str, count_key: str) -> str:
            if ok_key not in pipe_res:
                return "⚠️ Belum dijalankan"
            if pipe_res.get(ok_key) is False:
                return "❌ Script gagal jalan (cek stderr di log pipeline)"
            missing = pipe_res.get(missing_key, [])
            if not missing:
                return f"✅ Lengkap ({pipe_res.get(count_key, 22)} file)"
            return f"⚠️ Kurang {len(missing)} file ({', '.join(missing[:5])})"

        ftp_214_str = _ftp_status("ftp_214_ok", "ftp_214_missing_files", "ftp_214_downloaded_count")
        ftp_status_str = _ftp_status("ftp_215_ok", "ftp_215_missing_files", "ftp_215_downloaded_count")

        msg = [
            f"🤖 *ARGON MULTI-AGENT MORNING BRIEFING*",
            f"📅 {datetime.now().strftime('%d %b %Y %H:%M WIB')}",
            f"────────────────────────",
            f"⚙️ *1. Status Pipeline & Database (Employee 1 & 2)*",
            f"• Sync IIA & Upload CSV FTP: {'✅ OK' if pipe_res.get('csv_sync_ok', pipe_res.get('pipeline_ok')) else '⚠️ Perlu Cek'} ({pipe_res.get('uploaded_csv_count', 0)} CSV)",
            f"• Pull File BK FTP 214: {ftp_214_str}",
            f"• Pull File BK FTP 215: {ftp_status_str}",
            f"• Sync DB `kurang_kirim` (Home Lab): +{kk_sync.get('synced_rows', 0)} baris baru",
            f"• Sync DB `rekap_selisih_bk`: {db_sync_str}",
            f"",
            f"🕵️‍♂️ *2. Hasil Audit Leader Agent ({summary['total_evaluated']} Nota Open)*",
            f"✅ *Confirm Kurang Kirim (Siap Potong Batch):* *{len(ready_pb)} Nota* (Rp {pb_val:,.0f})",
            f"   └ Status Eksekusi DB (`status='Open'`): {f'Sudah di-update ({executed_count} nota)' if executed_count else 'Menunggu komando *ACC PB*'}",
            f"⚠️ *Butuh Review Lo (Match Tanggal/Komplain WA):* *{len(need_att)} Nota* (Rp {att_val:,.0f}){kb_tag}",
            f"📢 *Kasus Gelap (Siap Push ke PIC Cabang):* *{chaser_count} Nota* (Rp {chaser_val:,.0f})",
            f"",
            f"💡 *Balas Chat Ini dengan Komando:*",
            f"• Ketik *`ACC PB`* → Set {len(ready_pb)} nota valid ke `Potong Batch` (Status: Open)",
            f"• Ketik *`CEK REVIEW`* → Lihat detail {len(need_att)} nota anomali/WA PIC",
            f"• Ketik *`EVALUASI`* → Lihat evaluasi akurasi Leader & override mingguan",
            f"• Ketik *`BLAST PIC`* → Preview rekap tagihan ke PIC Cabang (kirim setelah `BLAST PIC KONFIRM`)",
        ]
        return "\n".join(msg)

    def build_weekly_evaluation(self, office_conn, days: int = 7) -> str:
        """
        Evaluasi performa Agent: Rekomendasi Leader vs Keputusan Rendra (Overrides & Accuracy).
        """
        with office_conn.cursor() as cur:
            self.emp_investigator._ensure_agent_tables(cur)

            cur.execute("""
                SELECT
                    COUNT(*) as total_reviews,
                    SUM(CASE WHEN manager_decision = 'APPROVED_BY_RENDRA' THEN 1 ELSE 0 END) as approved_manual,
                    SUM(CASE WHEN manager_decision = 'AUTO_APPROVED' THEN 1 ELSE 0 END) as auto_approved
                FROM agent_case_reviews
                WHERE updated_at >= NOW() - INTERVAL %s DAY
            """, [days])
            stats = cur.fetchone() or {}
            total_reviews = int(stats.get("total_reviews") or 0)
            auto_approved = int(stats.get("auto_approved") or 0)
            approved_manual = int(stats.get("approved_manual") or 0)

            cur.execute("""
                SELECT
                    leader_recommendation,
                    rendra_decision,
                    COUNT(*) as count_override,
                    AVG(confidence_score) as avg_conf
                FROM agent_overrides
                WHERE created_at >= NOW() - INTERVAL %s DAY
                GROUP BY leader_recommendation, rendra_decision
                ORDER BY count_override DESC
            """, [days])
            override_groups = cur.fetchall() or []
            total_overrides = sum(r["count_override"] for r in override_groups)

            cur.execute("""
                SELECT kata_kunci, tindakan, alasan, contoh_kasus
                FROM agent_precedents
                WHERE created_at >= NOW() - INTERVAL %s DAY
                ORDER BY id DESC
            """, [days])
            recent_precedents = cur.fetchall() or []

        total_decisions = auto_approved + approved_manual + total_overrides
        if total_decisions > 0:
            agreed = total_decisions - total_overrides
            agreement_rate = (agreed / total_decisions) * 100
        else:
            agreement_rate = 100.0

        lines = [
            f"📊 *EVALUASI KINERJA AGENT ({days} HARI TERAKHIR)*",
            f"📅 Update: {datetime.now().strftime('%d %b %Y %H:%M WIB')}",
            f"────────────────────────",
            f"🎯 *1. Statistik Akurasi & Rekomendasi*",
            f"• Total Kasus Diproses: {total_reviews} nota",
            f"• Auto-Approved (Conf ≥ 90): {auto_approved} nota",
            f"• Disetujui Rendra (Sesuai Leader): {max(0, approved_manual - total_overrides)} nota",
            f"• Di-Override Rendra (Beda Tindakan): {total_overrides} nota",
            f"• *Agreement Rate:* *{agreement_rate:.1f}%*",
            f"",
            f"🔄 *2. Pola Perubahan/Override Terbanyak*",
        ]

        if not override_groups:
            lines.append("• Belum ada catatan override di periode ini (semua saran Leader diterima atau belum ada SET manual).")
        else:
            for og in override_groups:
                lines.append(
                    f"• Leader: `{og['leader_recommendation']}` → Rendra: *`{og['rendra_decision']}`* "
                    f"({og['count_override']}x, rata-rata conf {int(og['avg_conf'] or 0)}%)"
                )

        lines.extend([
            f"",
            f"💡 *3. Preseden Kasus Baru*",
            f"• Preseden Baru Terdaftar: {len(recent_precedents)} aturan"
        ])

        if recent_precedents:
            for p in recent_precedents[:5]:
                lines.append(f"  - Pola: *'{p['kata_kunci']}'* → `{p['tindakan']}` ({p['alasan'][:50]})")
        else:
            lines.append("  - Belum ada preseden baru yang didaftarkan.")

        lines.extend([
            f"",
            f"💡 *Saran Adaptasi Manager:*",
        ])
        if agreement_rate >= 90:
            lines.append("• Rekomendasi Leader sangat akurat (≥ 90%). Aturan SOP berjalan optimal.")
        elif any(og["leader_recommendation"] == "Potong Batch" and og["count_override"] >= 3 for og in override_groups):
            lines.append("• Perhatian: Saran 'Potong Batch' sering diubah Rendra. Cek apakah ada penyesuaian umur produk MAPPING4.xlsx.")
        elif any(og["rendra_decision"] == "Beban Karyawan" for og in override_groups):
            lines.append("• Ada override ke 'Beban Karyawan'. Pastikan bukti form investigasi toko / driver lengkap.")
        else:
            lines.append("• Evaluasi berkala membantu Leader semakin selaras dengan preferensi keputusan Rendra.")

        return "\n".join(lines)
