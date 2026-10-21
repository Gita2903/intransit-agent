-- ============================================================================
-- SKEMA DATABASE BARU KHUSUS AI AGENT & HERMES CS (intbk_agent_db)
-- Clean, independen dari admin_dashboard2, terhubung ke DB homelab kurang_kirim
-- ============================================================================

-- 1. Rekap Intransit & Selisih BK (tarik dari IIA Web / Excel)
CREATE TABLE IF NOT EXISTS `rekap_intransit` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `tipe` enum('Intransit','Selisih BK') NOT NULL,
  `kdcab` varchar(10) NOT NULL,
  `kdtk` varchar(10) NOT NULL,
  `nama_toko` varchar(150) DEFAULT NULL,
  `nonota` varchar(30) NOT NULL,
  `tgl_kirim` date NOT NULL,
  `no_urut` int DEFAULT 0,
  `kode_fg` varchar(50) NOT NULL,
  `nama_produk` varchar(200) DEFAULT NULL,
  `qty_sj` int DEFAULT 0,
  `nilai_sj` decimal(15,2) DEFAULT 0.00,
  `no_btb` varchar(50) DEFAULT NULL,
  `qty_bpb` int DEFAULT 0,
  `nilai_btb` decimal(15,2) DEFAULT 0.00,
  `qty_selisih` int DEFAULT 0,
  `nilai_selisih` decimal(15,2) DEFAULT 0.00,
  `periode_start` date DEFAULT NULL,
  `periode_end` date DEFAULT NULL,
  `source_file` varchar(150) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_item_nota` (`kdtk`, `nonota`, `kode_fg`, `no_urut`),
  KEY `idx_lookup` (`kdtk`, `nonota`, `tgl_kirim`),
  KEY `idx_tgl_kirim` (`tgl_kirim`),
  KEY `idx_kdcab` (`kdcab`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Tindakan & Status Kasus (Potong Batch, SJ Ulang, Beban Karyawan, dll)
CREATE TABLE IF NOT EXISTS `rekap_actions` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `kdtk` varchar(10) NOT NULL,
  `nonota` varchar(30) NOT NULL,
  `tindakan` varchar(50) NOT NULL DEFAULT 'Belum Diproses',
  `status` enum('Open','Resolved','Closed') NOT NULL DEFAULT 'Open',
  `keterangan` text DEFAULT NULL,
  `updated_by` varchar(50) DEFAULT 'Hermes-CS',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_action` (`kdtk`, `nonota`),
  KEY `idx_status` (`status`, `tindakan`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. Data Komplain Masuk (Dari CS / Toko via Hermes)
CREATE TABLE IF NOT EXISTS `data_komplain` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `kdtk` varchar(10) NOT NULL,
  `kdcab` varchar(10) DEFAULT NULL,
  `nonota` varchar(30) DEFAULT NULL,
  `tgl_kirim` date DEFAULT NULL,
  `item_komplain` text DEFAULT NULL,
  `keluhan` text NOT NULL,
  `pelapor` varchar(100) DEFAULT NULL,
  `status_komplain` enum('BARU','PROSES','SELESAI','DITOLAK') NOT NULL DEFAULT 'BARU',
  `tindakan_diambil` varchar(50) DEFAULT NULL,
  `catatan_cs` text DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_komplain_lookup` (`kdtk`, `nonota`, `status_komplain`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. Mirror Data Kurang Kirim dari Homelab (untuk verifikasi Potong Batch)
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
  KEY `idx_kk_match` (`kode_toko`, `tgl_kirim`, `sj_last4`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. Tabel Audit / Review Leader & Rendra
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

-- 6. Preseden & Overrides
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

CREATE TABLE IF NOT EXISTS `agent_overrides` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `kdtk` varchar(20) NOT NULL,
  `nonota` varchar(30) NOT NULL,
  `leader_recommendation` varchar(50) NOT NULL,
  `confidence_score` int DEFAULT 0,
  `leader_reasoning` text,
  `rendra_decision` varchar(50) NOT NULL,
  `override_source` varchar(30) DEFAULT 'HERMES_CS',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_override_lookup` (`kdtk`, `nonota`),
  KEY `idx_override_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 7. Views untuk kompatibilitas kode internal
CREATE OR REPLACE VIEW `v_rekap_selisih_intransit` AS
SELECT * FROM `rekap_intransit`;

CREATE OR REPLACE VIEW `rekap_selisih_bk` AS
SELECT * FROM `rekap_intransit`;

CREATE OR REPLACE VIEW `rekap_selisih_bk_actions` AS
SELECT * FROM `rekap_actions`;
