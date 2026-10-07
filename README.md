# logdash – Redis Enterprise support-package log timeline

Merges every `node_N/logs/*` file from a Redis Enterprise support package (including rotated `.gz` files) into one time-sorted database, and serves a local dashboard to explore it. Every line is shown as `time node log message`.

Requires Python 3.8+ (standard library only). No internet access needed.

## Files

| File | Purpose |
|---|---|
| `build.py` | Reads a support package and builds `logs.db` (and optionally `merged.log`) |
| `server.py` | Serves the dashboard and a small JSON API over a `logs.db` |
| `dashboard.html` | The UI (served by `server.py`; don't open it directly) |

## 1. Build the database

```
python3 build.py ~/Downloads/debuginfo.010DB0726D35FCBF.tar.gz
```

`build.py` accepts either a `.tar.gz` support package or an already-extracted package directory. It extracts only `node_N/logs/*` files (to a temp folder, deleted afterwards), parses them, sorts by time, indexes, and writes the database.

Choose where the database goes with `-o` / `--output`:

```
python3 build.py <package> -o /data/case123/logs.db   # exact file path
python3 build.py <package> -o /data/case123           # a directory: writes /data/case123/logs.db
```

A value is treated as a directory if it already exists as one, ends in `/`, or has no file extension.

Without `-o` it writes `logs.db` next to `build.py`. Missing directories are created. If you use `-o` with a staged build (below), pass the same `-o` to every stage. Then start the server with the same path: `python3 server.py /data/case123/logs.db`.

Notes:

- Allow several minutes and a few GB of free disk space for a large package (5.5M lines produced a ~2.5 GB database). The work is done in the system temp folder first, then the finished database is copied to the output location.
- An existing database at the output path is overwritten.
- The default build does not write `merged.log` (slow). The dashboard's **Export filtered** button produces the same `time node log message` text for any filter, including no filter.
- A normal build does everything in one command. If you also want `merged.log`, or your environment limits how long one command may run, run the build as separate stages instead (see below).

### Running the build in stages

A normal build is four steps run back to back. Each can be run on its own by adding its name after the package:

| Stage | What it does |
|---|---|
| `load` | Extracts the logs from the package and parses them into a temporary working database (unsorted). |
| `index` | Sorts the working database by time and adds the indexes the dashboard needs. |
| `export` | Optional, and not part of a normal build. Writes `merged.log`, a plain-text file of `time node log message` lines, into the same directory as the database. |
| `finish` | Copies the working database to its final location (`logs.db`, or whatever `-o` says) and deletes the working copy. |

Run them in this order, repeating the same package and `-o` on each command:

```
python3 build.py <package> load   -o /data/case123
python3 build.py <package> index  -o /data/case123
python3 build.py <package> export -o /data/case123     # optional: only if you want merged.log
python3 build.py <package> finish -o /data/case123
```

`export` must come after `index` (so the lines are in time order) and before `finish` (which removes the working database it reads from). You can skip `export`, in which case this is just a normal build split into pieces. The extracted logs are only deleted by a normal one-command build, so after a staged build you can remove the `logdash_extract` folder in your system temp directory yourself.

### How lines are parsed

- About ten timestamp formats are recognised (Python-style logs, Redis server logs, dmcproxy, Envoy, JSON logs from `cm_server`, syslog-style, etc.).
- Lines without a timestamp (stack traces, wrapped output) take the time of the preceding line. Undated lines at the very start of a file take the first dated line's time; if a file has no dated lines they are dropped.
- All times are treated as **UTC** (the logs carry no zone). Syslog-style lines with no year (e.g. `pdns_server_stderr`) assume 2026; change `DEFAULT_YEAR` in `build.py` for other years.
- Rotated copies share a log name: `cluster_wd.log.3.gz` appears as `cluster_wd`.
- Level (CRIT/ERROR/WARN/INFO/DEBUG) is inferred from the line text; Envoy access lines are classed by HTTP status (5xx = ERROR, 4xx = WARN). Lines with no recognisable level show as `NONE`.
- Messages are truncated to 4,000 characters.

## 2. Start the dashboard

```
python3 server.py                          # uses logs.db next to server.py
python3 server.py /path/to/logs.db         # explicit database
python3 server.py /path/to/logs.db -p 9000 # different port (default 6424)
```

Then open <http://localhost:6424>. Options:

| Option | Meaning |
|---|---|
| `db` (positional) | Path to a `logs.db` built by `build.py` |
| `-p`, `--port` | Port (default 6424) |
| `--host` | Bind address (default `127.0.0.1`; use `0.0.0.0` to share on your network — the data is unauthenticated) |

The database is opened read-only, so several packages can be served at once from different ports.

## 3. Using the dashboard

**Timeline chart** – stacked bars of line counts over time.

- Choose **Colour by** node, log or level.
- **Drag** across the chart to zoom into a time range; **Reset zoom** returns to the full range.
- Edit the **from / to** boxes (UTC, `YYYY-MM-DD HH:MM:SS`) and click **Apply** to jump to an exact window.
- Hover a bar to see per-series counts for that bucket.

**Filters (left panel)** – Nodes, Levels and Logs, each with line counts.

- **Click** toggles one item; **click and drag** paints the same on/off state across items.
- **Shift-click** selects or clears a range; **Alt-click** keeps only that item.
- **all / none / invert** act on the items currently shown, so type in the log filter box first to act on just the matching logs (e.g. `redis-` then *none*).
- Changes apply after a short pause, so you can click several items in a row.

**Message search** – the "message contains…" box does a case-insensitive substring match; press Enter or **Apply**.

**Log table** – matching lines in time order with columns Time, Node, Log, Level, Message.

- Use **Prev / Next** to page (200 rows per page) and **newest first** to reverse order.
- Click a row to expand a long message.
- ERROR/CRIT lines are tinted red, WARN lines amber.

**Export filtered** – downloads every line matching the current filters, time range and search as `merged.log` (`time node_N log message`).

## Troubleshooting

- `no node_N/logs/ files found` – the package doesn't contain `node_N/logs/` directly (e.g. nested per-node archives). Extract those first and pass the directory.
- `database not found` – check the path passed to `server.py`, or run `build.py` first.
- Searches on a very large database can take several seconds, since they scan message text; narrow the time range or logs first to speed them up.
