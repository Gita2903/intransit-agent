import os
import unittest
from unittest.mock import MagicMock, patch
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
            "confidence": 99,  # test fence: confidence should be clamped <= 85
            "alasan": "Indikasi kelalaian driver",
        }
        action, conf, reason = self.leader.judge_single_case(case)
        self.assertEqual(action, "Beban Karyawan")
        self.assertLessEqual(conf, 85)  # fence: Beban Karyawan never auto-approved
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

        # Mock query return values
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

        # Mock previous review
        mock_cur.fetchone.return_value = {
            "leader_recommendation": "Potong Batch",
            "confidence_score": 95,
            "leader_reasoning": "Confirm kurang kirim",
        }

        resp = handle_owner_command("SET TVKZ 2610171500 Penjualan Tunai")
        self.assertIn("Override dari saran Leader", resp)
        self.assertIn("Penjualan Tunai", resp)

    @patch("main_orchestrator.manager")
    def test_handle_owner_command_preseden(self, mock_mgr):
        from main_orchestrator import handle_owner_command

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.lastrowid = 12
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_mgr.emp_investigator.get_office_conn.return_value = mock_conn
        mock_cur.fetchone.return_value = None

        resp = handle_owner_command("PRESEDEN TVKZ 2610171500 SJ Ulang Toko tutup renovasi 3 hari")
        self.assertIn("PRESEDEN #12 TERCATAT", resp)
        self.assertIn("SJ Ulang", resp)
        self.assertIn("toko tutup renovasi", resp)


if __name__ == "__main__":
    unittest.main()
