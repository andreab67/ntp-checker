from flask import Flask, request, jsonify
import os, psycopg2, psycopg2.extras
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
DB_URL = os.getenv('DATABASE_URL')

SAFE_WINDOWS = {
    '24h': '24 hours',
    '14d': '14 days',
    '90d': '90 days'
}
SAFE_INTERVALS = {
    '5min': '5 minutes',
    '1h': '1 hour',
    '1d': '1 day'
}

def q(sql, args=()):
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, args)
            return cur.fetchall()

def aggregate_offset(window_human: str, interval_human: str):
    rows = q(f"""
        SELECT date_bin(%s::interval, ts, '2000-01-01'::timestamptz) AS bucket,
               avg(last_offset_sec) AS avg_offset,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY abs(last_offset_sec)) AS p95_abs_offset,
               max(abs(last_offset_sec)) AS max_abs_offset
        FROM metrics.ntp_parent
        WHERE ts >= now() - %s::interval
        GROUP BY bucket
        ORDER BY bucket;
    """, (interval_human, window_human))
    return [{"ts": r["bucket"].isoformat(),
             "avg": r["avg_offset"],
             "p95": r["p95_abs_offset"],
             "max": r["max_abs_offset"]}
            for r in rows]

@app.get("/api/offset")
def api_offset():
    window = request.args.get("window", "24h")
    interval = request.args.get("interval", "5min")
    if window not in SAFE_WINDOWS or interval not in SAFE_INTERVALS:
        return jsonify({"error":"bad params"}), 400
    data = aggregate_offset(SAFE_WINDOWS[window], SAFE_INTERVALS[interval])
    return jsonify(data)

@app.get("/api/latest")
def api_latest():
    rows = q("""
        SELECT ts, last_offset_sec, stratum, total_sources, leap_status, gps_mode
        FROM metrics.ntp_parent
        ORDER BY ts DESC
        LIMIT 1;
    """)
    if not rows: return jsonify({})
    r = rows[0]
    return jsonify({
        "ts": r["ts"].isoformat(),
        "offset": r["last_offset_sec"],
        "stratum": r["stratum"],
        "total_sources": r["total_sources"],
        "leap_status": r["leap_status"],
        "gps_mode": r["gps_mode"]
    })

@app.get("/")
def index():
    return """
<!doctype html><html><head>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>NTP/GPS Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  body{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b1220;color:#e5e7eb;margin:0}
  header{padding:16px 24px;border-bottom:1px solid #111;background:#0e1626}
  .wrap{padding:18px 24px}
  .grid{display:grid;grid-template-columns:1fr;gap:16px}
  .card{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:16px}
  .row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
  .pill{display:inline-block;padding:6px 10px;border-radius:999px;font-weight:600}
  .ok{background:#064e3b;color:#d1fae5}
  .bad{background:#7f1d1d;color:#fecaca}
  h2{margin:0 0 12px 0;font-size:18px}
</style>
</head><body>
<header><h1>NTP/GPS Dashboard</h1></header>
<div class=wrap>
  <div class="card">
    <div class="row">
      <div>Stratum: <span id=stratum style="font-weight:700;font-size:20px"></span></div>
      <div>Leap: <span id=leap class=pill></span></div>
      <div>GPS: <span id=gps class=pill></span></div>
    </div>
  </div>
  <div class="grid">
    <div class="card"><h2>Offset last 24h (5-min buckets)</h2><div id=chart24h></div></div>
    <div class="card"><h2>Offset last 14d (hourly buckets)</h2><div id=chart14d></div></div>
    <div class="card"><h2>Offset last 90d (daily buckets)</h2><div id=chart90d></div></div>
  </div>
</div>
<script>
async function fetchJSON(u){ const r = await fetch(u); return r.json(); }
function plot(id,data,title){
  const x = data.map(d=>d.ts);
  const avg = data.map(d=>d.avg);
  const p95 = data.map(d=>d.p95);
  const max = data.map(d=>d.max);
  const traces = [
    {x, y: avg, name:'avg offset', mode:'lines'},
    {x, y: p95, name:'p95 abs offset', mode:'lines'},
    {x, y: max, name:'max abs offset', mode:'lines'}
  ];
  Plotly.newPlot(id, traces, {margin:{l:40,r:10,t:10,b:40}, legend:{orientation:'h'}}, {displayModeBar:false, staticPlot:true});
}
async function draw(){
  const latest = await fetchJSON('/api/latest');
  document.getElementById('stratum').textContent = latest.stratum ?? '—';

  const leap = document.getElementById('leap');
  leap.textContent = latest.leap_status || 'Unknown';
  leap.className = 'pill ' + (latest.leap_status==='Normal' ? 'ok' : 'bad');

  const gps = document.getElementById('gps');
  const gm = (latest.gps_mode||'').toLowerCase();
  gps.textContent = latest.gps_mode || 'Unknown';
  gps.className = 'pill ' + ((gm.includes('2d')||gm.includes('3d')) ? 'ok' : 'bad');

  const d24 = await fetchJSON('/api/offset?window=24h&interval=5min');
  const d14 = await fetchJSON('/api/offset?window=14d&interval=1h');
  const d90 = await fetchJSON('/api/offset?window=90d&interval=1d');
  plot('chart24h', d24, '24h');
  plot('chart14d', d14, '14d');
  plot('chart90d', d90, '90d');
}
draw(); setInterval(draw, 30000);
</script>
</body></html>
"""
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("DASH_PORT","8080")))
