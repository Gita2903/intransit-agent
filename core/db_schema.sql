-- ============================================================================
-- SKEMA DATABASE TAMBAHAN UNTUK AI AGENT (DIJALANKAN DI MYSQL KANTOR)
-- ============================================================================

-- 1. Tabel Mirror dari Home Lab (Auto-Sync tiap hari dari MariaDB kurang_kirim)
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

-- 2. Tabel Waiting List (Laporan WA PIC yang mendahului data Rekap H-1)
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

-- 3. Tabel Meja Kerja Leader & Manager (Audit & Rekomendasi AI)
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

-- 4. Kontak PIC per cabang (tujuan BLAST PIC). Isi lewat WA: REG PIC <KDCAB> <NOMOR/JID> <NAMA>
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

-- 5. Log setiap pesan BLAST PIC (dipakai buat cooldown anti-spam)
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

-- 6. Log override keputusan Rendra vs rekomendasi Leader (bahan evaluasi akurasi)
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

-- 7. Preseden kasus khusus yang diputuskan Rendra (rujukan Leader berikutnya)
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
