"""Kostendeler - kosten delen met huisgenoten, als Home Assistant add-on.

Alleen Python-standaardbibliotheek: http.server + sqlite3.
Gebruikers worden herkend via de headers die Home Assistant Ingress meestuurt
(X-Remote-User-Id / X-Remote-User-Display-Name).
"""

import json
import mimetypes
import os
import re
import sqlite3
import calendar
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("DB_PATH", "/data/kostendeler.db")
PORT = int(os.environ.get("PORT", "8099"))
# Ingress-verkeer komt altijd van de Supervisor-proxy. "*" = alles toestaan (alleen voor testen).
ALLOWED_IPS = [ip.strip() for ip in os.environ.get("ALLOWED_IPS", "172.30.32.2").split(",")]
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

mimetypes.add_type("font/woff2", ".woff2")

DEFAULT_CATEGORIES = [
    ("Boodschappen", "cart"),
    ("Schoonmaak", "sparkle"),
    ("Drogisterij", "bottle"),
    ("Huis & inrichting", "sofa"),
    ("Eten bestellen", "utensils"),
    ("Overig", "box"),
]
# Oude emoji-iconen (versie 1.x) omzetten naar icoonnamen.
EMOJI_TO_ICON = {"🛒": "cart", "🧽": "sparkle", "🧴": "bottle", "🛋️": "sofa", "🛋": "sofa",
                 "🍕": "utensils", "📦": "box"}

SCHEMA_VERSION = 6
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_AMOUNT = 100_000_00  # 100.000 euro in centen


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- database

def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = connect()
    db.execute("PRAGMA journal_mode = WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT 'box',
                archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                amount INTEGER NOT NULL,
                paid_by TEXT NOT NULL REFERENCES users(id),
                date TEXT NOT NULL,
                category_id INTEGER REFERENCES categories(id),
                created_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS expense_shares(
                expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                amount INTEGER NOT NULL,
                PRIMARY KEY(expense_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS settlements(
                id INTEGER PRIMARY KEY,
                from_user TEXT NOT NULL REFERENCES users(id),
                to_user TEXT NOT NULL REFERENCES users(id),
                amount INTEGER NOT NULL,
                date TEXT NOT NULL,
                created_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date);
            CREATE INDEX IF NOT EXISTS idx_settlements_date ON settlements(date);
            """
        )
        if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            db.executemany("INSERT INTO categories(name, icon) VALUES (?, ?)", DEFAULT_CATEGORIES)
    if version < 2:
        for row in db.execute("SELECT id, icon FROM categories").fetchall():
            if not re.fullmatch(r"[a-z]+", row["icon"] or ""):
                db.execute("UPDATE categories SET icon = ? WHERE id = ?",
                           (EMOJI_TO_ICON.get(row["icon"], "box"), row["id"]))
    if version < 3:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS recurring(
                id INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                amount INTEGER NOT NULL,
                paid_by TEXT NOT NULL REFERENCES users(id),
                category_id INTEGER REFERENCES categories(id),
                day INTEGER NOT NULL,
                start_month TEXT NOT NULL,
                last_month TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS recurring_shares(
                recurring_id INTEGER NOT NULL REFERENCES recurring(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                amount INTEGER NOT NULL,
                PRIMARY KEY(recurring_id, user_id)
            );
            ALTER TABLE expenses ADD COLUMN recurring_id INTEGER REFERENCES recurring(id) ON DELETE SET NULL;
            ALTER TABLE expenses ADD COLUMN recurring_month TEXT;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_recurring
                ON expenses(recurring_id, recurring_month) WHERE recurring_id IS NOT NULL;
            """
        )
        if not db.execute("SELECT 1 FROM categories WHERE name = 'Vaste lasten'").fetchone():
            db.execute("INSERT INTO categories(name, icon) VALUES ('Vaste lasten', 'home')")
    if version < 4:
        db.executescript(
            """
            ALTER TABLE users ADD COLUMN notify_service TEXT;
            ALTER TABLE users ADD COLUMN monthly_report INTEGER NOT NULL DEFAULT 1;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # Eerste maandrapport pas op de eerstvolgende 1e, niet direct na installatie.
        kv_set(db, "last_report", month_add(date.today().strftime("%Y-%m"), -1))
    if version < 5:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                assigned_to TEXT REFERENCES users(id),
                due_date TEXT,
                repeat_unit TEXT,
                repeat_every INTEGER NOT NULL DEFAULT 1,
                done INTEGER NOT NULL DEFAULT 0,
                done_at TEXT,
                done_by TEXT,
                created_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(done, due_date);
            """
        )
    if version < 6:
        db.executescript(
            """
            ALTER TABLE users ADD COLUMN task_reminder INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE users ADD COLUMN task_reminder_hour INTEGER NOT NULL DEFAULT 8;
            ALTER TABLE users ADD COLUMN task_assigned INTEGER NOT NULL DEFAULT 1;
            """
        )
        # Niet meteen op de dag van de update een herinnering sturen.
        for (uid,) in db.execute("SELECT id FROM users").fetchall():
            kv_set(db, f"task_reminder:{uid}", date.today().isoformat())
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()
    db.close()


def user_ids(db):
    return [r["id"] for r in db.execute("SELECT id FROM users")]


def list_users(db):
    return [dict(r) for r in db.execute(
        "SELECT id, name, notify_service, monthly_report, task_reminder, task_reminder_hour, task_assigned "
        "FROM users ORDER BY created_at, name")]


def kv_get(db, key):
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(db, key, value):
    db.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))


def list_categories(db):
    return [dict(r) for r in db.execute(
        "SELECT id, name, icon, archived FROM categories ORDER BY archived, id")]


def balances(db):
    bal = {uid: 0 for uid in user_ids(db)}
    for r in db.execute("SELECT paid_by, SUM(amount) s FROM expenses GROUP BY paid_by"):
        bal[r["paid_by"]] = bal.get(r["paid_by"], 0) + r["s"]
    for r in db.execute("SELECT user_id, SUM(amount) s FROM expense_shares GROUP BY user_id"):
        bal[r["user_id"]] = bal.get(r["user_id"], 0) - r["s"]
    for r in db.execute("SELECT from_user, SUM(amount) s FROM settlements GROUP BY from_user"):
        bal[r["from_user"]] = bal.get(r["from_user"], 0) + r["s"]
    for r in db.execute("SELECT to_user, SUM(amount) s FROM settlements GROUP BY to_user"):
        bal[r["to_user"]] = bal.get(r["to_user"], 0) - r["s"]
    return bal


def suggest_settlements(bal):
    """Minimale set betalingen om iedereen op nul te zetten (gretig)."""
    debtors = sorted([[u, -b] for u, b in bal.items() if b < 0], key=lambda x: -x[1])
    creditors = sorted([[u, b] for u, b in bal.items() if b > 0], key=lambda x: -x[1])
    out = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        if pay > 0:
            out.append({"from": debtors[i][0], "to": creditors[j][0], "amount": pay})
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return out


def expense_rows(db, where, params):
    rows = db.execute(
        f"""SELECT e.id, e.description, e.amount, e.paid_by, e.date, e.category_id,
                   e.created_by, e.created_at, e.recurring_id, e.recurring_month
            FROM expenses e WHERE {where} ORDER BY e.date DESC, e.id DESC""",
        params,
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["shares"] = {
            s["user_id"]: s["amount"]
            for s in db.execute(
                "SELECT user_id, amount FROM expense_shares WHERE expense_id = ?", (r["id"],))
        }
        result.append(item)
    return result


# ---------------------------------------------------------------- vaste kosten

GEN_LOCK = threading.Lock()


def month_add(month, n):
    y, m = map(int, month.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def due_date(month, day):
    """Dag in de maand, begrensd op de laatste dag (31 = altijd de laatste dag)."""
    y, m = map(int, month.split("-"))
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def recurring_next(row, today=None):
    today = today or date.today()
    month = month_add(row["last_month"], 1) if row["last_month"] else row["start_month"]
    return due_date(month, row["day"])


def generate_recurring(db, today=None):
    """Zet alle vaste kosten die inmiddels aan de beurt zijn als uitgave in de lijst."""
    today = today or date.today()
    added = 0
    with GEN_LOCK:
        rules = db.execute("SELECT * FROM recurring WHERE active = 1").fetchall()
        for r in rules:
            shares = db.execute(
                "SELECT user_id, amount FROM recurring_shares WHERE recurring_id = ?", (r["id"],)).fetchall()
            if not shares:
                continue
            month = month_add(r["last_month"], 1) if r["last_month"] else r["start_month"]
            last = r["last_month"]
            while due_date(month, r["day"]) <= today:
                cur = db.execute(
                    "INSERT OR IGNORE INTO expenses(description, amount, paid_by, date, category_id, "
                    "created_by, recurring_id, recurring_month) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["description"], r["amount"], r["paid_by"], due_date(month, r["day"]).isoformat(),
                     r["category_id"], "vaste-kosten", r["id"], month))
                if cur.rowcount:
                    db.executemany(
                        "INSERT INTO expense_shares(expense_id, user_id, amount) VALUES (?, ?, ?)",
                        [(cur.lastrowid, s["user_id"], s["amount"]) for s in shares])
                    added += 1
                last = month
                month = month_add(month, 1)
            if last != r["last_month"]:
                db.execute("UPDATE recurring SET last_month = ? WHERE id = ?", (last, r["id"]))
        db.commit()
    return added


def recurring_rows(db):
    out = []
    for r in db.execute("SELECT * FROM recurring ORDER BY active DESC, day, id").fetchall():
        item = {k: r[k] for k in ("id", "description", "amount", "paid_by", "category_id", "day", "active")}
        item["shares"] = {s["user_id"]: s["amount"] for s in db.execute(
            "SELECT user_id, amount FROM recurring_shares WHERE recurring_id = ?", (r["id"],))}
        item["next_date"] = recurring_next(r).isoformat() if r["active"] else None
        out.append(item)
    return out


# ---------------------------------------------------------------- taken

TASK_COLS = ("id, title, assigned_to, due_date, repeat_unit, repeat_every, done, done_at, done_by, "
             "created_by, created_at")
REPEAT_UNITS = ("day", "week", "month")


def add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def task_step(base, unit, every, n):
    if unit == "day":
        return base + timedelta(days=every * n)
    if unit == "week":
        return base + timedelta(weeks=every * n)
    return add_months(base, every * n)


def task_next_due(due, unit, every, today):
    """Eerstvolgende datum ná vandaag. Een taak die je te vroeg afvinkt slaat die beurt over."""
    base = date.fromisoformat(due) if due else today
    if (today - base).days > 3650:
        base = today
    n = 1
    nxt = task_step(base, unit, every, n)
    while nxt <= today:
        n += 1
        nxt = task_step(base, unit, every, n)
    return nxt


def sql_now(db):
    return db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]


def task_row(db, tid):
    r = db.execute(f"SELECT {TASK_COLS} FROM tasks WHERE id = ?", (tid,)).fetchone()
    return dict(r) if r else None


def task_lists(db):
    open_ = [dict(r) for r in db.execute(
        f"SELECT {TASK_COLS} FROM tasks WHERE done = 0 ORDER BY due_date IS NULL, due_date, id")]
    done = [dict(r) for r in db.execute(
        f"SELECT {TASK_COLS} FROM tasks WHERE done = 1 ORDER BY done_at DESC, id DESC LIMIT 30")]
    return {"open": open_, "done": done}


def background_generator():
    while True:
        time.sleep(60)
        try:
            db = connect()
            generate_recurring(db)
            maybe_send_monthly_report(db)
            maybe_send_task_reminders(db)
            # Afgevinkte taken ruimen zichzelf na 90 dagen op.
            db.execute("DELETE FROM tasks WHERE done = 1 AND done_at < datetime('now', '-90 days')")
            db.commit()
            db.close()
        except Exception as e:  # noqa: BLE001
            print("Achtergrondtaak:", repr(e), file=sys.stderr)


# ---------------------------------------------------------------- Home Assistant-meldingen

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
MONTHS_NL = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
             "september", "oktober", "november", "december"]
_panel_url = None


def ha_call(method, path, body=None, base="http://supervisor/core/api"):
    if not SUPERVISOR_TOKEN:
        raise ApiError(503, "Meldingen werken alleen als de add-on in Home Assistant draait")
    req = urllib.request.Request(
        base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise ApiError(502, f"Home Assistant gaf een fout ({e.code})")
    except OSError:
        raise ApiError(502, "Home Assistant is niet bereikbaar")


def panel_url():
    """Pad naar de app in de HA-zijbalk, zodat een tik op de melding hem opent."""
    global _panel_url
    if _panel_url is None:
        try:
            info = ha_call("GET", "/addons/self/info", base="http://supervisor")
            _panel_url = "/" + info["data"]["slug"]
        except Exception:  # noqa: BLE001
            _panel_url = ""
    return _panel_url


def notify_services():
    services = []
    for domain in ha_call("GET", "/services") or []:
        if domain.get("domain") == "notify":
            services += [name for name in domain.get("services", {}) if name not in ("notify", "persistent_notification")]
    return sorted(services)


def send_notification(service, title, message):
    data = {"title": title, "message": message}
    url = panel_url()
    if url:
        data["data"] = {"url": url, "clickAction": url}
    ha_call("POST", f"/services/notify/{service}", data)


def euro(cents):
    s = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {s}"


def report_text(db, uid, month):
    names = {u["id"]: u["name"] for u in list_users(db)}
    s = compute_summary(db, month)
    y, m = month.split("-")
    title = f"Kostendeler · {MONTHS_NL[int(m) - 1]} {y}"
    lines = []
    mine = s["per_user"].get(uid, {}).get("share", 0)
    if s["total"]:
        lines.append(f"Uitgegeven: {euro(s['total'])}, jouw deel {euro(mine)}.")
        top = s["per_category"][0]
        cat = db.execute("SELECT name FROM categories WHERE id = ?", (top["category_id"],)).fetchone()
        lines.append(f"Meeste geld naar {cat[0] if cat else 'overig'}: {euro(top['total'])}.")
    else:
        lines.append("Geen uitgaven vorige maand.")
    owed = [x for x in suggest_settlements(balances(db)) if uid in (x["from"], x["to"])]
    if not owed:
        lines.append("Jullie staan quitte.")
    for x in owed:
        if x["to"] == uid:
            lines.append(f"{names.get(x['from'], 'Je huisgenoot')} betaalt jou nog {euro(x['amount'])}.")
        else:
            lines.append(f"Je betaalt {names.get(x['to'], 'je huisgenoot')} nog {euro(x['amount'])}.")
    return title, " ".join(lines)


def maybe_send_monthly_report(db, now=None):
    now = now or datetime.now()
    prev = month_add(now.strftime("%Y-%m"), -1)
    if now.hour < 9 or kv_get(db, "last_report") == prev:
        return 0
    sent = 0
    for u in list_users(db):
        if u["notify_service"] and u["monthly_report"]:
            title, msg = report_text(db, u["id"], prev)
            try:
                send_notification(u["notify_service"], title, msg)
                sent += 1
            except ApiError as e:
                print(f"Maandrapport naar {u['name']} mislukt: {e.message}", file=sys.stderr)
    kv_set(db, "last_report", prev)
    db.commit()
    return sent


WEEKDAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def due_phrase(iso, today=None):
    today = today or date.today()
    d = date.fromisoformat(iso)
    diff = (d - today).days
    if diff == 0:
        return "vandaag"
    if diff == 1:
        return "morgen"
    if diff < 0:
        return "al te laat"
    return f"{WEEKDAYS_NL[d.weekday()]} {d.day} {MONTHS_NL[d.month - 1]}"


def due_tasks_for(db, uid, today):
    """Open taken voor deze persoon (of voor iedereen) die vandaag of eerder aan de beurt zijn."""
    return [dict(r) for r in db.execute(
        "SELECT title, due_date FROM tasks WHERE done = 0 AND due_date IS NOT NULL AND due_date <= ? "
        "AND (assigned_to IS NULL OR assigned_to = ?) ORDER BY due_date, id", (today.isoformat(), uid))]


def task_reminder_text(tasks, today):
    if not tasks:
        return "Taken", "Niets op de lijst voor vandaag."
    late = sum(1 for t in tasks if t["due_date"] < today.isoformat())
    title = "1 taak voor vandaag" if len(tasks) == 1 else f"{len(tasks)} taken voor vandaag"
    if len(tasks) == 1:
        return title, tasks[0]["title"] + (" (te laat)" if late else "")
    names = [t["title"] for t in tasks[:3]]
    rest = len(tasks) - len(names)
    listing = ", ".join(names) + f" en nog {rest}" if rest else ", ".join(names[:-1]) + " en " + names[-1]
    msg = listing + "."
    if late:
        msg += " Daarvan " + ("is er 1" if late == 1 else f"zijn er {late}") + " te laat."
    return title, msg


def maybe_send_task_reminders(db, now=None):
    """Eén keer per dag, vanaf het gekozen uur, de taken van vandaag. Geen taken = geen melding."""
    now = now or datetime.now()
    today = now.date()
    sent = 0
    for u in list_users(db):
        if not (u["notify_service"] and u["task_reminder"]) or now.hour < u["task_reminder_hour"]:
            continue
        key = f"task_reminder:{u['id']}"
        if kv_get(db, key) == today.isoformat():
            continue
        kv_set(db, key, today.isoformat())
        db.commit()
        tasks = due_tasks_for(db, u["id"], today)
        if not tasks:
            continue
        title, msg = task_reminder_text(tasks, today)
        try:
            send_notification(u["notify_service"], title, msg)
            sent += 1
        except ApiError as e:
            print(f"Takenherinnering naar {u['name']} mislukt: {e.message}", file=sys.stderr)
    return sent


def _send_quietly(service, title, message):
    try:
        send_notification(service, title, message)
    except Exception as e:  # noqa: BLE001
        print("Melding mislukt:", getattr(e, "message", repr(e)), file=sys.stderr)


def notify_assigned(db, task, me, previous=None):
    """Melding als iemand anders een taak aan jou toewijst (niet bij jezelf, niet bij 'iedereen')."""
    target = task["assigned_to"]
    if not target or target == me or target == previous:
        return False
    row = db.execute("SELECT notify_service, task_assigned FROM users WHERE id = ?", (target,)).fetchone()
    if not row or not row["notify_service"] or not row["task_assigned"]:
        return False
    who = db.execute("SELECT name FROM users WHERE id = ?", (me,)).fetchone()
    msg = f"{who[0] if who else 'Je huisgenoot'}: {task['title']}"
    if task["due_date"]:
        msg += f" · {due_phrase(task['due_date'])}"
    # Op de achtergrond, zodat opslaan niet wacht op Home Assistant.
    threading.Thread(target=_send_quietly, args=(row["notify_service"], "Nieuwe taak voor jou", msg),
                     daemon=True).start()
    return True


def sync_reminder_marker(db, uid):
    """Na het aanzetten of verzetten: niet meteen sturen als het uur vandaag al voorbij is."""
    row = db.execute("SELECT task_reminder_hour FROM users WHERE id = ?", (uid,)).fetchone()
    now = datetime.now()
    if row and now.hour >= row[0]:
        kv_set(db, f"task_reminder:{uid}", now.date().isoformat())


# ---------------------------------------------------------------- validatie

def clean_icon(value, default):
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"[a-z]{1,20}", value) else default


def require_month(qs):
    month = (qs.get("month") or [date.today().strftime("%Y-%m")])[0]
    if not MONTH_RE.match(month):
        raise ApiError(400, "Ongeldige maand")
    return month


def parse_amount(value, field="Bedrag", allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, f"{field} moet een geheel aantal centen zijn")
    if value < 0 or (value == 0 and not allow_zero) or value > MAX_AMOUNT:
        raise ApiError(400, f"{field} is ongeldig")
    return value


def parse_date(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError(400, "Ongeldige datum")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "Ongeldige datum")
    return value


def validate_expense(db, body, with_date=True):
    description = str(body.get("description") or "").strip()
    if not description:
        raise ApiError(400, "Vul een omschrijving in")
    if len(description) > 200:
        raise ApiError(400, "Omschrijving is te lang")
    amount = parse_amount(body.get("amount"))
    users = set(user_ids(db))
    paid_by = body.get("paid_by")
    if paid_by not in users:
        raise ApiError(400, "Onbekende betaler")
    category_id = body.get("category_id")
    if category_id is not None:
        if not db.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
            raise ApiError(400, "Onbekende categorie")
    shares = body.get("shares")
    if not isinstance(shares, dict) or not shares:
        raise ApiError(400, "Kies voor wie de uitgave is")
    clean = {}
    for uid, cents in shares.items():
        if uid not in users:
            raise ApiError(400, "Onbekende persoon in de verdeling")
        cents = parse_amount(cents, "Aandeel", allow_zero=True)
        if cents > 0:
            clean[uid] = cents
    if not clean:
        raise ApiError(400, "Kies voor wie de uitgave is")
    if sum(clean.values()) != amount:
        raise ApiError(400, "De verdeling telt niet op tot het totaalbedrag")
    return {
        "description": description,
        "amount": amount,
        "paid_by": paid_by,
        "date": parse_date(body.get("date")) if with_date else None,
        "category_id": category_id,
        "shares": clean,
    }


def validate_task(db, body):
    title = str(body.get("title") or "").strip()
    if not title:
        raise ApiError(400, "Vul een taak in")
    if len(title) > 200:
        raise ApiError(400, "De taak is te lang")
    assigned = body.get("assigned_to") or None
    if assigned is not None and not (isinstance(assigned, str) and db.execute(
            "SELECT 1 FROM users WHERE id = ?", (assigned,)).fetchone()):
        raise ApiError(400, "Onbekende persoon")
    due = body.get("due_date") or None
    if due is not None:
        due = parse_date(due)
    unit = body.get("repeat_unit") or None
    every = body.get("repeat_every", 1)
    if unit is not None:
        if unit not in REPEAT_UNITS or isinstance(every, bool) or not isinstance(every, int) or not 1 <= every <= 52:
            raise ApiError(400, "Ongeldige herhaling")
        if due is None:
            due = date.today().isoformat()
    else:
        every = 1
    return {"title": title, "assigned_to": assigned, "due_date": due, "repeat_unit": unit, "repeat_every": every}


def clean_done(db, body):
    """done_at / done_by uit een 'ongedaan maken'-verzoek."""
    at = body.get("done_at")
    if at is not None and (not isinstance(at, str) or len(at) > 32):
        raise ApiError(400, "Ongeldige tijd")
    by = body.get("done_by")
    if not (isinstance(by, str) and db.execute("SELECT 1 FROM users WHERE id = ?", (by,)).fetchone()):
        by = None
    return at, by


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Kostendeler/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers
    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ApiError(413, "Verzoek te groot")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Ongeldige JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "Ongeldige JSON")
        return body

    def allowed(self):
        return "*" in ALLOWED_IPS or self.client_address[0] in ALLOWED_IPS

    def ingress_user(self):
        uid = self.headers.get("X-Remote-User-Id")
        if not uid:
            return None
        name = (self.headers.get("X-Remote-User-Display-Name")
                or self.headers.get("X-Remote-User-Name") or "Gebruiker").strip()
        return uid, name

    def current_user(self, db):
        ing = self.ingress_user()
        if ing:
            uid, name = ing
            db.execute(
                "INSERT INTO users(id, name) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                (uid, name),
            )
            db.commit()
            return uid
        # Terugvalmodus zonder Ingress: de app kiest zelf een gebruiker.
        uid = self.headers.get("X-Kostendeler-User")
        if uid and db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            return uid
        return None

    # -- dispatch
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        if not self.allowed():
            self.send_json(403, {"error": "Alleen bereikbaar via Home Assistant"})
            return
        url = urlparse(self.path)
        path = url.path
        if not path.startswith("/api/"):
            if method == "GET":
                self.serve_static(path)
            else:
                self.send_json(405, {"error": "Niet toegestaan"})
            return
        db = connect()
        try:
            generate_recurring(db)
            me = self.current_user(db)
            qs = parse_qs(url.query)
            status, payload = self.route(db, method, path[len("/api"):], qs, me)
            self.send_json(status, payload)
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            print("Fout:", repr(e), file=sys.stderr)
            self.send_json(500, {"error": "Er ging iets mis op de server"})
        finally:
            db.close()

    def route(self, db, method, path, qs, me):
        if path == "/me" and method == "GET":
            return 200, {
                "me": me,
                "ingress": self.ingress_user() is not None,
                "users": list_users(db),
                "categories": list_categories(db),
            }

        if path == "/users" and method == "POST":
            if self.ingress_user():
                raise ApiError(400, "Gebruikers komen uit Home Assistant")
            name = str(self.read_json().get("name") or "").strip()[:60]
            if not name:
                raise ApiError(400, "Vul een naam in")
            uid = "local-" + os.urandom(6).hex()
            db.execute("INSERT INTO users(id, name) VALUES (?, ?)", (uid, name))
            db.commit()
            return 201, {"id": uid, "name": name}

        # Alles hieronder vereist een bekende gebruiker.
        if not me:
            raise ApiError(401, "Onbekende gebruiker")

        if path == "/balances" and method == "GET":
            bal = balances(db)
            return 200, {"balances": bal, "suggestions": suggest_settlements(bal)}

        if path == "/activity" and method == "GET":
            month = require_month(qs)
            expenses = expense_rows(db, "substr(e.date, 1, 7) = ?", (month,))
            settlements = [dict(r) for r in db.execute(
                "SELECT id, from_user, to_user, amount, date, created_by, created_at FROM settlements "
                "WHERE substr(date, 1, 7) = ? ORDER BY date DESC, id DESC", (month,))]
            return 200, {"month": month, "expenses": expenses, "settlements": settlements}

        if path == "/summary" and method == "GET":
            year = (qs.get("year") or [None])[0]
            return 200, compute_summary(db, year) if year else self.summary(db, require_month(qs))

        if path == "/expenses" and method == "POST":
            data = validate_expense(db, self.read_json())
            cur = db.execute(
                "INSERT INTO expenses(description, amount, paid_by, date, category_id, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (data["description"], data["amount"], data["paid_by"], data["date"],
                 data["category_id"], me),
            )
            eid = cur.lastrowid
            db.executemany(
                "INSERT INTO expense_shares(expense_id, user_id, amount) VALUES (?, ?, ?)",
                [(eid, uid, c) for uid, c in data["shares"].items()],
            )
            db.commit()
            return 201, expense_rows(db, "e.id = ?", (eid,))[0]

        m = re.fullmatch(r"/expenses/(\d+)", path)
        if m:
            eid = int(m.group(1))
            if not db.execute("SELECT 1 FROM expenses WHERE id = ?", (eid,)).fetchone():
                raise ApiError(404, "Uitgave niet gevonden")
            if method == "PUT":
                data = validate_expense(db, self.read_json())
                db.execute(
                    "UPDATE expenses SET description=?, amount=?, paid_by=?, date=?, category_id=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (data["description"], data["amount"], data["paid_by"], data["date"],
                     data["category_id"], eid),
                )
                db.execute("DELETE FROM expense_shares WHERE expense_id = ?", (eid,))
                db.executemany(
                    "INSERT INTO expense_shares(expense_id, user_id, amount) VALUES (?, ?, ?)",
                    [(eid, uid, c) for uid, c in data["shares"].items()],
                )
                db.commit()
                return 200, expense_rows(db, "e.id = ?", (eid,))[0]
            if method == "DELETE":
                deleted = expense_rows(db, "e.id = ?", (eid,))[0]
                db.execute("DELETE FROM expenses WHERE id = ?", (eid,))
                db.commit()
                return 200, {"ok": True, "deleted": deleted}

        if path == "/expenses/restore" and method == "POST":
            body = self.read_json()
            data = validate_expense(db, body)
            eid = body.get("id")
            if not isinstance(eid, int) or db.execute("SELECT 1 FROM expenses WHERE id = ?", (eid,)).fetchone():
                eid = None
            rid = body.get("recurring_id")
            if rid is not None and not db.execute("SELECT 1 FROM recurring WHERE id = ?", (rid,)).fetchone():
                rid = None
            cur = db.execute(
                "INSERT INTO expenses(id, description, amount, paid_by, date, category_id, created_by, created_at, "
                "recurring_id, recurring_month) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?)",
                (eid, data["description"], data["amount"], data["paid_by"], data["date"], data["category_id"],
                 body.get("created_by") or me, body.get("created_at"), rid,
                 body.get("recurring_month") if rid else None))
            new_id = cur.lastrowid
            db.executemany("INSERT INTO expense_shares(expense_id, user_id, amount) VALUES (?, ?, ?)",
                           [(new_id, u, c) for u, c in data["shares"].items()])
            db.commit()
            return 201, {"id": new_id}

        if path == "/settlements" and method == "POST":
            body = self.read_json()
            users = set(user_ids(db))
            frm, to = body.get("from_user"), body.get("to_user")
            if frm not in users or to not in users or frm == to:
                raise ApiError(400, "Ongeldige personen")
            amount = parse_amount(body.get("amount"))
            day = parse_date(body.get("date") or date.today().isoformat())
            cur = db.execute(
                "INSERT INTO settlements(from_user, to_user, amount, date, created_by) "
                "VALUES (?, ?, ?, ?, ?)", (frm, to, amount, day, me))
            db.commit()
            return 201, {"id": cur.lastrowid}

        m = re.fullmatch(r"/settlements/(\d+)", path)
        if m and method == "DELETE":
            row = db.execute("SELECT * FROM settlements WHERE id = ?", (int(m.group(1)),)).fetchone()
            db.execute("DELETE FROM settlements WHERE id = ?", (int(m.group(1)),))
            db.commit()
            if row:
                return 200, {"ok": True, "deleted": dict(row)}
            return 200, {"ok": True}

        if path == "/settlements/restore" and method == "POST":
            body = self.read_json()
            users = set(user_ids(db))
            frm, to = body.get("from_user"), body.get("to_user")
            if frm not in users or to not in users or frm == to:
                raise ApiError(400, "Ongeldige personen")
            sid = body.get("id")
            if not isinstance(sid, int) or db.execute("SELECT 1 FROM settlements WHERE id = ?", (sid,)).fetchone():
                sid = None
            cur = db.execute(
                "INSERT INTO settlements(id, from_user, to_user, amount, date, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                (sid, frm, to, parse_amount(body.get("amount")), parse_date(body.get("date")),
                 body.get("created_by") or me, body.get("created_at")))
            db.commit()
            return 201, {"id": cur.lastrowid}

        if path == "/me" and method == "PUT":
            body = self.read_json()
            if "notify_service" in body:
                svc = body["notify_service"] or None
                if svc is not None and not re.fullmatch(r"[a-z0-9_]{1,80}", str(svc)):
                    raise ApiError(400, "Ongeldige meldingsdienst")
                db.execute("UPDATE users SET notify_service = ? WHERE id = ?", (svc, me))
            if "monthly_report" in body:
                db.execute("UPDATE users SET monthly_report = ? WHERE id = ?", (1 if body["monthly_report"] else 0, me))
            if "task_reminder" in body:
                db.execute("UPDATE users SET task_reminder = ? WHERE id = ?", (1 if body["task_reminder"] else 0, me))
            if "task_reminder_hour" in body:
                hour = body["task_reminder_hour"]
                if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
                    raise ApiError(400, "Kies een uur tussen 0 en 23")
                db.execute("UPDATE users SET task_reminder_hour = ? WHERE id = ?", (hour, me))
            if "task_assigned" in body:
                db.execute("UPDATE users SET task_assigned = ? WHERE id = ?", (1 if body["task_assigned"] else 0, me))
            if {"notify_service", "task_reminder", "task_reminder_hour"} & set(body):
                sync_reminder_marker(db, me)
            db.commit()
            return 200, {"ok": True}

        if path == "/notify/services" and method == "GET":
            if not SUPERVISOR_TOKEN:
                return 200, {"available": False, "services": []}
            return 200, {"available": True, "services": notify_services()}

        if path == "/notify/test" and method == "POST":
            body = self.read_json()
            row = db.execute("SELECT notify_service FROM users WHERE id = ?", (me,)).fetchone()
            if not row or not row["notify_service"]:
                raise ApiError(400, "Kies eerst waar meldingen heen moeten")
            if body.get("kind") == "tasks":
                title, msg = task_reminder_text(due_tasks_for(db, me, date.today()), date.today())
            else:
                title, msg = report_text(db, me, month_add(date.today().strftime("%Y-%m"), -1))
            send_notification(row["notify_service"], "Testmelding · " + title, msg)
            return 200, {"ok": True}

        if path == "/recurring" and method == "GET":
            return 200, {"recurring": recurring_rows(db)}

        if path == "/recurring" and method == "POST":
            body = self.read_json()
            data = validate_expense(db, body, with_date=False)
            day = self.parse_day(body.get("day"))
            today = date.today()
            this_month = today.strftime("%Y-%m")
            if day >= today.day or due_date(this_month, day) >= today or body.get("include_current"):
                start = this_month
            else:
                start = month_add(this_month, 1)
            cur = db.execute(
                "INSERT INTO recurring(description, amount, paid_by, category_id, day, start_month, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (data["description"], data["amount"], data["paid_by"], data["category_id"], day, start, me))
            rid = cur.lastrowid
            db.executemany("INSERT INTO recurring_shares(recurring_id, user_id, amount) VALUES (?, ?, ?)",
                           [(rid, u, c) for u, c in data["shares"].items()])
            db.commit()
            generate_recurring(db)
            return 201, {"id": rid}

        m = re.fullmatch(r"/recurring/(\d+)", path)
        if m:
            rid = int(m.group(1))
            row = db.execute("SELECT * FROM recurring WHERE id = ?", (rid,)).fetchone()
            if not row:
                raise ApiError(404, "Vaste uitgave niet gevonden")
            if method == "PUT":
                body = self.read_json()
                if set(body) == {"active"}:
                    active = 1 if body["active"] else 0
                    if active and not row["active"]:
                        # Hervatten: niet alsnog de gemiste maanden toevoegen.
                        today = date.today()
                        month = today.strftime("%Y-%m")
                        last = month if due_date(month, row["day"]) < today else month_add(month, -1)
                        db.execute("UPDATE recurring SET active = 1, last_month = ? WHERE id = ?", (last, rid))
                    else:
                        db.execute("UPDATE recurring SET active = ? WHERE id = ?", (active, rid))
                    db.commit()
                    return 200, {"ok": True}
                data = validate_expense(db, body, with_date=False)
                day = self.parse_day(body.get("day"))
                db.execute("UPDATE recurring SET description=?, amount=?, paid_by=?, category_id=?, day=? WHERE id=?",
                           (data["description"], data["amount"], data["paid_by"], data["category_id"], day, rid))
                db.execute("DELETE FROM recurring_shares WHERE recurring_id = ?", (rid,))
                db.executemany("INSERT INTO recurring_shares(recurring_id, user_id, amount) VALUES (?, ?, ?)",
                               [(rid, u, c) for u, c in data["shares"].items()])
                db.commit()
                return 200, {"ok": True}
            if method == "DELETE":
                db.execute("DELETE FROM recurring WHERE id = ?", (rid,))
                db.commit()
                return 200, {"ok": True}

        if path == "/tasks" and method == "GET":
            return 200, task_lists(db)

        if path == "/tasks" and method == "POST":
            data = validate_task(db, self.read_json())
            cur = db.execute(
                "INSERT INTO tasks(title, assigned_to, due_date, repeat_unit, repeat_every, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (data["title"], data["assigned_to"], data["due_date"], data["repeat_unit"], data["repeat_every"], me))
            db.commit()
            task = task_row(db, cur.lastrowid)
            notify_assigned(db, task, me)
            return 201, task

        if path == "/tasks/restore" and method == "POST":
            body = self.read_json()
            data = validate_task(db, body)
            tid = body.get("id")
            if not isinstance(tid, int) or isinstance(tid, bool) or task_row(db, tid):
                tid = None
            done = 1 if body.get("done") else 0
            done_at, done_by = clean_done(db, body)
            created_at = body.get("created_at")
            if not isinstance(created_at, str) or len(created_at) > 32:
                created_at = None
            created_by = body.get("created_by") if isinstance(body.get("created_by"), str) else me
            cur = db.execute(
                "INSERT INTO tasks(id, title, assigned_to, due_date, repeat_unit, repeat_every, done, done_at, "
                "done_by, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                (tid, data["title"], data["assigned_to"], data["due_date"], data["repeat_unit"], data["repeat_every"],
                 done, done_at, done_by, created_by, created_at))
            db.commit()
            return 201, {"id": cur.lastrowid}

        m = re.fullmatch(r"/tasks/(\d+)/(complete|reopen)", path)
        if m and method == "POST":
            tid = int(m.group(1))
            row = task_row(db, tid)
            if not row:
                raise ApiError(404, "Taak niet gevonden")
            if m.group(2) == "reopen":
                db.execute("UPDATE tasks SET done = 0, done_at = NULL, done_by = NULL WHERE id = ?", (tid,))
            elif not row["done"]:
                body = self.read_json()
                today = date.fromisoformat(parse_date(body["today"])) if body.get("today") else date.today()
                if row["repeat_unit"]:
                    # Terugkerende taak: blijft open, met de eerstvolgende datum.
                    nxt = task_next_due(row["due_date"], row["repeat_unit"], row["repeat_every"], today)
                    db.execute("UPDATE tasks SET due_date = ?, done_at = ?, done_by = ? WHERE id = ?",
                               (nxt.isoformat(), sql_now(db), me, tid))
                else:
                    db.execute("UPDATE tasks SET done = 1, done_at = ?, done_by = ? WHERE id = ?",
                               (sql_now(db), me, tid))
            db.commit()
            return 200, {"task": task_row(db, tid), "previous": row}

        m = re.fullmatch(r"/tasks/(\d+)", path)
        if m:
            tid = int(m.group(1))
            row = task_row(db, tid)
            if not row:
                raise ApiError(404, "Taak niet gevonden")
            if method == "PUT":
                body = self.read_json()
                data = validate_task(db, body)
                done, done_at, done_by = row["done"], row["done_at"], row["done_by"]
                if "done" in body:
                    done = 1 if body["done"] else 0
                    if "done_at" in body or "done_by" in body:
                        done_at, done_by = clean_done(db, body)
                    elif done != row["done"]:
                        done_at, done_by = (sql_now(db), me) if done else (None, None)
                db.execute(
                    "UPDATE tasks SET title=?, assigned_to=?, due_date=?, repeat_unit=?, repeat_every=?, "
                    "done=?, done_at=?, done_by=? WHERE id=?",
                    (data["title"], data["assigned_to"], data["due_date"], data["repeat_unit"], data["repeat_every"],
                     done, done_at, done_by, tid))
                db.commit()
                task = task_row(db, tid)
                if not task["done"]:
                    notify_assigned(db, task, me, previous=row["assigned_to"])
                return 200, task
            if method == "DELETE":
                db.execute("DELETE FROM tasks WHERE id = ?", (tid,))
                db.commit()
                return 200, {"ok": True, "deleted": row}

        if path == "/categories" and method == "POST":
            body = self.read_json()
            name = str(body.get("name") or "").strip()[:40]
            icon = clean_icon(body.get("icon"), "box")
            if not name:
                raise ApiError(400, "Vul een naam in")
            cur = db.execute("INSERT INTO categories(name, icon) VALUES (?, ?)", (name, icon))
            db.commit()
            return 201, {"id": cur.lastrowid, "name": name, "icon": icon, "archived": 0}

        m = re.fullmatch(r"/categories/(\d+)", path)
        if m and method == "PUT":
            body = self.read_json()
            cid = int(m.group(1))
            row = db.execute("SELECT * FROM categories WHERE id = ?", (cid,)).fetchone()
            if not row:
                raise ApiError(404, "Categorie niet gevonden")
            name = str(body.get("name", row["name"])).strip()[:40] or row["name"]
            icon = clean_icon(body.get("icon"), row["icon"])
            archived = 1 if body.get("archived", row["archived"]) else 0
            db.execute("UPDATE categories SET name=?, icon=?, archived=? WHERE id=?",
                       (name, icon, archived, cid))
            db.commit()
            return 200, {"id": cid, "name": name, "icon": icon, "archived": archived}

        raise ApiError(404, "Niet gevonden")

    @staticmethod
    def parse_day(value):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31:
            raise ApiError(400, "Kies een dag tussen 1 en 31")
        return value

    def summary(self, db, month):
        return compute_summary(db, month)


    def serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def compute_summary(db, period):
    """Overzicht voor een maand ('2026-11') of een heel jaar ('2026')."""
    if not re.fullmatch(r"\d{4}(-\d{2})?", period):
        raise ApiError(400, "Ongeldige periode")
    n = len(period)
    month = period
    total = db.execute(
        f"SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE substr(date,1,{n}) = ?",
        (month,)).fetchone()[0]
    per_category = [dict(r) for r in db.execute(
        f"""SELECT e.category_id, SUM(e.amount) AS total, COUNT(*) AS count
           FROM expenses e WHERE substr(e.date,1,{n}) = ?
           GROUP BY e.category_id ORDER BY total DESC""", (month,))]
    cat_shares = {}
    for r in db.execute(
            f"""SELECT e.category_id, s.user_id, SUM(s.amount) AS share
               FROM expenses e JOIN expense_shares s ON s.expense_id = e.id
               WHERE substr(e.date,1,{n}) = ? GROUP BY e.category_id, s.user_id""", (month,)):
        cat_shares.setdefault(r["category_id"], {})[r["user_id"]] = r["share"]
    for c in per_category:
        c["shares"] = cat_shares.get(c["category_id"], {})
    per_user = {uid: {"paid": 0, "share": 0} for uid in user_ids(db)}
    for r in db.execute(
            f"SELECT paid_by, SUM(amount) s FROM expenses WHERE substr(date,1,{n})=? GROUP BY paid_by",
            (month,)):
        per_user.setdefault(r["paid_by"], {"paid": 0, "share": 0})["paid"] = r["s"]
    for r in db.execute(
            f"""SELECT s.user_id, SUM(s.amount) sh FROM expenses e
               JOIN expense_shares s ON s.expense_id = e.id
               WHERE substr(e.date,1,{n})=? GROUP BY s.user_id""", (month,)):
        per_user.setdefault(r["user_id"], {"paid": 0, "share": 0})["share"] = r["sh"]
    if n == 7:
        history = [dict(r) for r in db.execute(
            """SELECT substr(date,1,7) AS month, SUM(amount) AS total FROM expenses
               WHERE substr(date,1,7) <= ? GROUP BY month ORDER BY month DESC LIMIT 12""",
            (month,))]
        history.reverse()
    else:
        totals = {r["month"]: r["total"] for r in db.execute(
            """SELECT substr(date,1,7) AS month, SUM(amount) AS total FROM expenses
               WHERE substr(date,1,4) = ? GROUP BY month""", (period,))}
        history = [{"month": f"{period}-{m:02d}", "total": totals.get(f"{period}-{m:02d}", 0)} for m in range(1, 13)]
    return {"month" if n == 7 else "year": period, "total": total, "per_category": per_category,
            "per_user": per_user, "history": history}


def main():
    init_db()
    threading.Thread(target=background_generator, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Kostendeler draait op poort {PORT}, database: {DB_PATH}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
