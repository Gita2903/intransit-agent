from typing import List, Dict, Any, Tuple, Optional

from core.exp_mapping import PENJUALAN_TUNAI_SISA_HARI
from core.llm import ask_json, llm_enabled


class LeaderAgent:
    """
    THE LEADER: Critical Auditor & Case Judge.
    Menilai bukti dari InvestigatorEmployee dan memutuskan rekomendasi tindakan:
      - Potong Batch
      - Penjualan Tunai
      - SJ Ulang
      - Beban Karyawan
      - Follow Up PIC
    """

    @staticmethod
    def _shelf_note(shelf: Dict[str, Any]) -> str:
        """Catatan singkat soal item yang belum ke-mapping, biar kelihatan kalau mapping bolong."""
        if not shelf or not shelf.get("mapping_ok"):
            return ""
        unk = shelf.get("unknown_items") or []
        if unk:
            return f"(Umur produk belum ke-mapping: {', '.join(unk)[:80]})"
        return ""

    @staticmethod
    def _classify_claim_with_llm(klaim: str, kdtk: str, tgl_kirim: str, items: str) -> Optional[Dict[str, Any]]:
        """Analisa klaim PIC bebas via LLM model Leader (role='leader')."""
        if not llm_enabled() or not klaim.strip():
            return None
        prompt = f"""
        Kamu adalah AI Lead Auditor untuk rekonsiliasi pengiriman logistik toko Indomaret.
        Ada laporan klaim dari PIC Toko/Cabang untuk nota yang TIDAK ADA di data kurang_kirim gudang:
        - Kode Toko: {kdtk}
        - Tanggal Kirim: {tgl_kirim}
        - Item Dilaporkan: {items}
        - Teks Klaim PIC: "{klaim}"

        Klasifikasikan klaim ini ke salah satu kategori berikut:
        1. "plu_mati": PLU tidak aktif, tidak terdaftar, atau mati di sistem toko (SOP: SJ Ulang).
        2. "salah_picking": Kesalahan gudang salah ambil barang / salah isi keranjang (SOP: Beban Karyawan).
        3. "salah_driver": Kesalahan sopir salah turun toko / ketinggalan / serah terima selisih (SOP: Beban Karyawan).
        4. "barang_rusak": Barang basi, kemasan pecah, penyok saat jalan (SOP: Beban Karyawan).
        5. "lainnya": Tidak cocok pola di atas atau kasus baru.

        PAGAR KETAT:
        - Tindakan HANYA boleh salah satu dari: ["SJ Ulang", "Beban Karyawan", "Potong Batch", "Penjualan Tunai", "Follow Up PIC"]
        - Confidence MAKSIMAL 85 untuk klaim tanpa bukti fisik gudang.
        - Jika tidak yakin, pilih kategori "lainnya" dan tindakan "Follow Up PIC".

        Balas HANYA JSON murni (tanpa markdown, tanpa penjelasan):
        {{
          "kategori": "...",
          "tindakan": "...",
          "confidence": 75,
          "alasan": "ringkasan analisa 1-2 kalimat"
        }}
        """
        try:
            return ask_json(prompt, role="leader")
        except Exception:
            return None

    def judge_single_case(self, case: Dict[str, Any], precedents: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, int, str]:
        kdtk = case["kdtk"]
        tipe = case["tipe"]
        aging = case["aging_days"]
        items = case["items_str"]

        # 1. ADA DI TABEL kurang_kirim (Confirm Barang Ga Kekirim)
        if case["in_kurang_kirim"]:
            kk = case["kk_data"] or {}
            lampiran = kk.get("lampiran") or "tanpa_nama_file"
            match_type = case["kk_match_type"]

            base_conf = 95 if match_type == "EXACT_SJ" else 85
            match_info = (
                f"Match 100% (Toko+Tgl+NoSJ '{kk.get('nomor_surat_jalan')}')"
                if match_type == "EXACT_SJ"
                else f"Match Toko+Tgl Kirim (NoSJ di kurang_kirim tertulis '{kk.get('nomor_surat_jalan')}')"
            )

            shelf = case.get("shelf") or {}
            shelf_note = self._shelf_note(shelf)

            # Semua item di nota sisa umurnya pas (default 1 hari) -> Penjualan Tunai.
            # Sengaja BUKAN Potong Batch, jadi ga ikut auto-approve / ACC PB: lewat CEK REVIEW + SET.
            if shelf.get("penjualan_tunai_all"):
                return (
                    "Penjualan Tunai",
                    base_conf,
                    f"[CONFIRM KURANG KIRIM] {match_info}, Lampiran: {lampiran}. "
                    f"Barang balik dengan sisa umur {PENJUALAN_TUNAI_SISA_HARI} hari ({', '.join(shelf['sisa_1'])[:120]}). "
                    f"Sesuai aturan: Penjualan Tunai ke karyawan."
                )

            # Campuran: sebagian item sisa umur pas, sebagian lagi enggak. Tindakan cuma satu per nota,
            # jadi turunin confidence (< 90) biar ga auto-approve dan Rendra yang mutusin.
            if shelf.get("penjualan_tunai_mixed"):
                return (
                    "Potong Batch",
                    min(base_conf, 75),
                    f"[CONFIRM KURANG KIRIM] {match_info}, Lampiran: {lampiran}. "
                    f"NOTA CAMPURAN: sisa umur {PENJUALAN_TUNAI_SISA_HARI} hari = {', '.join(shelf['sisa_1'])[:100]}; "
                    f"sisanya lebih lama/expired/belum ke-mapping. Cek manual: Potong Batch atau Penjualan Tunai. {shelf_note}"
                )

            # Ada item yang sudah lewat umur: jangan diam-diam di-auto-approve.
            if shelf.get("expired"):
                return (
                    "Potong Batch",
                    min(base_conf, 75),
                    f"[CONFIRM KURANG KIRIM] {match_info}, Lampiran: {lampiran}. "
                    f"PERHATIAN: item sudah lewat umur ({', '.join(shelf['expired'])[:120]}). Cek fisik sebelum Potong Batch. {shelf_note}"
                )

            return (
                "Potong Batch",
                base_conf,
                f"[CONFIRM KURANG KIRIM] {match_info}, Lampiran: {lampiran}. "
                f"Barang terbukti tidak terkirim ke toko {kdtk}, kembalikan ke stok gudang (Potong Batch). {shelf_note}".strip()
            )

        # 2. TIDAK ADA DI kurang_kirim, TAPI ADA FEEDBACK WA PIC
        if case["wa_feedback"]:
            wa = case["wa_feedback"]
            klaim = str(wa.get("klaim_pic") or "").strip()
            klaim_lower = klaim.lower()
            pic_name = wa.get("pic_sender") or "PIC"

            # 2a. Cek preseden keputusan Rendra sebelumnya
            if precedents:
                for p in precedents:
                    kw = str(p.get("kata_kunci") or "").strip().lower()
                    if kw and kw in klaim_lower:
                        return (
                            p["tindakan"],
                            88,
                            f"[PRESEDEN #{p['id']}] Cocok pola '{p['kata_kunci']}': {p['alasan']} (Preseden Rendra)."
                        )

            # 2b. Cek via Leader LLM (jika aktif)
            llm_res = self._classify_claim_with_llm(klaim, kdtk, case.get("tgl_kirim", ""), items)
            if llm_res:
                kat = str(llm_res.get("kategori") or "").lower()
                valid_tindakan = {"Potong Batch", "SJ Ulang", "Beban Karyawan", "Penjualan Tunai", "Follow Up PIC"}
                tindakan = llm_res.get("tindakan") if llm_res.get("tindakan") in valid_tindakan else "SJ Ulang"
                conf = min(int(llm_res.get("confidence") or 75), 85)  # fence: LLM tidak pernah auto-approved
                alasan = str(llm_res.get("alasan") or "").strip()

                if kat == "plu_mati":
                    return ("SJ Ulang", min(conf, 88), f"[FEEDBACK WA {pic_name} - PLU MATI] {alasan}")
                if kat in ("salah_picking", "salah_driver", "barang_rusak"):
                    return ("Beban Karyawan", min(conf, 85), f"[FEEDBACK WA {pic_name} - INDIKASI KELALAIAN] {alasan}. Rekomendasi: Beban Karyawan (perlu verifikasi bukti/Rendra).")
                if kat == "lainnya" or conf < 70:
                    return ("Follow Up PIC", 70, f"[KASUS BARU] {pic_name} lapor: '{klaim[:100]}'. Analisa Leader: {alasan or 'Pola klaim belum ada di SOP/preseden'}. Butuh arahan Rendra (balas: PRESEDEN <KDTK> <NONOTA> <Tindakan> <Alasan>).")
                return (tindakan, conf, f"[FEEDBACK WA {pic_name}] {alasan}")

            # 2c. Fallback jika LLM mati / gagal: keyword matching
            if "plu" in klaim_lower and ("tidak aktif" in klaim_lower or "mati" in klaim_lower or "ga aktif" in klaim_lower):
                return (
                    "SJ Ulang",
                    92,
                    f"[FEEDBACK WA {pic_name}] Konfirmasi PLU tidak aktif di toko {kdtk}. "
                    f"Sesuai SOP, buatkan SJ Ulang ke toko/PLU aktif."
                )

            if "driver" in klaim_lower or "picking" in klaim_lower or "serah terima" in klaim_lower:
                return (
                    "Beban Karyawan",
                    85,
                    f"[FEEDBACK WA {pic_name}] Toko klaim selisih dengan indikasi kesalahan tim picking/driver, "
                    f"namun belum tercatat di kurang_kirim. Rekomendasi: Beban Karyawan (perlu verifikasi bukti {wa.get('bukti_foto_path')})."
                )

            return (
                "SJ Ulang",
                80,
                f"[ANOMALI KLAIM TOKO] {pic_name} lapor via WA ('{wa.get('klaim_pic')}'), "
                f"TAPI toko {kdtk} tanggal {case['tgl_kirim']} TIDAK TERDAFTAR di tabel kurang_kirim gudang! "
                f"Karena bukti kurang kirim dari gudang tidak ada, rekomendasikan 'SJ Ulang' agar toko proses ulang, "
                f"atau konfirmasi tim gudang apakah lupa input ke kurang_kirim."
            )

        # 3. KASUS GELAP (Gantung Tanpa kurang_kirim & Tanpa WA)
        urgency = "KRITIS" if aging > 14 or case["total_nilai_selisih"] >= 1_000_000 else "NORMAL"
        return (
            "Follow Up PIC",
            100,
            f"[{urgency} - AGING {aging} HARI] Status masih {tipe} senilai Rp {case['total_nilai_selisih']:,.0f}. "
            f"Tidak ada di tabel kurang_kirim dan belum ada konfirmasi WA dari PIC Cabang {case['kdcab']}."
        )

    def review_all_cases_and_save(self, enriched_cases: List[Dict[str, Any]], office_conn) -> Dict[str, Any]:
        reviews_to_save = []
        summary = {
            "total_evaluated": len(enriched_cases),
            "ready_potong_batch": [],
            "need_manager_attention": [],
            "chaser_by_cabang": {},
        }

        precedents = []
        try:
            with office_conn.cursor() as cur:
                cur.execute("SELECT id, kata_kunci, kategori, tindakan, alasan FROM agent_precedents ORDER BY id ASC")
                precedents = cur.fetchall() or []
        except Exception:
            precedents = []

        for c in enriched_cases:
            rec_action, conf, reason = self.judge_single_case(c, precedents=precedents)
            c["leader_recommendation"] = rec_action
            c["confidence_score"] = conf
            c["leader_reasoning"] = reason

            if rec_action == "Potong Batch" and conf >= 90:
                mgr_status = "AUTO_APPROVED"
                summary["ready_potong_batch"].append(c)
            elif rec_action == "Follow Up PIC":
                mgr_status = "PENDING_APPROVAL"
                summary["chaser_by_cabang"].setdefault(c["kdcab"], []).append(c)
            else:
                mgr_status = "PENDING_APPROVAL"
                summary["need_manager_attention"].append(c)

            reviews_to_save.append({
                "kdtk": c["kdtk"],
                "nonota": c["nonota"],
                "kdcab": c["kdcab"],
                "tipe_masalah": c["tipe"],
                "aging_days": c["aging_days"],
                "nilai_selisih": c["total_nilai_selisih"],
                "in_kurang_kirim": 1 if c["in_kurang_kirim"] else 0,
                "leader_recommendation": rec_action,
                "confidence_score": conf,
                "leader_reasoning": reason,
                "manager_decision": mgr_status,
            })

        if reviews_to_save:
            with office_conn.cursor() as cur:
                sql = """
                    INSERT INTO agent_case_reviews
                        (kdtk, nonota, kdcab, tipe_masalah, aging_days, nilai_selisih,
                         in_kurang_kirim, leader_recommendation, confidence_score,
                         leader_reasoning, manager_decision)
                    VALUES
                        (%(kdtk)s, %(nonota)s, %(kdcab)s, %(tipe_masalah)s, %(aging_days)s,
                         %(nilai_selisih)s, %(in_kurang_kirim)s, %(leader_recommendation)s,
                         %(confidence_score)s, %(leader_reasoning)s, %(manager_decision)s)
                    ON DUPLICATE KEY UPDATE
                        aging_days = VALUES(aging_days),
                        nilai_selisih = VALUES(nilai_selisih),
                        in_kurang_kirim = VALUES(in_kurang_kirim),
                        leader_recommendation = VALUES(leader_recommendation),
                        confidence_score = VALUES(confidence_score),
                        leader_reasoning = VALUES(leader_reasoning),
                        manager_decision = IF(
                            agent_case_reviews.manager_decision = 'APPROVED_BY_RENDRA',
                            'APPROVED_BY_RENDRA',
                            VALUES(manager_decision)
                        )
                """
                cur.executemany(sql, reviews_to_save)
                office_conn.commit()

        return summary

    def execute_approved_actions_to_db(self, approved_cases: List[Dict[str, Any]], office_conn) -> int:
        """
        Menembak langsung ke tabel `rekap_selisih_bk_actions` dengan status = 'Open'
        sampai Berita Acara (ba_rekonsiliasi) resmi terbit.
        """
        valid_tindakan = {"Potong Batch", "SJ Ulang", "Beban Karyawan", "Penjualan Tunai"}
        payloads = []

        for c in approved_cases:
            tindakan = c.get("tindakan") or c.get("leader_recommendation")
            if tindakan not in valid_tindakan:
                continue
            payloads.append({
                "kdtk": c["kdtk"],
                "nonota": c["nonota"],
                "tindakan": tindakan,
                "keterangan": c.get("leader_reasoning", "")[:250],
                "status": "Open",
                "updated_by": "Argon-LeaderAgent",
            })

        if not payloads:
            return 0

        with office_conn.cursor() as cur:
            sql_action = """
                INSERT INTO rekap_selisih_bk_actions
                    (kdtk, nonota, tindakan, keterangan, status, updated_by, created_at, updated_at)
                VALUES
                    (%(kdtk)s, %(nonota)s, %(tindakan)s, %(keterangan)s, %(status)s, %(updated_by)s, NOW(), NOW())
                ON DUPLICATE KEY UPDATE
                    tindakan = VALUES(tindakan),
                    keterangan = VALUES(keterangan),
                    status = VALUES(status),
                    updated_by = VALUES(updated_by),
                    updated_at = NOW()
            """
            cur.executemany(sql_action, payloads)
            office_conn.commit()

        return len(payloads)
