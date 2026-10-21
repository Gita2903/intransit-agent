import os
import unittest
from unittest.mock import MagicMock, patch
import main_orchestrator
from agents.leader import LeaderAgent
from agents.manager import ManagerAgent


class TestNewPhases(unittest.TestCase):
    def setUp(self):
        self.leader = LeaderAgent()
        self.base_case = {
            "kdtk": "TVKZ",
            "kdcab": "G026",
            "tipe": "Intransit",
            "aging_days": 3,
            "items_str": "BUN BURGER",
            "total_nilai_selisih": 50000.0,
            "tgl_kirim": "2026-10-18",
            "in_kurang_kirim": False,
            "kk_match_type": None,
            "kk_data": None,
            "shelf": {},
            "wa_feedback": None,
        }

    def test_precedent_matching(self):
        case = dict(self.base_case)
        case["wa_feedback"] = {
            "pic_sender": "PIC Robby",
            "klaim_pic": "Toko renovasi tutup sementara 3 hari ya",
        }
        precedents = [
            {"id": 1, "kata_kunci": "toko renovasi", "tindakan": "SJ Ulang", "alasan": "Toko tutup sementara"}
        ]
        action, conf, reason = self.leader.judge_single_case(case, precedents=precedents)
        self.assertEqual(action, "SJ Ulang")
        self.assertEqual(conf, 88)
        self.assertIn("[PRESEDEN #1]", reason)

    @patch("agents.leader.ask_json")
    @patch("agents.leader.llm_enabled", return_value=True)
    def test_leader_llm_claim_classification(self, mock_llm_enabled, mock_ask_json):
        case = dict(self.base_case)
        case["wa_feedback"] = {
            "pic_sender": "PIC Andi",
            "klaim_pic": "Kemasan sobek dan basi pas serah terima sopir",
        }
        mock_ask_json.return_value = {
            "kategori": "salah_driver",
            "tindakan": "Beban Karyawan",
            "confidence": 99,
            "alasan": "Indikasi kelalaian driver",
        }
        action, conf, reason = self.leader.judge_single_case(case)
        self.assertEqual(action, "Beban Karyawan")
        self.assertLessEqual(conf, 85)
        self.assertIn("INDIKASI KELALAIAN", reason)

    @patch("agents.leader.ask_json")
    @patch("agents.leader.llm_enabled", return_value=True)
    def test_leader_llm_kasus_baru(self, mock_llm_enabled, mock_ask_json):
        case = dict(self.base_case)
        case["wa_feedback"] = {
            "pic_sender": "PIC Budi",
            "klaim_pic": "Ada razia polantas mobil ga tembus ke toko",
        }
        mock_ask_json.return_value = {
            "kategori": "lainnya",
            "tindakan": "Follow Up PIC",
            "confidence": 60,
            "alasan": "Kejadian force majeure di jalan belum ada SOP",
        }
        action, conf, reason = self.leader.judge_single_case(case)
        self.assertEqual(action, "Follow Up PIC")
        self.assertIn("[KASUS BARU]", reason)

    @patch("agents.leader.llm_enabled", return_value=False)
    def test_keyword_fallback_when_llm_disabled(self, mock_llm_enabled):
        case = dict(self.base_case)
        case["wa_feedback"] = {
            "pic_sender": "PIC Robby",
            "klaim_pic": "PLU mati di toko minta buatkan ulang",
        }
        action, conf, reason = self.leader.judge_single_case(case)
        self.assertEqual(action, "SJ Ulang")
        self.assertIn("PLU tidak aktif", reason)

    def test_execute_approved_actions_support_tindakan_field(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        cases = [{
            "kdtk": "TVKZ",
            "nonota": "2610171500",
            "tindakan": "SJ Ulang",
            "leader_reasoning": "Test reasoning"
        }]
        res = self.leader.execute_approved_actions_to_db(cases, mock_conn)
        self.assertEqual(res, 1)
        self.assertTrue(mock_cur.executemany.called)

    def test_manager_weekly_evaluation(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        mock_cur.fetchone.return_value = {
            "total_reviews": 10,
            "approved_manual": 4,
            "auto_approved": 5,
        }
        mock_cur.fetchall.side_effect = [
            [{"leader_recommendation": "Potong Batch", "rendra_decision": "Penjualan Tunai", "count_override": 1, "avg_conf": 85}],
            [{"kata_kunci": "toko renovasi", "tindakan": "SJ Ulang", "alasan": "Toko tutup", "contoh_kasus": "TVKZ / 123"}]
        ]

        manager = ManagerAgent()
        report = manager.build_weekly_evaluation(mock_conn, days=7)
        self.assertIn("EVALUASI KINERJA AGENT", report)
        self.assertIn("Potong Batch", report)
        self.assertIn("Penjualan Tunai", report)
        self.assertIn("toko renovasi", report)

    @patch("main_orchestrator.manager")
    def test_handle_owner_command_set_override(self, mock_mgr):
        from main_orchestrator import handle_owner_command

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_mgr.emp_investigator.get_office_conn.return_value = mock_conn

        mock_cur.fetchone.return_value = {
            "leader_recommendation": "Potong Batch",
            "confidence_score": 95,
            "leader_reasoning": "Confirm kurang kirim",
        }

        resp = handle_owner_command("SET TVKZ 2610171500 Penjualan Tunai")
        self.assertIn("Override dari saran Leader", resp)
        self.assertIn("Penjualan Tunai", resp)

    @patch("main_orchestrator.manager")
    def test_handle_owner_command_preseden_smart_keyword(self, mock_mgr):
        from main_orchestrator import handle_owner_command

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.lastrowid = 12
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_mgr.emp_investigator.get_office_conn.return_value = mock_conn
        mock_cur.fetchone.return_value = None

        # Test stopwords removal ("Karena sedang" dibuang otomatis -> "toko tutup renovasi")
        resp = handle_owner_command("PRESEDEN TVKZ 2610171500 SJ Ulang Karena sedang toko tutup renovasi 3 hari")
        self.assertIn("PRESEDEN #12 TERCATAT", resp)
        self.assertIn("SJ Ulang", resp)
        self.assertIn("toko tutup renovasi", resp)

    @patch("main_orchestrator.manager")
    def test_conversational_store_only_lookup(self, mock_mgr):
        from main_orchestrator import handle_owner_command

        mock_conn = MagicMock()
        mock_mgr.emp_investigator.get_office_conn.return_value = mock_conn
        mock_mgr.emp_investigator.resolve_store_or_nota.return_value = [{
            "tipe": "INTRANSIT",
            "kdcab": "G026",
            "kdtk": "TVKZ",
            "nama_toko": "Puri Junction",
            "nonota": "2610171500",
            "tgl_kirim": "2026-10-17",
            "aging_days": 2,
            "total_qty_selisih": 10,
            "total_nilai_selisih": 148000.0,
            "items_str": "Dimsum Ayam CBN",
            "tindakan": "Belum Diproses",
            "status": "Open",
            "leader_recommendation": "SJ Ulang",
        }]

        # Cuma ketik "TVKZ" doang tanpa format komando
        resp = handle_owner_command("TVKZ")
        self.assertIn("DATA AKTIF TOKO TVKZ", resp)
        self.assertIn("2610171500", resp)
        self.assertIn("Dimsum Ayam CBN", resp)

    @patch("main_orchestrator.manager")
    def test_conversational_sj_request_by_store_code(self, mock_mgr):
        from main_orchestrator import handle_owner_command

        mock_conn = MagicMock()
        mock_mgr.emp_investigator.get_office_conn.return_value = mock_conn
        mock_mgr.emp_investigator.resolve_store_or_nota.return_value = [{
            "kdtk": "TASC",
            "nama_toko": "IDM Mas Mansyur 2",
            "nonota": "2601022007",
        }]
        mock_mgr.emp_pipeline.download_sj_pdf_by_nota.return_value = {
            "nonota": "2601022007",
            "file_path": "/tmp/SJ_2601022007.pdf"
        }
        mock_mgr.emp_comm.send_whatsapp_document.return_value = {"status": "sent"}

        # Minta SJ cuma pakai kode toko tanpa nomor nota 10 digit
        resp = handle_owner_command("bro minta sj toko TASC dong")
        self.assertIn("2601022007", resp)
        self.assertIn("terkirim", resp)

    @patch("main_orchestrator.manager")
    def test_conversational_natural_action_without_nonota(self, mock_mgr):
        from main_orchestrator import handle_owner_command

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_mgr.emp_investigator.get_office_conn.return_value = mock_conn
        mock_mgr.emp_investigator.resolve_store_or_nota.return_value = [{
            "kdtk": "TASC",
            "nama_toko": "IDM Mas Mansyur 2",
            "nonota": "2601022007",
            "tgl_kirim": "2026-01-02",
            "total_nilai_selisih": 21600.0,
        }]
        mock_cur.fetchone.return_value = None

        # Eksekusi tindakan pakai bahasa santai tanpa kata SET dan tanpa nonota
        resp = handle_owner_command("eh yang toko TASC potong batch aja bro")
        self.assertIn("2601022007", resp)
        self.assertIn("Potong Batch", resp)



    @patch("main_orchestrator.parse_pic_whatsapp_with_llm")
    @patch("main_orchestrator.manager")
    def test_forwarded_relokasi_teej_to_t9dc_not_raya_and_not_pdf_spam(self, mock_mgr, mock_parse_llm):
        from main_orchestrator import handle_owner_command, _find_all_kdtks

        msg = (
            "Pak rendra.. toko Teej citra 2\n\n"
            "Menolak fz bento nya.. dan di alihkan ke toko raya serang 16_T9dc\n\n"
            "Tolong di buatkan sj nya ya"
        )
        # 1. Pastikan _find_all_kdtks mengenali TEEJ dan T9DC (dari 16_T9dc), dan mengabaikan kata RAYA
        codes = _find_all_kdtks(msg)
        self.assertEqual(codes, ["TEEJ", "T9DC"])

        # 2. Pastikan saat dikirim oleh Rendra (Owner), tidak nyasar download 5 PDF lama atau cek toko RAYA,
        #    melainkan dicatat sebagai klaim relokasi TEEJ -> T9DC dan mencocokkan item Bento di DB!
        mock_conn = MagicMock()
        mock_mgr.emp_investigator.get_office_conn.return_value = mock_conn
        mock_mgr.emp_investigator.resolve_store_or_nota.return_value = [
            {"kdtk": "TEEJ", "nama_toko": "IDM CITRA RAYA 2", "nonota": "2610060477", "tgl_kirim": "2026-10-06", "items_str": "Bun Burger CBN, Vegetable CBN"},
            {"kdtk": "TEEJ", "nama_toko": "IDM CITRA RAYA 2", "nonota": "2610090461", "tgl_kirim": "2026-10-09", "items_str": "Fz Bento Set Beef Yakiniku CBN"},
        ]
        mock_mgr.emp_comm.register_pic_wa_to_waiting_list.return_value = 99
        mock_parse_llm.return_value = {
            "is_claim_report": True,
            "kdtk": "TEEJ",
            "nonota": "",
            "tgl_kirim": "2026-10-20",
            "item_dilaporkan": "Fz Bento",
            "klaim_pic": "Menolak Fz Bento, dialihkan ke toko T9DC, minta dibuatkan SJ",
        }

        resp = handle_owner_command(msg)
        self.assertIn("TEEJ", resp)
        self.assertIn("T9DC", resp)
        # Harus memilih nota 2610090461 karena item_str-nya mengandung 'Bento'!
        self.assertIn("2610090461", resp)
        self.assertFalse(mock_mgr.emp_pipeline.download_sj_pdf_by_nota.called)


if __name__ == "__main__":
    unittest.main()
