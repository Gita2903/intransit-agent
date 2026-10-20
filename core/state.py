from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import date, datetime


@dataclass
class CaseItem:
    kdcab: str
    kdtk: str
    nama_toko: str
    nonota: str
    tipe: str                  # 'Intransit' atau 'Selisih BK'
    tgl_kirim: str
    aging_days: int
    nilai_selisih: float
    items_str: str
    in_kurang_kirim: bool = False
    kurang_kirim_lampiran: Optional[str] = None
    wa_feedback: Optional[Dict[str, Any]] = None
    is_short_expiry: bool = False
    recommended_action: Optional[str] = None
    confidence: int = 0
    reasoning: str = ""


@dataclass
class OfficeState:
    run_date: str = field(default_factory=lambda: date.today().strftime("%Y-%m-%d"))
    pipeline_status: Dict[str, Any] = field(default_factory=dict)
    pipeline_verified_by_leader: bool = False
    retry_count: int = 0

    active_cases: List[CaseItem] = field(default_factory=list)
    approved_for_execution: List[CaseItem] = field(default_factory=list)
    need_human_approval: List[CaseItem] = field(default_factory=list)
    chaser_queue_by_cabang: Dict[str, List[CaseItem]] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)

    def log(self, role: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] [{role}] {msg}"
        self.logs.append(entry)
        print(entry)
