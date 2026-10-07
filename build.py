#!/usr/bin/env python3
"""Merge every node_N/logs/* file (incl. .gz and rotated copies) into one time-sorted
SQLite DB (logs.db) and a merged text file (merged.log: time node log message).

usage: python3 build.py PACKAGE [stage] [-o OUTPUT]
  PACKAGE  a support package .tar.gz (e.g. ~/Downloads/debuginfo.010DB0726D35FCBF.tar.gz)
           or an already-extracted package directory (default: parent of this script's dir)
  stage    all (default: extract, load, sort/index, install the DB) | load | index | export | finish
           'export' additionally writes merged.log next to the DB (slow; the dashboard can also export on demand)
  -o       path of the database to write (or a directory, to use logs.db inside it); default: logs.db next to build.py
"""
import calendar, gzip, os, re, shutil, sqlite3, sys, tempfile, time
from datetime import datetime, timezone
from multiprocessing import Pool

import argparse
HERE = os.path.dirname(os.path.abspath(__file__))
_ap = argparse.ArgumentParser(description="Merge support-package node logs into a SQLite database")
_ap.add_argument("package", nargs="?", default=os.path.dirname(HERE),
                 help="support package .tar.gz or extracted directory (default: parent of this script's dir)")
_ap.add_argument("stage", nargs="?", default="all", choices=["all", "load", "index", "export", "finish"])
_ap.add_argument("-o", "--output", default=os.path.join(HERE, "logs.db"),
                 help="where to write the database (default: logs.db next to this script)")
_args = _ap.parse_args()
PKG = os.path.abspath(os.path.expanduser(_args.package))
DB = os.path.abspath(os.path.expanduser(_args.output))
OUTDIR = os.path.dirname(DB)       # merged.log (export stage) is written alongside the database
if os.path.isdir(DB) or _args.output.endswith(("/", os.sep)) or not os.path.splitext(DB)[1]:
    # -o looks like a directory (exists, trailing slash, or no file extension): use logs.db inside it
    DB = os.path.join(DB, "logs.db"); OUTDIR = os.path.dirname(DB)
os.makedirs(OUTDIR, exist_ok=True)
MAXMSG = 4000
DEFAULT_YEAR = 2026
MON = {m: i for i, m in enumerate(calendar.month_abbr) if m}

# (regex, handler) -- first match wins; group layout handled per handler
R_ISO = re.compile(r'^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:[.,](\d{1,6}))?')
R_JSON = re.compile(r'^\{"timestamp": "(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)"')
R_BRK = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:[.,](\d{1,6}))?')
R_DMC = re.compile(r'^\s*\d+\) (\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(?:\.(\d{1,6}))?')
R_REDIS = re.compile(r'^\d+:[A-Z] (\d\d) (\w{3}) (\d{4}) (\d\d):(\d\d):(\d\d)(?:\.(\d{1,3}))?')
R_SLASH = re.compile(r'^\w+: (?:pid=\d+ )?(\d{4})/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)(?:\.(\d{1,6}))?')
R_SYSLOG = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d) ')
R_PREFIX = re.compile(r'^\d+\) ')  # dmcproxy "123) " line counters

R_LEVEL = re.compile(
    r'\b(CRITICAL|FATAL|ERROR|ERR|WARNING|WARN|NOTICE|INFO|INF|DEBUG|DBG|TRACE|http)\b|\[(critical|error|warning|warn|info|debug|trace)\]')
LV = {"CRITICAL": "CRIT", "FATAL": "CRIT", "ERROR": "ERROR", "ERR": "ERROR", "WARNING": "WARN",
      "WARN": "WARN", "NOTICE": "INFO", "INFO": "INFO", "INF": "INFO", "DEBUG": "DEBUG",
      "DBG": "DEBUG", "TRACE": "DEBUG", "HTTP": "INFO"}
R_REDISLV = re.compile(r'^\d+:[A-Z] .{20,26}? ([.\-*#]) ')
REDISLV = {".": "DEBUG", "-": "DEBUG", "*": "INFO", "#": "WARN"}
R_ROT = re.compile(r'(\.\d+)?(\.gz)?$')


def ms(y, mo, d, h, mi, s, frac):
    t = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s)))
    f = int((frac or "0").ljust(3, "0")[:3])
    return t * 1000 + f


def parse_ts(line):
    """Return epoch-ms or None."""
    c = line[:1]
    if c.isdigit() or c == " ":
        m = R_ISO.match(line)
        if m:
            return ms(*m.groups())
        m = R_DMC.match(line)
        if m:
            return ms(*m.groups())
        m = R_REDIS.match(line)
        if m:
            d, mon, y, h, mi, s, fr = m.groups()
            return ms(y, MON.get(mon, 1), d, h, mi, s, fr)
        return None
    if c == "[":
        m = R_BRK.match(line)
        return ms(*m.groups()) if m else None
    if c == "{":
        m = R_JSON.match(line)
        return ms(*m.groups(), None) if m else None
    if c.isalpha():
        m = R_SLASH.match(line)
        if m:
            return ms(*m.groups())
        m = R_SYSLOG.match(line)
        if m and m.group(1) in MON:
            mon, d, h, mi, s = m.groups()
            return ms(DEFAULT_YEAR, MON[mon], d, h, mi, s, None)
    return None


def level_of(line):
    m = R_REDISLV.match(line)
    if m:
        return REDISLV[m.group(1)]
    head = line[:160]
    m = R_LEVEL.search(head)
    if m:
        return LV[(m.group(1) or m.group(2)).upper()]
    if '"HTTP/' in head:  # envoy access log: classify by status code
        m = re.search(r'" (\d{3}) ', head)
        if m:
            return "ERROR" if m.group(1)[0] == "5" else ("WARN" if m.group(1)[0] == "4" else "INFO")
    return ""


def logname(fn):
    n = R_ROT.sub("", fn)           # strip .N.gz
    n = re.sub(r'\.log$', '', n)
    return n


def process(args):
    node, path = args
    fn = os.path.basename(path)
    name = logname(fn)
    op = gzip.open if fn.endswith(".gz") else open
    rows, last, pending = [], None, []
    try:
        with op(path, "rt", errors="replace", newline="\n") as fh:
            for line in fh:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                t = parse_ts(line)
                if t is None:
                    t = last
                else:
                    last = t
                    if pending:   # leading undated lines take first real timestamp
                        rows.extend((t, node, name, lv, m) for lv, m in pending)
                        pending = []
                if t is None:
                    pending.append((level_of(line), line[:MAXMSG]))
                    continue
                rows.append((t, node, name, level_of(line), line[:MAXMSG]))
    except Exception as e:
        sys.stderr.write(f"skip {path}: {e}\n")
    return path, rows, len(pending)


R_MEMBER = re.compile(r'(?:^|/)node_(\d+)/logs/([^/]+)$')


def extract_logs(tgz, dest):
    """Stream a .tar.gz support package, extracting only node_N/logs/* files into dest.
    Works whatever the top-level directory is called."""
    import tarfile
    os.makedirs(dest, exist_ok=True)
    n = 0
    with tarfile.open(tgz, "r|gz") as tf:
        for m in tf:
            mm = R_MEMBER.search(m.name)
            if not (m.isfile() and mm):
                continue
            d = os.path.join(dest, f"node_{mm.group(1)}", "logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, mm.group(2)), "wb") as out:
                shutil.copyfileobj(tf.extractfile(m), out)
            n += 1
            if n % 500 == 0:
                print(f"  extracted {n} files", flush=True)
    print(f"extracted {n} log files from {os.path.basename(tgz)}", flush=True)


def main():
    stage = _args.stage
    if stage != 'all' and stage != 'load': return later(stage)
    src = PKG
    if os.path.isfile(src):                      # a .tar.gz / .tgz support package
        pkg = os.path.join(tempfile.gettempdir(), "logdash_extract")
        if stage == 'all' or not os.path.isdir(pkg):    # 'load' reuses a previous extraction
            shutil.rmtree(pkg, ignore_errors=True)
            extract_logs(src, pkg)
        src = pkg
    jobs = []
    for d in sorted(os.listdir(src)):
        m = re.fullmatch(r"node_(\d+)", d)
        ld = os.path.join(src, d, "logs")
        if m and os.path.isdir(ld):
            for fn in sorted(os.listdir(ld)):
                p = os.path.join(ld, fn)
                if os.path.isfile(p):
                    jobs.append((int(m.group(1)), p))
    if not jobs:
        sys.exit(f"no node_N/logs/ files found in {PKG}")
    print(f"{len(jobs)} files from {PKG}", flush=True)
    WORK = os.path.join(tempfile.gettempdir(), "logdash_build.db")   # build locally, copy at end
    if os.path.exists(WORK):
        os.remove(WORK)
    db = sqlite3.connect(WORK)
    db.execute("PRAGMA journal_mode=OFF"); db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY, ts INTEGER, node INTEGER, log TEXT, level TEXT, msg TEXT)")
    t0, n, orphan = time.time(), 0, 0
    jobs.sort(key=lambda j: -os.path.getsize(j[1]))
    with Pool() as pool:
        for i, (path, rows, orph) in enumerate(pool.imap_unordered(process, jobs, chunksize=1), 1):
            db.executemany("INSERT INTO logs(ts,node,log,level,msg) VALUES(?,?,?,?,?)", rows)
            n += len(rows); orphan += orph
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} files, {n:,} lines, {time.time()-t0:.0f}s", flush=True)
    db.commit(); db.close()
    print(f"loaded {n:,} lines ({orphan} undated dropped)", flush=True)
    if stage == 'all':
        for st in ('index', 'finish'): later(st)   # run 'export' stage separately for merged.log
        shutil.rmtree(os.path.join(tempfile.gettempdir(), "logdash_extract"), ignore_errors=True)


def later(stage):
    import shutil, tempfile
    WORK = os.path.join(tempfile.gettempdir(), "logdash_build.db")
    db = sqlite3.connect(WORK); db.execute("PRAGMA synchronous=OFF"); db.execute("PRAGMA journal_mode=OFF")
    if stage == 'index':   # physically sort by time so rowid order == time order, then index
        db.execute("CREATE TABLE s AS SELECT ts,node,log,level,msg FROM logs ORDER BY ts,node,log,id")
        db.execute("DROP TABLE logs"); db.execute("ALTER TABLE s RENAME TO logs")
        db.execute("CREATE INDEX i_ts ON logs(ts)")
        db.execute("CREATE INDEX i_node_ts ON logs(node,ts)")
        db.execute("CREATE INDEX i_log_ts ON logs(log,ts)")
        db.commit()
    elif stage == 'export':
        with open(os.path.join(OUTDIR, "merged.log"), "w") as f:
            for ts, node, log, lv, msg in db.execute("SELECT ts,node,log,level,msg FROM logs ORDER BY rowid"):
                t = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                f.write(f"{t} node_{node} {log} {msg}\n")
    elif stage == 'finish':
        db.close(); shutil.copyfile(WORK, DB); os.remove(WORK)
    print("stage", stage, "done", flush=True)


if __name__ == "__main__":
    main()
