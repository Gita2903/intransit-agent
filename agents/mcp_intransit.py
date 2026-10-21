"""
Runner otomatis untuk n8n / Task Scheduler / CLI.
Secara default mengeksekusi sync_pagi():
  1. Sync data kurang_kirim dari Homelab MariaDB
  2. Tarik rekap intransit detail dari IIA Web ke intbk_agent_db
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from mcp_intransit import sync_pagi, main

if __name__ == "__main__":
    # Jika dipanggil tanpa argumen oleh n8n Execute Command, langsung jalankan sync pagi
    if len(sys.argv) <= 1:
        print(sync_pagi())
        sys.exit(0)
    main()
