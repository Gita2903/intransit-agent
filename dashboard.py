"""
Dashboard Pribadi Rendra — Intransit Command Center.
Single-file Flask app, query langsung ke intbk_agent_db.

Jalankan:  python dashboard.py
Buka:     http://localhost:5050
"""
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, jsonify, render_template_string
from dotenv import load_dotenv
import pymysql
import pymysql.cursors

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)


def _db():
    return pymysql.connect(
        host=os.environ.get("OFFICE_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("OFFICE_DB_PORT", 3306)),
        user=os.environ.get("OFFICE_DB_USER", "root"),
        password=os.environ.get("OFFICE_DB_PASS", ""),
        database=os.environ.get("OFFICE_DB_NAME", "intbk_agent_db"),
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
        read_timeout=15,
    )


def _q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall() or []


def _q1(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone() or {}


def _rp(val):
    try:
        return f"Rp {float(val or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "Rp 0"


# ── routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    conn = _db()
    try:
        # Metrik
        m_nota = _q1(conn, """
            SELECT COUNT(DISTINCT CONCAT(r.kdtk,'_',r.nonota)) AS total
            FROM rekap_intransit r
            LEFT JOIN rekap_actions a ON a.kdtk=r.kdtk AND a.nonota=r.nonota
            WHERE r.kode_fg IS NOT NULL AND TRIM(r.kode_fg)!=''
              AND COALESCE(a.status,'Open') = 'Open'
        """)
        m_nilai = _q1(conn, """
            SELECT COALESCE(SUM(sub.ns),0) AS total FROM (
                SELECT r.kdtk, r.nonota, SUM(r.nilai_selisih) AS ns
                FROM rekap_intransit r
                LEFT JOIN rekap_actions a ON a.kdtk=r.kdtk AND a.nonota=r.nonota
                WHERE r.kode_fg IS NOT NULL AND TRIM(r.kode_fg)!=''
                  AND COALESCE(a.status,'Open') = 'Open'
                GROUP BY r.kdtk, r.nonota
            ) sub
        """)
        m_komplain = _q1(conn, "SELECT COUNT(*) AS total FROM data_komplain WHERE status_komplain IN ('BARU','PROSES')")
        m_kk = _q1(conn, "SELECT COUNT(*) AS total, MAX(tgl_kirim) AS tgl FROM kurang_kirim_sync")
        m_rekap_tgl = _q1(conn, "SELECT MAX(tgl_kirim) AS tgl FROM rekap_intransit")

        # Top 15 nota
        top_nota = _q(conn, """
            SELECT r.kdcab, r.kdtk, r.nama_toko, r.nonota, r.tgl_kirim,
                   DATEDIFF(CURDATE(), r.tgl_kirim) AS aging,
                   SUM(r.nilai_selisih) AS nilai, r.tipe,
                   COALESCE(a.tindakan,'Belum Diproses') AS tindakan,
                   COALESCE(a.status,'Open') AS status
            FROM rekap_intransit r
            LEFT JOIN rekap_actions a ON a.kdtk=r.kdtk AND a.nonota=r.nonota
            WHERE r.kode_fg IS NOT NULL AND TRIM(r.kode_fg)!=''
            GROUP BY r.kdcab, r.kdtk, r.nama_toko, r.nonota, r.tgl_kirim, r.tipe, a.tindakan, a.status
            ORDER BY nilai DESC
            LIMIT 15
        """)

        # Breakdown tindakan (pie)
        pie = _q(conn, """
            SELECT COALESCE(a.tindakan,'Belum Diproses') AS label, COUNT(DISTINCT CONCAT(r.kdtk,'_',r.nonota)) AS n
            FROM rekap_intransit r
            LEFT JOIN rekap_actions a ON a.kdtk=r.kdtk AND a.nonota=r.nonota
            WHERE r.kode_fg IS NOT NULL AND TRIM(r.kode_fg)!=''
            GROUP BY label ORDER BY n DESC
        """)

        # Per cabang (bar)
        bar = _q(conn, """
            SELECT r.kdcab, COUNT(DISTINCT CONCAT(r.kdtk,'_',r.nonota)) AS nota,
                   COALESCE(SUM(r.nilai_selisih),0) AS nilai
            FROM rekap_intransit r
            LEFT JOIN rekap_actions a ON a.kdtk=r.kdtk AND a.nonota=r.nonota
            WHERE r.kode_fg IS NOT NULL AND TRIM(r.kode_fg)!=''
              AND COALESCE(a.status,'Open')='Open'
            GROUP BY r.kdcab ORDER BY nilai DESC LIMIT 12
        """)

        # Komplain terbaru
        komplain = _q(conn, """
            SELECT id, kdtk, kdcab, nonota, keluhan, status_komplain, pelapor, created_at
            FROM data_komplain ORDER BY id DESC LIMIT 5
        """)
    finally:
        conn.close()

    return render_template_string(TEMPLATE,
        m_nota=m_nota, m_nilai=m_nilai, m_komplain=m_komplain,
        m_kk=m_kk, m_rekap_tgl=m_rekap_tgl,
        top_nota=top_nota, pie=pie, bar=bar, komplain=komplain,
        rp=_rp, now=datetime.now(),
    )


@app.route("/api/detail/<nonota>")
def detail(nonota):
    conn = _db()
    try:
        rows = _q(conn, """
            SELECT kode_fg, nama_produk, qty_sj, nilai_sj, qty_bpb, nilai_btb, qty_selisih, nilai_selisih
            FROM rekap_intransit WHERE nonota = %s AND kode_fg IS NOT NULL AND TRIM(kode_fg)!=''
            ORDER BY no_urut
        """, [nonota])
    finally:
        conn.close()
    for r in rows:
        for k in ("nilai_sj", "nilai_btb", "nilai_selisih"):
            r[k] = float(r.get(k) or 0)
    return jsonify(rows)


# ── template ──────────────────────────────────────────────────────────────────
TEMPLATE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Intransit Command Center</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.header{background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);padding:24px 32px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:20px;font-weight:700;background:linear-gradient(90deg,#10b981,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .meta{font-size:13px;color:#94a3b8}
.header .meta span{color:#10b981;font-weight:600}
.container{max-width:1280px;margin:0 auto;padding:24px 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:28px}
.card{background:#1e293b;border-radius:12px;padding:20px;border:1px solid #334155;transition:border-color .2s}
.card:hover{border-color:#10b981}
.card .label{font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.card .value{font-size:28px;font-weight:700;color:#f1f5f9}
.card .sub{font-size:12px;color:#64748b;margin-top:4px}
.card.alert .value{color:#ef4444}
.card.ok .value{color:#10b981}
.grid2{display:grid;grid-template-columns:1.6fr 1fr;gap:20px;margin-bottom:28px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.panel{background:#1e293b;border-radius:12px;padding:20px;border:1px solid #334155}
.panel h2{font-size:14px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 6px;color:#64748b;font-weight:600;font-size:11px;text-transform:uppercase;border-bottom:1px solid #334155}
td{padding:8px 6px;border-bottom:1px solid rgba(51,65,85,.4);white-space:nowrap}
tr:hover td{background:rgba(16,185,129,.05)}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600}
.badge-open{background:rgba(234,179,8,.15);color:#eab308}
.badge-resolved{background:rgba(16,185,129,.15);color:#10b981}
.badge-closed{background:rgba(100,116,139,.2);color:#94a3b8}
.badge-pb{background:rgba(6,182,212,.15);color:#06b6d4}
.badge-baru{background:rgba(239,68,68,.15);color:#ef4444}
.badge-proses{background:rgba(234,179,8,.15);color:#eab308}
.badge-selesai{background:rgba(16,185,129,.15);color:#10b981}
.chart-wrap{height:220px;margin-bottom:20px}
.komplain-item{padding:10px 0;border-bottom:1px solid rgba(51,65,85,.4)}
.komplain-item:last-child{border:none}
.komplain-item .ki-head{display:flex;justify-content:space-between;margin-bottom:4px}
.komplain-item .ki-head span{font-size:12px;font-weight:600}
.komplain-item .ki-body{font-size:12px;color:#94a3b8}
.footer{text-align:center;padding:16px;font-size:11px;color:#475569}
.nota-link{color:#06b6d4;cursor:pointer;text-decoration:none}
.nota-link:hover{text-decoration:underline}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;justify-content:center;align-items:center}
.modal-bg.show{display:flex}
.modal{background:#1e293b;border-radius:12px;padding:24px;border:1px solid #334155;max-width:700px;width:90%;max-height:80vh;overflow-y:auto}
.modal h3{font-size:16px;margin-bottom:12px;color:#f1f5f9}
.modal .close{float:right;cursor:pointer;color:#94a3b8;font-size:18px}
.modal .close:hover{color:#ef4444}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>⚡ INTRANSIT COMMAND CENTER</h1>
    <div class="meta">Database: <span>intbk_agent_db</span> · Data terakhir: <span>{{ m_rekap_tgl.tgl or '-' }}</span></div>
  </div>
  <div class="meta">Rendered {{ now.strftime('%d %b %Y %H:%M:%S WIB') }} · <a href="/" style="color:#06b6d4">↻ Refresh</a></div>
</div>

<div class="container">

<!-- KARTU METRIK -->
<div class="cards">
  <div class="card {{ 'alert' if (m_nota.total or 0) > 0 else 'ok' }}">
    <div class="label">Total Nota Open</div>
    <div class="value">{{ m_nota.total or 0 }}</div>
    <div class="sub">Nota yang belum Resolved/Closed</div>
  </div>
  <div class="card alert">
    <div class="label">Total Nilai Selisih Open</div>
    <div class="value">{{ rp(m_nilai.total) }}</div>
    <div class="sub">Akumulasi rupiah selisih aktif</div>
  </div>
  <div class="card {{ 'alert' if (m_komplain.total or 0) > 0 else 'ok' }}">
    <div class="label">Komplain Aktif</div>
    <div class="value">{{ m_komplain.total or 0 }}</div>
    <div class="sub">Status BARU + PROSES</div>
  </div>
  <div class="card ok">
    <div class="label">Gudang Sync</div>
    <div class="value">{{ '{:,}'.format(m_kk.total or 0).replace(',','.') }}</div>
    <div class="sub">Data kurang_kirim · terakhir {{ m_kk.tgl or '-' }}</div>
  </div>
</div>

<!-- TABEL + CHART -->
<div class="grid2">
  <div class="panel">
    <h2>Top 15 Nota (Nilai Selisih Terbesar)</h2>
    <div style="overflow-x:auto">
    <table>
      <tr><th>Cab</th><th>Toko</th><th>Nota</th><th>Kirim</th><th>Aging</th><th>Nilai</th><th>Tindakan</th><th>Status</th></tr>
      {% for r in top_nota %}
      <tr>
        <td>{{ r.kdcab }}</td>
        <td>{{ r.kdtk }} {{ (r.nama_toko or '')[:18] }}</td>
        <td><a class="nota-link" onclick="showDetail('{{ r.nonota }}')">{{ r.nonota }}</a></td>
        <td>{{ r.tgl_kirim }}</td>
        <td>{{ r.aging }} hr</td>
        <td style="text-align:right;font-weight:600">{{ rp(r.nilai) }}</td>
        <td>
          {% if r.tindakan == 'Potong Batch' %}<span class="badge badge-pb">{{ r.tindakan }}</span>
          {% elif r.tindakan == 'Belum Diproses' %}<span class="badge badge-baru">{{ r.tindakan }}</span>
          {% else %}<span class="badge badge-open">{{ r.tindakan }}</span>{% endif %}
        </td>
        <td>
          {% if r.status == 'Open' %}<span class="badge badge-open">Open</span>
          {% elif r.status == 'Resolved' %}<span class="badge badge-resolved">Resolved</span>
          {% else %}<span class="badge badge-closed">{{ r.status }}</span>{% endif %}
        </td>
      </tr>
      {% endfor %}
    </table>
    </div>
  </div>

  <div>
    <div class="panel" style="margin-bottom:20px">
      <h2>Breakdown Tindakan</h2>
      <div class="chart-wrap"><canvas id="pieChart"></canvas></div>
    </div>
    <div class="panel" style="margin-bottom:20px">
      <h2>Selisih per Cabang (Open)</h2>
      <div class="chart-wrap"><canvas id="barChart"></canvas></div>
    </div>
    <div class="panel">
      <h2>Komplain Terbaru</h2>
      {% if komplain %}
        {% for k in komplain %}
        <div class="komplain-item">
          <div class="ki-head">
            <span>#{{ k.id }} · {{ k.kdtk }} {{ k.nonota or '' }}</span>
            <span class="badge badge-{{ k.status_komplain|lower }}">{{ k.status_komplain }}</span>
          </div>
          <div class="ki-body">{{ (k.keluhan or '')[:120] }} · {{ k.pelapor or '-' }} · {{ k.created_at }}</div>
        </div>
        {% endfor %}
      {% else %}
        <div style="color:#64748b;font-size:13px;padding:12px 0">Belum ada data komplain.</div>
      {% endif %}
    </div>
  </div>
</div>

</div>

<!-- MODAL DETAIL NOTA -->
<div class="modal-bg" id="modalBg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <span class="close" onclick="closeModal()">✕</span>
    <h3 id="modalTitle">Detail Nota</h3>
    <div id="modalBody" style="font-size:13px;color:#94a3b8">Loading...</div>
  </div>
</div>

<div class="footer">Intransit Command Center · intbk_agent_db · {{ now.strftime('%Y') }}</div>

<script>
// Pie chart
const pieCtx = document.getElementById('pieChart').getContext('2d');
const pieData = {{ pie | tojson }};
const pieColors = ['#10b981','#06b6d4','#eab308','#ef4444','#8b5cf6','#f97316','#64748b'];
new Chart(pieCtx, {
  type: 'doughnut',
  data: {
    labels: pieData.map(d => d.label),
    datasets: [{
      data: pieData.map(d => d.n),
      backgroundColor: pieColors.slice(0, pieData.length),
      borderWidth: 0
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { position:'bottom', labels:{color:'#94a3b8',font:{size:11},padding:8,boxWidth:12} }
    }
  }
});

// Bar chart
const barCtx = document.getElementById('barChart').getContext('2d');
const barData = {{ bar | tojson }};
new Chart(barCtx, {
  type: 'bar',
  data: {
    labels: barData.map(d => d.kdcab),
    datasets: [{
      label: 'Nota Open',
      data: barData.map(d => d.nota),
      backgroundColor: '#06b6d4',
      borderRadius: 4, barPercentage: 0.6
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    scales: {
      x:{ticks:{color:'#64748b',font:{size:10}},grid:{display:false}},
      y:{ticks:{color:'#64748b',font:{size:10}},grid:{color:'rgba(51,65,85,.3)'}}
    },
    plugins:{ legend:{display:false} }
  }
});

// Modal detail
function showDetail(nonota) {
  document.getElementById('modalBg').classList.add('show');
  document.getElementById('modalTitle').textContent = 'Detail Nota ' + nonota;
  document.getElementById('modalBody').innerHTML = 'Loading...';
  fetch('/api/detail/' + nonota)
    .then(r => r.json())
    .then(items => {
      if (!items.length) { document.getElementById('modalBody').innerHTML = 'Tidak ada item.'; return; }
      const fmt = v => new Intl.NumberFormat('id-ID').format(v || 0);
      let html = '<table><tr><th>Kode FG</th><th>Nama Produk</th><th>Qty SJ</th><th>Qty BPB</th><th>Qty Selisih</th><th>Nilai Selisih</th></tr>';
      items.forEach(i => {
        html += `<tr><td>${i.kode_fg}</td><td>${(i.nama_produk||'').substring(0,40)}</td><td>${i.qty_sj}</td><td>${i.qty_bpb}</td><td style="color:#ef4444;font-weight:600">${i.qty_selisih}</td><td style="text-align:right;font-weight:600">Rp ${fmt(i.nilai_selisih)}</td></tr>`;
      });
      html += '</table>';
      document.getElementById('modalBody').innerHTML = html;
    });
}
function closeModal() { document.getElementById('modalBg').classList.remove('show'); }
</script>
</body>
</html>"""


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5050)
    args = p.parse_args()
    print(f"⚡ Intransit Command Center → http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)
