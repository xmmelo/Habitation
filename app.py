
#!/usr/bin/env python3
"""
House debt dashboard.
 
Stores settings, payers, monthly entries and a Euribor cache in a local SQLite
file (debt.db) and calculates the loan on its own.
 
- Interest rate = Euribor + spread.
- Payers are defined in setup, each with an amount already paid toward the loan
  before this dashboard was used. That amount reduces the opening debt.
 
Run:  python3 app.py   then open http://127.0.0.1:8000
Only the Python standard library is used.
"""
import csv
import io
import json
import os
import re
import sqlite3
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
 
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "debt.db")
HTML_PATH = os.path.join(BASE, "index.html")
HOST, PORT = "127.0.0.1", 8000
 
# Euribor 6-month monthly averages from the ECB Data Portal (chart only).
ECB_EURIBOR_URL = (
    "https://data-api.ecb.europa.eu/service/data/FM/"
    "M.U2.EUR.RT.MM.EURIBOR6MD_.HSTA?lastNObservations=12&format=csvdata"
)
EURIBOR_MAX_AGE = timedelta(hours=12)
 
DEFAULT_SETTINGS = {
    "setup_done": False,
    "principal": 200000.0,     # debt value (original loan)
    "euribor_rate": 2.00,      # Euribor value used in the loan, % per year
    "spread": 0.45,            # bank spread, % per year
    "insurance": 45.0,         # monthly insurance
    "other_monthly": 0.0,      # other monthly costs
    "term_months": 300,        # loan term in months
    "start_month": "2023-01",  # first installment month, YYYY-MM
    "euribor_fetched_at": "",  # internal: last successful ECB refresh
}
 
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
NAME_MAX = 40
 
 
class BadRequest(Exception):
    """Invalid input; returned to the client as HTTP 400."""
 
 
# ---------- database ----------
 
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
 
 
def init_db():
    with closing(connect()) as c, c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS euribor (month TEXT PRIMARY KEY, rate REAL NOT NULL)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS payers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT UNIQUE NOT NULL,
                already_paid REAL NOT NULL DEFAULT 0
            )""")
 
        # Migration 1: payers gained an "already paid" column.
        pcols = [r[1] for r in c.execute("PRAGMA table_info(payers)")]
        if "already_paid" not in pcols:
            c.execute("ALTER TABLE payers ADD COLUMN already_paid REAL NOT NULL DEFAULT 0")
 
        # Migration 2: single-user "entries" with amounts -> per-payer lines.
        ecols = [r[1] for r in c.execute("PRAGMA table_info(entries)")]
        legacy = "down_payment" in ecols
        if legacy:
            c.execute("ALTER TABLE entries RENAME TO entries_legacy")
        c.execute("CREATE TABLE IF NOT EXISTS entries (month TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS entry_lines (
                month        TEXT NOT NULL,
                payer_id     INTEGER REFERENCES payers(id),
                payment      REAL NOT NULL DEFAULT 0,
                down_payment REAL NOT NULL DEFAULT 0,
                saved_amount REAL NOT NULL DEFAULT 0
            )""")
        if legacy:
            c.execute("INSERT OR IGNORE INTO entries (month) SELECT month FROM entries_legacy")
            c.execute("""
                INSERT INTO entry_lines (month, payer_id, payment, down_payment, saved_amount)
                SELECT month, NULL, 0, down_payment, saved_amount FROM entries_legacy
            """)
 
        # Migration 3: single "annual_rate" -> Euribor + spread.
        keys = {r[0] for r in c.execute("SELECT key FROM settings")}
        if "spread" not in keys and "annual_rate" in keys:
            old = json.loads(c.execute("SELECT value FROM settings WHERE key = 'annual_rate'").fetchone()[0])
            spread = round(old - DEFAULT_SETTINGS["euribor_rate"], 4)
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('spread', ?)", (json.dumps(spread),))
 
        for key, value in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
 
 
def read_settings(c):
    settings = dict(DEFAULT_SETTINGS)
    for row in c.execute("SELECT key, value FROM settings"):
        if row["key"] in settings:
            settings[row["key"]] = json.loads(row["value"])
    return settings
 
 
# ---------- helpers ----------
 
def parse_month(ym):
    y, m = ym.split("-")
    return int(y) * 12 + (int(m) - 1)
 
 
def month_str(index):
    return f"{index // 12:04d}-{index % 12 + 1:02d}"
 
 
def check_month(value):
    value = str(value or "")
    if not MONTH_RE.match(value):
        raise BadRequest("month must be YYYY-MM")
    return value
 
 
def number(value, name):
    try:
        x = float(value if value not in (None, "") else 0)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number")
    if x < 0:
        raise BadRequest(f"{name} cannot be negative")
    return x
 
 
# ---------- Euribor (ECB chart) ----------
 
def fetch_euribor():
    req = urllib.request.Request(ECB_EURIBOR_URL, headers={"Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        text = resp.read().decode("utf-8")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        period, value = row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if period and value and MONTH_RE.match(period):
            rows.append((period, float(value)))
    if not rows:
        raise ValueError("no Euribor observations returned")
    with closing(connect()) as c, c:
        c.executemany("INSERT OR REPLACE INTO euribor (month, rate) VALUES (?, ?)", rows)
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('euribor_fetched_at', ?)",
                  (json.dumps(datetime.now().isoformat(timespec="seconds")),))
 
 
def euribor_block():
    error = None
    with closing(connect()) as c:
        last = read_settings(c)["euribor_fetched_at"]
    stale = True
    if last:
        try:
            stale = datetime.now() - datetime.fromisoformat(last) > EURIBOR_MAX_AGE
        except ValueError:
            stale = True
    if stale:
        try:
            fetch_euribor()
        except Exception as exc:
            error = f"Could not refresh Euribor from the ECB ({exc}). Showing saved values."
    with closing(connect()) as c:
        rows = c.execute("SELECT month, rate FROM euribor ORDER BY month DESC LIMIT 6").fetchall()
    series = [{"month": r["month"], "rate": r["rate"]} for r in reversed(rows)]
    return {"series": series, "latest": series[-1] if series else None, "error": error}
 
 
# ---------- loan calculation ----------
 
def installment(principal, annual_rate, n_months):
    r = annual_rate / 100 / 12
    if r == 0:
        return principal / n_months
    return principal * r / (1 - (1 + r) ** -n_months)
 
 
def compute(settings, monthly_entries, already_paid_total):
    """
    monthly_entries: list of {month, down_payment, saved_amount}, summed over payers.
    already_paid_total: amount already repaid by payers before tracking started.
    """
    principal = float(settings["principal"])
    annual_rate = float(settings["euribor_rate"]) + float(settings["spread"])
    n = int(settings["term_months"])
    insurance = float(settings["insurance"])
    other = float(settings["other_monthly"])
    r = annual_rate / 100 / 12
    pmt = installment(principal, annual_rate, n)
    start = parse_month(settings["start_month"])
 
    by_month = {e["month"]: e for e in monthly_entries}
    last = max((parse_month(e["month"]) for e in monthly_entries), default=start - 1)
 
    # The opening balance is the loan minus what has already been repaid.
    balance = max(0.0, principal - already_paid_total)
    interest_paid = 0.0
    for idx in range(start, last + 1):
        if balance <= 0:
            break
        extra = float(by_month.get(month_str(idx), {}).get("down_payment", 0) or 0)
        interest = balance * r
        payment = min(pmt, balance + interest)
        balance = max(0.0, balance + interest - payment - extra)
        interest_paid += interest
 
    next_idx = max(last + 1, start)
    months_left, b = 0, balance
    while b > 0 and months_left < 2000:
        b = max(0.0, b * (1 + r) - min(pmt, b * (1 + r)))
        months_left += 1
    end_month = month_str(next_idx + months_left - 1) if months_left else None
 
    installment_now = min(pmt, balance * (1 + r)) if balance > 0 else 0.0
    active = balance > 0

    total =  round(installment_now + ((insurance + other) if active else 0.0), 2)
    installment = round(installment_now, 2)
 
    return {
        "debt": round(balance, 2),
        "original": principal,
        "already_paid": round(already_paid_total, 2),
        "paid_pct": (principal - balance) / principal if principal > 0 else 0,
        "interest_paid": round(interest_paid, 2),
        "months_left": months_left,
        "end_month": end_month,
        "interest_rate": round(annual_rate, 4),
        "unapplied_savings": round(sum(float(e["saved_amount"]) for e in monthly_entries), 2),
        "next_month": month_str(next_idx),
        "monthly": {
            "installment": round(installment_now, 2),
            "insurance": round(insurance if active else 0.0, 2),
            "other": round(other if active else 0.0, 2),
            "total": round(installment_now + ((insurance + other) if active else 0.0), 2),
            "total_per_payer": round(total / 2, 2)
        },
        "insurance": round(insurance, 2),
    }
 
 
# ---------- state ----------
 
def build_state():
    with closing(connect()) as c:
        settings = read_settings(c)
        payers = [dict(r) for r in c.execute("SELECT id, name, already_paid FROM payers ORDER BY id")]
        months = [r["month"] for r in c.execute("SELECT month FROM entries ORDER BY month")]
        lines = [dict(r) for r in c.execute(
            "SELECT month, payer_id, payment, down_payment, saved_amount FROM entry_lines")]
 
    names = {p["id"]: p["name"] for p in payers}
 
    monthly = {m: {"month": m, "down_payment": 0.0, "saved_amount": 0.0, "lines": []} for m in months}
    totals = {p["id"]: {"id": p["id"], "name": p["name"], "already_paid": float(p["already_paid"]),
                        "payment": 0.0, "down_payment": 0.0, "saved_amount": 0.0} for p in payers}
    for l in lines:
        if l["month"] not in monthly:
            continue
        m = monthly[l["month"]]
        m["down_payment"] += l["down_payment"]
        m["saved_amount"] += l["saved_amount"]
        pname = "Unassigned" if l["payer_id"] is None else names.get(l["payer_id"], "Unknown")
        m["lines"].append({
            "payer_id": l["payer_id"],
            "payer_name": pname,
            "payment": l["payment"],
            "down_payment": l["down_payment"],
            "saved_amount": l["saved_amount"],
        })
        if l["payer_id"] not in totals:
            totals[l["payer_id"]] = {"id": None, "name": "Unassigned", "already_paid": 0.0,
                                     "payment": 0.0, "down_payment": 0.0, "saved_amount": 0.0}
        t = totals[l["payer_id"]]
        t["payment"] += l["payment"]
        t["down_payment"] += l["down_payment"]
        t["saved_amount"] += l["saved_amount"]
 
    entries_out = [dict(m, lines=sorted(m["lines"], key=lambda x: x["payer_name"]))
                   for m in sorted(monthly.values(), key=lambda m: m["month"], reverse=True)]
    payer_totals = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()}
                    for t in totals.values()]
 
    already_total = sum(float(p["already_paid"]) for p in payers)
    public_settings = {k: v for k, v in settings.items() if k != "euribor_fetched_at"}
    public_settings["annual_rate"] = round(float(settings["euribor_rate"]) + float(settings["spread"]), 4)
 
    return {
        "settings": public_settings,
        "payers": [{"id": p["id"], "name": p["name"], "already_paid": float(p["already_paid"])} for p in payers],
        "payer_totals": payer_totals,
        "entries": entries_out,
        "dashboard": compute(settings, list(monthly.values()), already_total),
        "euribor": euribor_block(),
    }
 
 
# ---------- HTTP ----------
 
class Handler(BaseHTTPRequestHandler):
 
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
 
    def _error(self, code, message):
        self._send(code, {"error": message})
 
    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body
 
    def _run(self, fn):
        try:
            fn()
        except BadRequest as exc:
            self._error(400, str(exc))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._error(400, f"invalid input: {exc}")
 
    # GET
    def do_GET(self):
        self._run(self._get)
 
    def _get(self):
        path = urlparse(self.path).path
        if path == "/":
            with open(HTML_PATH, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, build_state())
        self._error(404, "not found")
 
    # POST: monthly entries
    def do_POST(self):
        self._run(self._post)
 
    def _post(self):
        if urlparse(self.path).path != "/api/entries":
            return self._error(404, "not found")
        body = self._body()
        month = check_month(body.get("month"))
        raw_lines = body.get("lines") or []
        if not isinstance(raw_lines, list):
            raise BadRequest("lines must be a list")
 
        with closing(connect()) as c, c:
            valid_ids = {r["id"] for r in c.execute("SELECT id FROM payers")}
            rows = []
            for line in raw_lines:
                if not isinstance(line, dict):
                    raise BadRequest("each line must be an object")
                pid = int(line.get("payer_id"))
                if pid not in valid_ids:
                    raise BadRequest("unknown payer")
                payment = number(line.get("payment"), "monthly payment")
                down = number(line.get("down_payment"), "down payment")
                saved = number(line.get("saved_amount"), "saved amount")
                if payment or down or saved:
                    rows.append((month, pid, payment, down, saved))
 
            c.execute("DELETE FROM entry_lines WHERE month = ?", (month,))
            c.executemany(
                "INSERT INTO entry_lines (month, payer_id, payment, down_payment, saved_amount) "
                "VALUES (?, ?, ?, ?, ?)", rows)
            if rows:
                c.execute("INSERT OR IGNORE INTO entries (month) VALUES (?)", (month,))
            else:
                c.execute("DELETE FROM entries WHERE month = ?", (month,))
        self._send(200, build_state())
 
    # PUT: settings and payers
    def do_PUT(self):
        self._run(self._put)
 
    def _put(self):
        path = urlparse(self.path).path
        if path == "/api/settings":
            return self._put_settings()
        if path == "/api/payers":
            return self._put_payers()
        self._error(404, "not found")
 
    def _put_settings(self):
        body = self._body()
        updates = {}
        if "setup_done" in body:
            updates["setup_done"] = bool(body["setup_done"])
        if "start_month" in body:
            updates["start_month"] = check_month(body["start_month"])
        if "term_months" in body:
            term = int(body["term_months"])
            if term <= 0:
                raise BadRequest("term must be a positive number of months")
            updates["term_months"] = term
        for key, label in (("principal", "debt value"), ("euribor_rate", "Euribor"),
                           ("spread", "spread"), ("insurance", "insurance"),
                           ("other_monthly", "other monthly costs")):
            if key in body:
                updates[key] = number(body[key], label)
 
        with closing(connect()) as c, c:
            current = read_settings(c)
            principal = updates.get("principal", float(current["principal"]))
            already_total = c.execute("SELECT COALESCE(SUM(already_paid), 0) FROM payers").fetchone()[0]
            if already_total > principal:
                raise BadRequest("payers' already-paid amounts exceed the debt value")
            if updates.get("setup_done"):
                if c.execute("SELECT COUNT(*) FROM payers").fetchone()[0] == 0:
                    raise BadRequest("add at least one payer before finishing setup")
            for key, value in updates.items():
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                          (key, json.dumps(value)))
        self._send(200, build_state())
 
    def _put_payers(self):
        body = self._body()
        raw = body.get("payers")
        if not isinstance(raw, list) or not raw:
            raise BadRequest("add at least one payer")
 
        rows, seen = [], set()
        for item in raw:
            if not isinstance(item, dict):
                raise BadRequest("invalid payer")
            name = str(item.get("name", "")).strip()
            if not 1 <= len(name) <= NAME_MAX:
                raise BadRequest(f"payer names must have 1 to {NAME_MAX} characters")
            if name.lower() in seen:
                raise BadRequest(f"duplicate payer name: {name}")
            seen.add(name.lower())
            already = number(item.get("already_paid"), f"already paid by {name}")
            pid = item.get("id")
            pid = int(pid) if pid not in (None, "") else None
            rows.append((pid, name, already))
 
        with closing(connect()) as c, c:
            principal = float(read_settings(c)["principal"])
            if sum(r[2] for r in rows) > principal:
                raise BadRequest("total already paid cannot exceed the debt value")
 
            existing = {r["id"] for r in c.execute("SELECT id FROM payers")}
            incoming = {r[0] for r in rows if r[0] is not None}
            if not incoming <= existing:
                raise BadRequest("unknown payer")
 
            removed = existing - incoming
            for pid in removed:
                used = c.execute("SELECT COUNT(*) FROM entry_lines WHERE payer_id = ?", (pid,)).fetchone()[0]
                if used:
                    raise BadRequest("a payer with monthly entries cannot be removed. Delete those months first.")
            c.executemany("DELETE FROM payers WHERE id = ?", [(pid,) for pid in removed])
 
            try:
                for pid, name, already in rows:
                    if pid is None:
                        c.execute("INSERT INTO payers (name, already_paid) VALUES (?, ?)", (name, already))
                    else:
                        c.execute("UPDATE payers SET name = ?, already_paid = ? WHERE id = ?",
                                  (name, already, pid))
            except sqlite3.IntegrityError:
                raise BadRequest("a payer with that name already exists")
        self._send(200, build_state())
 
    # DELETE: monthly entries
    def do_DELETE(self):
        self._run(self._delete)
 
    def _delete(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/entries/"):
            return self._error(404, "not found")
        month = check_month(path.rsplit("/", 1)[-1])
        with closing(connect()) as c, c:
            c.execute("DELETE FROM entry_lines WHERE month = ?", (month,))
            c.execute("DELETE FROM entries WHERE month = ?", (month,))
        self._send(200, build_state())
 
    def log_message(self, fmt, *args):
        pass
 
 
if __name__ == "__main__":
    init_db()
    server = HTTPServer((HOST, PORT), Handler)
    print(f"House debt dashboard running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
 