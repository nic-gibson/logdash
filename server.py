#!/usr/bin/env python3
"""Local dashboard server.  usage: python3 server.py [logs.db] [-p PORT]   then open http://localhost:6424"""
import json, os, sqlite3, sys, threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import argparse
HERE = os.path.dirname(os.path.abspath(__file__))
_ap = argparse.ArgumentParser(description="Log timeline dashboard server")
_ap.add_argument("db", nargs="?", default=os.path.join(HERE, "logs.db"),
                 help="path to logs.db built by build.py (default: logs.db next to this script)")
_ap.add_argument("-p", "--port", type=int, default=6424, help="port to listen on (default 6424)")
_ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
_args = _ap.parse_args()
DB = os.path.abspath(os.path.expanduser(_args.db))
PORT, HOST = _args.port, _args.host
local = threading.local()


def conn():
    if not hasattr(local, "c"):
        local.c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    return local.c


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def where(q):
    w, a = [], []
    g = lambda k: [x for x in q.get(k, [""])[0].split(",") if x != ""]
    if q.get("t0", [""])[0]: w.append("ts>=?"); a.append(int(q["t0"][0]))
    if q.get("t1", [""])[0]: w.append("ts<?"); a.append(int(q["t1"][0]))
    for key, col, cast in (("nodes", "node", int), ("logs", "log", str), ("levels", "level", str)):
        v = g(key)
        if v:
            if key == "levels":
                v = ["" if x == "NONE" else x for x in v]
            w.append(f"{col} IN ({','.join('?'*len(v))})"); a += [cast(x) for x in v]
    s = q.get("q", [""])[0]
    if s:
        esc = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        w.append("msg LIKE ? ESCAPE '\\'"); a.append(f"%{esc}%")
    return (" WHERE " + " AND ".join(w)) if w else "", a


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, body, ctype="application/json"):
        if not isinstance(body, bytes): body = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); c = conn()
        try:
            if u.path in ("/", "/index.html"):
                return self.send(open(os.path.join(HERE, "dashboard.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/meta":
                lo, hi = c.execute("SELECT MIN(ts),MAX(ts) FROM logs").fetchone()
                return self.send({
                    "min": lo, "max": hi,
                    "nodes": c.execute("SELECT node,COUNT(*) FROM logs GROUP BY node ORDER BY node").fetchall(),
                    "logs": c.execute("SELECT log,COUNT(*) FROM logs GROUP BY log ORDER BY log").fetchall(),
                    "levels": c.execute("SELECT level,COUNT(*) FROM logs GROUP BY level").fetchall()})
            if u.path == "/api/hist":
                w, a = where(q)
                t0 = int(q["t0"][0]) if q.get("t0", [""])[0] else c.execute("SELECT MIN(ts) FROM logs").fetchone()[0]
                t1 = int(q["t1"][0]) if q.get("t1", [""])[0] else c.execute("SELECT MAX(ts) FROM logs").fetchone()[0] + 1
                n = int(q.get("buckets", ["150"])[0]); size = max(1, -(-(t1 - t0) // n))
                by = {"node": "node", "log": "log", "level": "level"}.get(q.get("by", ["node"])[0], "node")
                rows = c.execute(f"SELECT (ts-?)/? b,{by},COUNT(*) FROM logs{w} GROUP BY b,{by}", [t0, size] + a).fetchall()
                series = {}
                for b, k, cnt in rows:
                    if 0 <= b < n + 1:
                        series.setdefault(str(k) if k != "" else "NONE", [0] * (n + 1))[b] = cnt
                return self.send({"t0": t0, "size": size, "n": n + 1, "series": series})
            if u.path == "/api/lines":
                w, a = where(q); lim = min(int(q.get("limit", ["200"])[0]), 5000)
                off = int(q.get("offset", ["0"])[0]); order = "DESC" if q.get("desc", ["0"])[0] == "1" else "ASC"
                rows = c.execute(f"SELECT ts,node,log,level,msg FROM logs{w} ORDER BY rowid {order} LIMIT ? OFFSET ?", a + [lim, off]).fetchall()
                total = None
                if q.get("count", ["0"])[0] == "1":
                    total = c.execute(f"SELECT COUNT(*) FROM logs{w}", a).fetchone()[0]
                return self.send({"total": total, "rows": [[r[0], iso(r[0]), r[1], r[2], r[3], r[4]] for r in rows]})
            if u.path == "/api/export":
                w, a = where(q)
                self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=merged.log"); self.end_headers()
                for ts, node, log, lv, msg in c.execute(f"SELECT ts,node,log,level,msg FROM logs{w} ORDER BY rowid", a):
                    self.wfile.write(f"{iso(ts)} node_{node} {log} {msg}\n".encode())
                return
            self.send_response(404); self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send({"error": str(e)})


if __name__ == "__main__":
    if not os.path.isfile(DB): sys.exit(f"database not found: {DB} - run build.py first")
    print(f"serving {DB}\nhttp://localhost:{PORT}")
    import signal
    ThreadingHTTPServer.daemon_threads = True      # don't wait on in-flight requests at shutdown
    srv = ThreadingHTTPServer((HOST, PORT), H)
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))  # kill/systemd stop = same clean path
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        srv.server_close()
