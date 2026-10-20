import os
import re
from datetime import date, datetime
from typing import List, Dict, Any, Optional
import pymysql
import pymysql.cursors
from dotenv import load_dotenv

from core.exp_mapping import get_mapping

load_dotenv()


class InvestigatorEmployee:
    """
    EMPLOYEE 2: Database Sync, Evidence Cross-Checker & Case Enricher.
    Menghubungkan DB Home Lab (kurang_kirim) dengan DB Kantor (v_rekap_selisih_intransit).
    """

    SHORT_EXPIRY_KEYWORDS = [
        "bun burger", "beef patty", "chicken patty", "say burger",
        "vegetable cbn", "pickle timun", "signature sauce", "caramelized onion",
        "cheese slice", "po ", "onigiri", "nasi", "dimsum", "steam pao", "siomay"
    ]

    def __init__(self):
        self.office_db_cfg = {
            "host": os.environ.get("OFFICE_DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("OFFICE_DB_PORT", 3306)),
            "user": os.environ.get("OFFICE_DB_USER", "root"),
            "password": os.environ.get("OFFICE_DB_PASS", ""),
            "database": os.environ.get("OFFICE_DB_NAME", "intbk_db"),
            "cursorclass": pymysql.cursors.DictCursor,
        }
        self.homelab_db_cfg = {
            "host": os.environ.get("HOMELAB_DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("HOMELAB_DB_PORT", 3306)),
            "user": os.environ.get("HOMELAB_DB_USER", "root"),
            "password": os.environ.get("HOMELAB_DB_PASS", ""),
            "database": os.environ.get("HOMELAB_DB_NAME", "kurang_kirim"),
            "cursorclass": pymysql.cursors.DictCursor,
        }

    def get_office_conn(self):
        return pymysql.connect(**self.office_db_cfg)

    def get_homelab_conn(self):
        return pymysql.connect(**self.homelab_db_cfg)

    @staticmethod
    def extract_clean_sj_last4(raw_sj: Optional[str]) -> str:
        if not raw_sj:
            return ""
        digits = re.findall(r"\d+", str(raw_sj))
        if not digits:
            return ""
        last_num = digits[-1]
        return last_num.zfill(4)[-4:]

    def sync_kurang_kirim_from_homelab(self, full_sync: bool = False) -> Dict[str, Any]:
        office_conn = self.get_office_conn()
        try:
            with office_conn.cursor() as ocur:
                self._ensure_agent_tables(ocur)
                office_conn.commit()

                last_id = 0
                if not full_sync:
                    ocur.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM kurang_kirim_sync")
                    row = ocur.fetchone()
                    last_id = int(row["max_id"] or 0)

            homelab_conn = self.get_homelab_conn()
            try:
                with homelab_conn.cursor() as hcur:
                    if full_sync or last_id == 0:
                        hcur.execute("""
                            SELECT id, toko_id, kode_toko, tgl_kirim, nomor_surat_jalan,
                                   lampiran, status, created_at
                            FROM kurang_kirim
                            ORDER BY id ASC
                        """)
                    else:
                        hcur.execute("""
                            SELECT id, toko_id, kode_toko, tgl_kirim, nomor_surat_jalan,
                                   lampiran, status, created_at
                            FROM kurang_kirim
                            WHERE id > %s OR created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY)
                            ORDER BY id ASC
                        """, [last_id])
                    kk_rows = hcur.fetchall()
            finally:
                homelab_conn.close()

            if not kk_rows:
                return {"status": "ok", "synced_rows": 0, "last_id": last_id}

            upsert_payload = []
            for r in kk_rows:
                upsert_payload.append({
                    "id": r["id"],
                    "kode_toko": str(r["kode_toko"] or "").strip().upper(),
                    "tgl_kirim": r["tgl_kirim"],
                    "nomor_surat_jalan": r["nomor_surat_jalan"],
                    "sj_last4": self.extract_clean_sj_last4(r["nomor_surat_jalan"]),
                    "lampiran": r["lampiran"],
                    "status": int(r["status"] if r["status"] is not None else 1),
                    "created_at": r["created_at"],
                })

            with office_conn.cursor() as ocur:
                sql_upsert = """
                    INSERT INTO kurang_kirim_sync
                        (id, kode_toko, tgl_kirim, nomor_surat_jalan, sj_last4, lampiran, status, created_at)
                    VALUES
                        (%(id)s, %(kode_toko)s, %(tgl_kirim)s, %(nomor_surat_jalan)s,
                         %(sj_last4)s, %(lampiran)s, %(status)s, %(created_at)s)
                    ON DUPLICATE KEY UPDATE
                        kode_toko = VALUES(kode_toko),
                        tgl_kirim = VALUES(tgl_kirim),
                        nomor_surat_jalan = VALUES(nomor_surat_jalan),
                        sj_last4 = VALUES(sj_last4),
                        lampiran = VALUES(lampiran),
                        status = VALUES(status)
                """
                ocur.executemany(sql_upsert, upsert_payload)
                office_conn.commit()

            return {
                "status": "ok",
                "synced_rows": len(upsert_payload),
                "max_id": max(r["id"] for r in upsert_payload)
            }
        finally:
            office_conn.close()

    def fetch_and_enrich_open_cases(self) -> List[Dict[str, Any]]:
        conn = self.get_office_conn()
        try:
            with conn.cursor() as cur:
                self._ensure_agent_tables(cur)
                conn.commit()

                sql_cases = """
                    SELECT
                        v.tipe,
                        v.kdcab,
                        v.kdtk,
                        v.nama_toko,
                        v.nonota,
                        v.tgl_kirim,
                        DATEDIFF(CURDATE(), v.tgl_kirim) AS aging_days,
                        SUM(v.qty_sj) AS total_qty_sj,
                        SUM(v.nilai_sj) AS total_nilai_sj,
                        SUM(v.qty_selisih) AS total_qty_selisih,
                        SUM(v.nilai_selisih) AS total_nilai_selisih,
                        GROUP_CONCAT(DISTINCT v.nama_produk SEPARATOR ', ') AS items_str,
                        GROUP_CONCAT(DISTINCT CONCAT(v.kode_fg, '||', COALESCE(v.nama_produk, '')) SEPARATOR '##') AS items_raw,
                        COALESCE(act.tindakan, 'Belum Diproses') AS current_tindakan,
                        COALESCE(act.status, 'Open') AS current_status
                    FROM v_rekap_selisih_intransit v
                    LEFT JOIN rekap_selisih_bk_actions act
                        ON act.kdtk = v.kdtk AND act.nonota = v.nonota
                    WHERE v.kode_fg IS NOT NULL
                      AND TRIM(v.kode_fg) != ''
                      AND COALESCE(act.status, 'Open') = 'Open'
                      AND COALESCE(act.tindakan, 'Belum Diproses') = 'Belum Diproses'
                    GROUP BY v.tipe, v.kdcab, v.kdtk, v.nama_toko, v.nonota, v.tgl_kirim,
                             act.tindakan, act.status
                    ORDER BY v.tgl_kirim ASC, v.nonota ASC
                """
                cur.execute(sql_cases)
                open_notas = cur.fetchall()

                if not open_notas:
                    return []

                cur.execute("""
                    SELECT id, kode_toko, tgl_kirim, nomor_surat_jalan, sj_last4, lampiran
                    FROM kurang_kirim_sync
                    WHERE status = 1
                """)
                kk_exact_map = {}
                kk_store_date_map = {}
                for kk in cur.fetchall():
                    tgl_str = str(kk["tgl_kirim"])[:10]
                    k_toko = str(kk["kode_toko"]).strip().upper()
                    sj4 = str(kk["sj_last4"] or "")
                    if sj4:
                        kk_exact_map[(k_toko, tgl_str, sj4)] = kk
                    kk_store_date_map[(k_toko, tgl_str)] = kk

                cur.execute("""
                    SELECT id, pic_sender, kdcab, kdtk, nonota, tgl_kirim,
                           item_dilaporkan, klaim_pic, bukti_foto_path
                    FROM agent_waiting_list
                    WHERE status_antrean IN ('WAITING_H1_DATA', 'MATCHED_TO_REKAP')
                """)
                wl_by_nota = {}
                wl_by_store_date = {}
                for wl in cur.fetchall():
                    k_toko = str(wl["kdtk"] or "").strip().upper()
                    nota_wl = str(wl["nonota"] or "").strip()
                    tgl_wl = str(wl["tgl_kirim"] or "")[:10]
                    if nota_wl:
                        wl_by_nota[(k_toko, nota_wl)] = wl
                    if tgl_wl:
                        wl_by_store_date[(k_toko, tgl_wl)] = wl

                exp_map = get_mapping()
                enriched_cases = []
                for row in open_notas:
                    kdtk = str(row["kdtk"] or "").strip().upper()
                    nonota = str(row["nonota"] or "").strip()
                    last4_nota = nonota[-4:].zfill(4) if len(nonota) >= 4 else nonota
                    tgl_str = str(row["tgl_kirim"])[:10] if row["tgl_kirim"] else ""
                    items_str = str(row["items_str"] or "")

                    kk_match = kk_exact_map.get((kdtk, tgl_str, last4_nota))
                    match_type = "EXACT_SJ" if kk_match else None

                    if not kk_match:
                        kk_fallback = kk_store_date_map.get((kdtk, tgl_str))
                        if kk_fallback:
                            kk_match = kk_fallback
                            match_type = "STORE_DATE_ONLY"

                    wa_match = wl_by_nota.get((kdtk, nonota)) or wl_by_store_date.get((kdtk, tgl_str))

                    items_lower = items_str.lower()
                    shelf = exp_map.analyze_case(str(row.get("items_raw") or ""), tgl_str)
                    if exp_map.loaded:
                        # Berdasarkan mapping umur produk: "pendek" = ada item yang sisa umurnya pas Penjualan Tunai
                        is_short_exp = shelf["penjualan_tunai_all"] or shelf["penjualan_tunai_mixed"]
                    else:
                        # mapping ga ke-load -> fallback ke tebakan keyword lama
                        is_short_exp = any(kw in items_lower for kw in self.SHORT_EXPIRY_KEYWORDS)
                    is_preorder = any(x.strip().lower().startswith("po ") for x in items_str.split(","))

                    enriched_cases.append({
                        "tipe": row["tipe"],
                        "kdcab": row["kdcab"],
                        "kdtk": kdtk,
                        "nama_toko": row["nama_toko"],
                        "nonota": nonota,
                        "tgl_kirim": tgl_str,
                        "aging_days": int(row["aging_days"] or 0),
                        "total_qty_sj": int(row["total_qty_sj"] or 0),
                        "total_nilai_sj": float(row["total_nilai_sj"] or 0.0),
                        "total_qty_selisih": int(row["total_qty_selisih"] or 0),
                        "total_nilai_selisih": float(row["total_nilai_selisih"] or 0.0),
                        "items_str": items_str,
                        "is_short_expiry": is_short_exp,
                        "shelf": shelf,
                        "is_preorder": is_preorder,
                        "in_kurang_kirim": kk_match is not None,
                        "kk_match_type": match_type,
                        "kk_data": kk_match,
                        "wa_feedback": wa_match,
                    })

                return enriched_cases
        finally:
            conn.close()

    def _ensure_agent_tables(self, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `kurang_kirim_sync` (
              `id` int UNSIGNED NOT NULL,
              `kode_toko` varchar(50) NOT NULL,
              `tgl_kirim` date NOT NULL,
              `nomor_surat_jalan` varchar(100) DEFAULT NULL,
              `sj_last4` varchar(10) DEFAULT '',
              `lampiran` varchar(255) DEFAULT NULL,
              `status` tinyint NOT NULL DEFAULT 1,
              `created_at` timestamp NULL DEFAULT NULL,
              `synced_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_kk_lookup` (`kode_toko`, `tgl_kirim`, `sj_last4`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `agent_waiting_list` (
              `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
              `pic_sender` varchar(150) DEFAULT NULL,
              `kdcab` varchar(10) DEFAULT NULL,
              `kdtk` varchar(20) NOT NULL,
              `nonota` varchar(30) DEFAULT NULL,
              `tgl_kirim` date DEFAULT NULL,
              `item_dilaporkan` text DEFAULT NULL,
              `klaim_pic` text DEFAULT NULL,
              `bukti_foto_path` text DEFAULT NULL,
              `status_antrean` enum('WAITING_H1_DATA','MATCHED_TO_REKAP','RESOLVED','INVALID') DEFAULT 'WAITING_H1_DATA',
              `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_wl_match` (`kdtk`, `nonota`, `status_antrean`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `agent_case_reviews` (
              `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
              `kdtk` varchar(20) NOT NULL,
              `nonota` varchar(30) NOT NULL,
              `kdcab` varchar(10) DEFAULT NULL,
              `tipe_masalah` varchar(30) NOT NULL,
              `aging_days` int DEFAULT 0,
              `nilai_selisih` decimal(15,2) DEFAULT 0,
              `in_kurang_kirim` tinyint(1) DEFAULT 0,
              `leader_recommendation` varchar(50) NOT NULL,
              `confidence_score` int DEFAULT 0,
              `leader_reasoning` text NOT NULL,
              `manager_decision` enum('PENDING_APPROVAL','AUTO_APPROVED','APPROVED_BY_RENDRA','REJECTED_BY_LEADER') DEFAULT 'PENDING_APPROVAL',
              `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
              `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              UNIQUE KEY `uk_case_review` (`kdtk`, `nonota`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
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
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
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
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `agent_overrides` (
              `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
              `kdtk` varchar(20) NOT NULL,
              `nonota` varchar(30) NOT NULL,
              `leader_recommendation` varchar(50) NOT NULL,
              `confidence_score` int DEFAULT 0,
              `leader_reasoning` text,
              `rendra_decision` varchar(50) NOT NULL,
              `override_source` varchar(30) DEFAULT 'WA_SET',
              `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_override_lookup` (`kdtk`, `nonota`),
              KEY `idx_override_created` (`created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `agent_precedents` (
              `id` int UNSIGNED NOT NULL AUTO_INCREMENT,
              `kata_kunci` varchar(100) NOT NULL,
              `kategori` varchar(50) DEFAULT 'kasus_khusus',
              `tindakan` varchar(50) NOT NULL,
              `alasan` text NOT NULL,
              `contoh_kasus` varchar(100) DEFAULT NULL,
              `created_by` varchar(50) DEFAULT 'Rendra',
              `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_prec_keyword` (`kata_kunci`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("CREATE OR REPLACE VIEW v_rekap_selisih_intransit AS SELECT * FROM rekap_selisih_bk")
