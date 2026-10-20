# 🏢 INTBK Multi-Agent System (`intbk-agent-hq`)

Sistem **Hierarchical Multi-Agent AI** (*Employee -> Leader -> Manager*) untuk mengotomatiskan alur kerja sinkronisasi data transaksi IIA & FTP 215, investigasi bukti *Kurang Kirim* lintas *environment* (Home Lab <-> Kantor), ekstraksi laporan WhatsApp PIC (Multimodal Vision), hingga eksekusi tindakan rekonsiliasi (*Potong Batch*, *SJ Ulang*, *Beban Karyawan*, *Penjualan Tunai*).

---

## 🏗️ Arsitektur Organisasi Agent

```text
                   [ WhatsApp via Evolution API ]
                         │               ▲
       (Laporan & Approval)               (Chat/Follow-up & Baca Balasan)
                         ▼               │
                ┌─────────────────────────────────┐
                │   👔 THE MANAGER (Orchestrator) │
                └────────┬───────────────▲────────┘
                         │               │
            (Delegasi Tugas)           (Hasil Audit & Rekomendasi)
                         ▼               │
                ┌─────────────────────────────────┐
                │ 🕵️‍♂️ THE LEADER (QA & Case Judge) │
                └───┬────────────┬────────────┬───┘
                    │            │            │
       ┌────────────┘            │            └────────────┐
       ▼                         ▼                         ▼
┌──────────────┐        ┌──────────────────┐      ┌──────────────────┐
│ 👷‍♂️ EMPLOYEE 1│        │  👷‍♂️ EMPLOYEE 2   │      │  👷‍♂️ EMPLOYEE 3   │
│ Pipeline &   │        │ Case & Document  │      │ PIC Chaser &     │
│ DB Sync      │        │ Investigator     │      │ Feedback Parser  │
└──────┬───────┘        └────────┬─────────┘      └────────┬─────────┘
       │                         │                         │
 [IIA, FTP, MySQL]       [MySQL, DB Home Lab]      [Excel Rekap, WA PIC]
```

### Pembagian Peran:
1. **👷‍♂️ Employee 1 (`emp_pipeline.py`) — Pipeline & Document Worker:**
   * Menjalankan rangkaian script harian: `download_zip_iia.py` -> `rename_csv_zip.py` -> `download_ftp.py` (FTP 214) -> `download_ftp_215.py` (FTP 215) -> `download_rekap_intransit.py`.
   * Menarik langsung laporan Rekap Detail (`_isDetail=true`) dari IIAWeb dan melakukan *auto-upsert* ke tabel `rekap_selisih_bk` di MySQL Kantor tanpa perlu upload manual.
   * Mendownload PDF Surat Jalan (`printDeliveryOrders`) dengan **Auto-Login CSRF** (tanpa *hardcoded cookies*).
2. **👷‍♂️ Employee 2 (`emp_investigator.py`) — Cross-Checker & DB Sync Worker:**
   * Menarik pembaruan data dari database MariaDB `kurang_kirim` di **Home Lab** secara *incremental* ke tabel `kurang_kirim_sync` di **MySQL Kantor**.
   * Membersihkan format `nomor_surat_jalan` menjadi 4 digit terakhir (`sj_last4`) dan mengawinkannya dengan data `v_rekap_selisih_intransit` & `agent_waiting_list`.
3. **👷‍♂️ Employee 3 (`emp_communicator.py`) — WhatsApp Evolution API & Vision Worker:**
   * Menerima *webhook* chat & foto dari PIC Cabang/Toko (contoh: foto layar komputer NPB/BPB toko yang diedit `0` atau foto kertas SJ).
   * Mengekstrak `KDTK`, `NONOTA`, dan item menggunakan *Multimodal Vision AI*, lalu mencatatnya ke tabel `agent_waiting_list` (untuk laporan H-0 yang mendahului tarikan data H-1).
   * Mengirim file PDF Surat Jalan via Evolution `sendMedia` (`send_whatsapp_document()`), dipakai oleh komando `SJ` di bawah.
4. **🕵️‍♂️ The Leader (`leader.py`) — QA Gatekeeper & Case Judge:**
   * Mengevaluasi bukti setiap nota yang masih `Open` / `Belum Diproses` berdasarkan *Rule of Thumb* operasional:
     * **Terdaftar di `kurang_kirim` (`status = 1`):** Rekomendasi otomatis **`Potong Batch`** (dengan penanda khusus jika item *short-expiry / fresh* untuk kandidat **`Penjualan Tunai`**).
     * **Komplain WA PIC tapi TIDAK ADA di `kurang_kirim`:** Rekomendasi **`SJ Ulang`** (jika PLU tidak aktif atau bukti kurang kuat dari toko) atau **`Beban Karyawan`** (jika ada bukti serah terima kesalahan tim *picking/driver*).
     * **Kasus Menggantung Tanpa Bukti & Tanpa Info WA:** Masuk antrean **`Follow Up PIC`** per cabang (`KDCAB`).
   * Mengeksekusi tindakan yang sudah disetujui ke tabel `rekap_selisih_bk_actions` dengan `status = 'Open'` hingga Berita Acara (`ba_rekonsiliasi`) diterbitkan.
5. **👔 The Manager (`manager.py`) — Chief of Operations:**
   * Mengirimkan *Morning Briefing* ke WhatsApp pribadi Rendra dan mengeksekusi komando balasan via chat WA (`ACC PB`, `CEK REVIEW`, `SET <KDTK> <NONOTA> <TINDAKAN>`).

---

## 📂 Struktur Direktori

```text
intbk-agent-hq/
├── .env.example                # Template konfigurasi environment (.env)
├── requirements.txt            # Daftar package Python yang dibutuhkan
├── README.md                   # Dokumentasi panduan sistem & penggunaan
├── main_orchestrator.py        # Entrypoint utama (Flask Blueprint, Webhook WA, & CLI)
│
├── core/
│   ├── db_schema.sql           # Skema 3 tabel baru AI Agent di MySQL Kantor
│   └── state.py                # Dataclass Shared State (OfficeState & CaseItem)
│
├── agents/
│   ├── manager.py              # Otak Manager Agent (Orkestrasi & Briefing WA)
│   ├── leader.py               # Otak Leader Agent (Decision Matrix & DB Action Executor)
│   └── employees/
│       ├── emp_pipeline.py     # Wrapper script IIA/FTP, Auto-Sync Detail DB, & Auto-Login PDF
│       ├── emp_investigator.py # Auto-Sync Home Lab `kurang_kirim` & Cross-Check Engine
│       └── emp_communicator.py # Integrasi WhatsApp Evolution API & Waiting List
│
└── tools/                      # Script operasional eksisting
    ├── download_zip_iia.py
    ├── rename_csv_zip.py
    ├── download_ftp.py          # FTP 214 (BK hari ini, difilter per supplier)
    ├── download_ftp_215.py      # FTP 215 (BK cabang H-1)
    ├── download_rekap_intransit.py
    ├── save_pdfs_upgrade.py
    └── rekap_selisih_bk.py
```

---

## 🚀 Panduan Instalasi & Persiapan

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Konfigurasi File `.env`
Salin `.env.example` menjadi `.env` (baik di root folder maupun di dalam `./tools/.env` jika script lama membacanya dari folder yang sama):
```bash
cp .env.example .env
```
Isi kredensial IIAWeb, FTP, MySQL Kantor (`OFFICE_DB_*`), MariaDB Home Lab (`HOMELAB_DB_*`), dan Evolution API (`EVOLUTION_*`).

### 3. Inisialisasi Tabel Agent di MySQL Kantor
Tabel `kurang_kirim_sync`, `agent_waiting_list`, dan `agent_case_reviews` akan dibuat secara otomatis saat `emp_investigator.py` berjalan pertama kali, atau bisa dieksekusi manual:
```bash
mysql -u root -p nama_db_kantor < core/db_schema.sql
```

---

## 🎮 Cara Menjalankan Sistem

### Opsi A: Menjalankan via CLI / Windows Task Scheduler / n8n Execute Command
* **1. Sinkronisasi Awal Tabel `kurang_kirim` dari Home Lab:**
  ```bash
  python main_orchestrator.py --mode sync-kk
  ```
* **2. Menjalankan Workflow Pagi Lengkap (IIA -> FTP -> Sync DB -> Audit Leader -> WA Briefing):**
  ```bash
  python main_orchestrator.py --mode morning
  ```

### Opsi B: Menjalankan sebagai Server Webhook & API (untuk n8n & Evolution API)
* **Menjalankan Standalone Server di Port 5055:**
  ```bash
  python main_orchestrator.py --mode server --port 5055
  ```
* **Atau Menggabungkan ke Aplikasi Flask Utama (`rekap_selisih_bk.py`):**
  Tambahkan 2 baris berikut pada `app.py` Flask utama Anda:
  ```python
  from main_orchestrator import agent_bp
  app.register_blueprint(agent_bp, url_prefix="/api/agent")
  ```

---

## 💬 Daftar Komando WhatsApp (Khusus Owner / Rendra)

Saat menerima *Morning Briefing* dari **Manager Agent**, Anda cukup membalas chat WhatsApp tersebut dengan komando berikut:
* **`ACC PB`** : Menyetujui dan langsung meng-insert seluruh nota berstatus *Confirm Kurang Kirim* (`confidence >= 90%`) ke tabel `rekap_selisih_bk_actions` dengan tindakan **`Potong Batch`** dan `status = 'Open'`.
* **`CEK REVIEW`** : Menampilkan daftar nota anomali (misal: komplain WA PIC yang tidak ada di `kurang_kirim`, atau *match* tanggal kirim saja) beserta analisis **Leader Agent**.
* **`SET <KDTK> <NONOTA> <TINDAKAN>`** : Mengeksekusi tindakan manual untuk satu nota langsung dari WhatsApp (contoh: `SET TVKZ 2610171500 SJ Ulang`).
* **`RUN MORNING`** : Memicu ulang proses sinkronisasi dan audit harian kapan saja dari WhatsApp.
* **`REG PIC <KDCAB> <NOMOR/JID> <NAMA>`** : Daftarin kontak PIC cabang (japri: `08xx`/`62xx`, grup: JID `...@g.us`). Contoh: `REG PIC G026 08123456789 Robby`.
* **`LIST PIC`** : Lihat kontak PIC yang sudah terdaftar.
* **`BLAST PIC`** : Preview tagihan kasus "gelap" (Follow Up PIC) per cabang. **Belum ada yang terkirim.**
* **`BLAST PIC KONFIRM`** : Kirim tagihan beneran ke kontak PIC. Jalan di background dengan jeda acak antar pesan, ada cooldown per kontak (default 20 jam), laporan hasil dikirim ke WA lo.
* **Minta Surat Jalan (PDF)** : Kirim pesan bebas yang berisi kata `SJ` / `PDF` / `surat jalan` + nomor nota 10 digit, boleh banyak nota sekaligus (maks 10). Contoh: `kirimkan pdf nota 2601030079` atau `kirimkan 3 sj ini: 2610151552 2610151553 2610151554`. PDF diambil dari IIAWeb lalu dikirim ke WA pengirim, tiap nota dibalas ✅/❌. Berlaku untuk Owner **dan PIC yang sudah terdaftar** (`REG PIC`, nomor HP saja, bukan grup). Pesan berawalan `SET` / `PRESEDEN` tidak dianggap permintaan SJ.


## 🥗 Aturan Penjualan Tunai (berdasarkan umur produk)
Leader menghitung sisa umur tiap item selisih dari `data/MAPPING4.xlsx` (kolom `UMUR PRODUK`, format `H+N dari produksi`):

`sisa umur = (tgl_kirim - PRODUCTION_LEAD_DAYS + N) - hari ini`

| Kondisi nota (sudah terbukti di `kurang_kirim`) | Rekomendasi | Confidence |
|---|---|---|
| Semua item sisa umur = `PENJUALAN_TUNAI_SISA_HARI` (default 1) | **Penjualan Tunai** | 95 / 85 (tidak auto, lewat `CEK REVIEW` + `SET`) |
| Campuran (sebagian sisa 1 hari, sebagian lain / belum ke-mapping) | Potong Batch (cek manual) | 75 |
| Ada item sudah lewat umur | Potong Batch (cek fisik dulu) | 75 |
| Selain itu / produk `H+180 cetak seal` | Potong Batch | 95 / 85 |

Item dicari lewat `ITEM ID` (`FG00xxxx`), lalu PLU 8 digit, lalu nama item. Item yang gagal ke-mapping disebut di alasan Leader.

## 🧠 Model AI per Peran (9router)
Semua panggilan AI lewat `core/llm.py` (endpoint OpenAI-compatible, default `http://localhost:20128/v1`). Model dipisah per peran:

| Peran | Role di kode | Env | Default combo |
|---|---|---|---|
| Employee | `worker` | `LLM_MODEL_WORKER` | `worker` |
| Leader | `leader` | `LLM_MODEL_LEADER` | `manager` |
| Manager | `manager` | `LLM_MODEL_MANAGER` | `manager` |

Saat ini yang benar-benar memanggil AI hanya parser WA PIC (Employee 3, role `worker`, harus support gambar). Keputusan Leader dan briefing Manager masih berbasis aturan/kode. `LLM_MODEL` (tanpa suffix) tetap didukung sebagai fallback semua peran. `LLM_DISABLED=1` mematikan AI.


## 💬 Mode Chat Fleksibel & Smart Auto-Lookup DB (Tanpa Format Kaku!)

Mulai versi ini, **Owner (Rendra)** maupun **PIC Cabang/Toko** tidak perlu lagi menghafal format perintah yang kaku atau 10 digit nomor nota (`NONOTA`):

1. **Ketik Kode Toko / Nama Toko Saja (Auto-Cek Intransit & Selisih):**
   * Cukup kirim chat singkat seperti: `TVKZ` atau `cek toko Puri Junction`
   * Agent otomatis mengecek tabel `v_rekap_selisih_intransit` (dan fallback ke `kurang_kirim_sync`), lalu membalas daftar `NONOTA`, tanggal kirim, umur kasus (*aging*), item selisih, nilai rupiah, serta rekomendasi Leader.
2. **Minta PDF Surat Jalan (SJ) Hanya dengan Kode Toko / 4 Digit Akhir SJ:**
   * Contoh chat bebas: `minta sj toko TASC dong`, `tolong pdf sj TVKZ`, atau `sj 1196 toko TC08`
   * Agent otomatis mencari `NONOTA` 10 digit yang terkait di database, mengunduh PDF Surat Jalan dari IIAWeb, dan langsung mengirimkannya ke WhatsApp pengirim!
3. **Laporan Klaim PIC Tanpa Nomor Nota (Auto-Enrich DB):**
   * Saat PIC melapor kendala tanpa menyebut nomor nota (misal: *"Pak toko T4BU barang sambal tidak datang ya"*), Agent otomatis mencari `NONOTA` dan `tgl_kirim` aktif milik toko `T4BU` di database dan langsung menautkannya ke `agent_waiting_list`.
4. **Eksekusi Tindakan dengan Bahasa Santai (Khusus Owner):**
   * Anda bisa langsung membalas santai: `eh yang toko TASC potong batch aja bro` atau `TVKZ 1500 sj ulang` (atau `SET TVKZ SJ Ulang` tanpa menulis nomor nota).
   * Jika toko hanya punya 1 nota `Open`, Agent langsung mengeksekusinya ke database. Jika ada lebih dari 1 nota `Open`, Agent akan menampilkan daftarnya dan menanyakan nota mana yang ingin dieksekusi.
