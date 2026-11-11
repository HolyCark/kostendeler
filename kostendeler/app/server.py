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
import hmac
import html
import ipaddress
import secrets
import socket
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

SCHEMA_VERSION = 9
DEFAULT_STAPLES = ["Wc-papier", "Afwasmiddel", "Keukenrol", "Melk", "Brood", "Koffie"]
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
    if version < 7:
        db.executescript(
            """
            ALTER TABLE tasks ADD COLUMN points INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE tasks ADD COLUMN rotation TEXT;
            ALTER TABLE tasks ADD COLUMN turn_user TEXT;
            ALTER TABLE tasks ADD COLUMN next_override TEXT;
            CREATE TABLE IF NOT EXISTS task_log(
                id INTEGER PRIMARY KEY,
                task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                user_id TEXT,
                points INTEGER NOT NULL DEFAULT 1,
                done_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_log_done ON task_log(done_at);
            CREATE TABLE IF NOT EXISTS groceries(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                added_by TEXT,
                checked_by TEXT,
                checked_at TEXT,
                expense_id INTEGER REFERENCES expenses(id) ON DELETE SET NULL,
                bought_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_groceries_status ON groceries(status);
            CREATE TABLE IF NOT EXISTS staples(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            );
            -- Wat al afgevinkt was telt mee in het takensaldo.
            INSERT INTO task_log(task_id, title, user_id, points, done_at)
                SELECT id, title, done_by, 1, done_at FROM tasks WHERE done_at IS NOT NULL;
            """
        )
        if db.execute("SELECT COUNT(*) FROM staples").fetchone()[0] == 0:
            db.executemany("INSERT INTO staples(name, position) VALUES (?, ?)",
                           [(n, i) for i, n in enumerate(DEFAULT_STAPLES)])
    if version < 8:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS recipes(
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                servings INTEGER NOT NULL DEFAULT 4,
                ingredients TEXT NOT NULL DEFAULT '[]',
                steps TEXT,
                source_url TEXT,
                created_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                used_count INTEGER NOT NULL DEFAULT 0,
                last_used TEXT
            );
            """
        )
    if version < 9:
        db.executescript(
            """
            ALTER TABLE users ADD COLUMN iban TEXT;
            ALTER TABLE users ADD COLUMN iban_name TEXT;
            CREATE TABLE IF NOT EXISTS meal_plan(
                day TEXT PRIMARY KEY,
                recipe_id INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
                title TEXT,
                servings INTEGER NOT NULL DEFAULT 2,
                listed INTEGER NOT NULL DEFAULT 0
            );
            """
        )
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()
    db.close()


def user_ids(db):
    return [r["id"] for r in db.execute("SELECT id FROM users")]


def list_users(db):
    return [dict(r) for r in db.execute(
        "SELECT id, name, notify_service, monthly_report, task_reminder, task_reminder_hour, task_assigned, "
        "iban, iban_name "
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
             "created_by, created_at, points, rotation, turn_user, next_override")
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


def task_dict(r):
    t = dict(r)
    try:
        t["rotation"] = json.loads(t["rotation"]) if t["rotation"] else None
    except ValueError:
        t["rotation"] = None
    return t


def task_row(db, tid):
    r = db.execute(f"SELECT {TASK_COLS} FROM tasks WHERE id = ?", (tid,)).fetchone()
    return task_dict(r) if r else None


def rotation_after(rotation, uid):
    if uid not in rotation:
        return rotation[0]
    return rotation[(rotation.index(uid) + 1) % len(rotation)]


def task_upcoming(t):
    """Wie er na deze beurt aan de beurt is (alleen bij 'om de beurt')."""
    if not t["rotation"]:
        return None
    return t["next_override"] or rotation_after(t["rotation"], t["turn_user"] or t["rotation"][0])


def month_points(db, month):
    per = {uid: {"points": 0, "count": 0} for uid in user_ids(db)}
    for r in db.execute(
            "SELECT user_id, SUM(points) AS p, COUNT(*) AS c FROM task_log "
            "WHERE strftime('%Y-%m', done_at, 'localtime') = ? AND user_id IS NOT NULL GROUP BY user_id", (month,)):
        per[r["user_id"]] = {"points": r["p"], "count": r["c"]}
    return per


def task_lists(db):
    open_ = [task_dict(r) for r in db.execute(
        f"SELECT {TASK_COLS} FROM tasks WHERE done = 0 ORDER BY due_date IS NULL, due_date, id")]
    for t in open_:
        t["upcoming"] = task_upcoming(t)
    done = [task_dict(r) for r in db.execute(
        f"SELECT {TASK_COLS} FROM tasks WHERE done = 1 ORDER BY done_at DESC, id DESC LIMIT 30")]
    month = date.today().strftime("%Y-%m")
    return {"open": open_, "done": done}


def complete_task(db, row, doer, today):
    """Vinkt een taak af en schrijft de punten bij. Geeft het id van de logregel terug."""
    now = sql_now(db)
    cur = db.execute("INSERT INTO task_log(task_id, title, user_id, points, done_at) VALUES (?, ?, ?, ?, ?)",
                     (row["id"], row["title"], doer, row["points"], now))
    if row["repeat_unit"]:
        # Terugkerende taak: blijft open, met de eerstvolgende datum (en bij 'om de beurt' de volgende persoon).
        nxt = task_next_due(row["due_date"], row["repeat_unit"], row["repeat_every"], today)
        assigned, turn, override = row["assigned_to"], row["turn_user"], row["next_override"]
        if row["rotation"]:
            turn = rotation_after(row["rotation"], row["turn_user"] or row["rotation"][0])
            assigned, override = override or turn, None
        db.execute("UPDATE tasks SET due_date=?, done_at=?, done_by=?, assigned_to=?, turn_user=?, next_override=? "
                   "WHERE id=?", (nxt.isoformat(), now, doer, assigned, turn, override, row["id"]))
    else:
        db.execute("UPDATE tasks SET done = 1, done_at = ?, done_by = ? WHERE id = ?", (now, doer, row["id"]))
    return cur.lastrowid


def find_open_task(db, query):
    """Voor automatiseringen: taak op id of op (exacte) naam."""
    if isinstance(query, int) or (isinstance(query, str) and query.strip().isdigit()):
        r = db.execute(f"SELECT {TASK_COLS} FROM tasks WHERE id = ? AND done = 0", (int(query),)).fetchone()
    else:
        r = db.execute(f"SELECT {TASK_COLS} FROM tasks WHERE done = 0 AND lower(title) = lower(?) "
                       "ORDER BY due_date IS NULL, due_date, id LIMIT 1", (str(query or "").strip(),)).fetchone()
    return task_dict(r) if r else None


# ---------------------------------------------------------------- boodschappen

def clean_name(value):
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    if not name:
        raise ApiError(400, "Vul iets in")
    if len(name) > 80:
        raise ApiError(400, "Dat is te lang")
    return name[:1].upper() + name[1:]


def grocery_lists(db):
    items = [dict(r) for r in db.execute(
        "SELECT id, name, status, added_by, checked_by, created_at FROM groceries "
        "WHERE status IN ('open', 'basket') ORDER BY id")]
    staples = [dict(r) for r in db.execute("SELECT id, name FROM staples ORDER BY position, id")]
    suggestions = [r[0] for r in db.execute(
        "SELECT name FROM groceries GROUP BY lower(name) ORDER BY COUNT(*) DESC, MAX(id) DESC LIMIT 60")]
    return {"items": items, "staples": staples, "suggestions": suggestions, "recipes": recipe_list(db),
            "menu": menu_list(db)}


# ---------------------------------------------------------------- weekmenu

def menu_list(db):
    """Geplande maaltijden van vorige week tot een maand vooruit."""
    start = (date.today() - timedelta(days=8)).isoformat()
    return [dict(r) for r in db.execute(
        "SELECT m.day, m.recipe_id, COALESCE(r.title, m.title) AS title, m.servings, m.listed "
        "FROM meal_plan m LEFT JOIN recipes r ON r.id = m.recipe_id WHERE m.day >= ? ORDER BY m.day", (start,))]


def menu_today(db):
    row = db.execute("SELECT COALESCE(r.title, m.title) FROM meal_plan m LEFT JOIN recipes r ON r.id = m.recipe_id "
                     "WHERE m.day = ?", (date.today().isoformat(),)).fetchone()
    return row[0] if row and row[0] else None


def valid_iban(value):
    iban = re.sub(r"\s+", "", str(value or "")).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return None
    digits = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return iban if int(digits) % 97 == 1 else None


# ---------------------------------------------------------------- recepten

RECIPE_COLS = "id, title, servings, ingredients, steps, source_url, created_by, used_count, last_used"


def recipe_dict(r):
    d = dict(r)
    try:
        d["ingredients"] = json.loads(d["ingredients"] or "[]")
    except ValueError:
        d["ingredients"] = []
    return d


def recipe_list(db):
    # Wat je vaak en kortgeleden maakte bovenaan.
    return [recipe_dict(r) for r in db.execute(
        f"SELECT {RECIPE_COLS} FROM recipes ORDER BY last_used IS NULL, last_used DESC, used_count DESC, title")]


def recipe_row(db, rid):
    r = db.execute(f"SELECT {RECIPE_COLS} FROM recipes WHERE id = ?", (rid,)).fetchone()
    return recipe_dict(r) if r else None


def validate_recipe(body):
    title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()
    if not title:
        raise ApiError(400, "Geef het recept een naam")
    if len(title) > 120:
        raise ApiError(400, "De naam is te lang")
    servings = body.get("servings", 4)
    if isinstance(servings, bool) or not isinstance(servings, int) or not 1 <= servings <= 50:
        raise ApiError(400, "Kies 1 tot 50 personen")
    raw = body.get("ingredients") or []
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        raise ApiError(400, "Ongeldige ingrediënten")
    ingredients = [re.sub(r"\s+", " ", str(x)).strip()[:120] for x in raw]
    ingredients = [x for x in ingredients if x][:80]
    if not ingredients:
        raise ApiError(400, "Vul minstens één ingrediënt in")
    steps = str(body.get("steps") or "").strip()[:8000] or None
    url = str(body.get("source_url") or "").strip()[:500] or None
    if url and not re.match(r"https?://", url):
        url = None
    return {"title": title, "servings": servings, "ingredients": json.dumps(ingredients, ensure_ascii=False),
            "steps": steps, "source_url": url}


def _text(value):
    """Tekst uit JSON-LD: HTML eruit, entiteiten omzetten, witruimte netjes."""
    value = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", value).strip()


def _find_recipe(node):
    if isinstance(node, list):
        for x in node:
            found = _find_recipe(x)
            if found:
                return found
    elif isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Recipe" in kinds:
            return node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                found = _find_recipe(node[key])
                if found:
                    return found
    return None


def _steps(node):
    if isinstance(node, str):
        return [_text(x) for x in re.split(r"\n+", node) if _text(x)]
    if isinstance(node, list):
        return [s for x in node for s in _steps(x)]
    if isinstance(node, dict):
        if "itemListElement" in node:
            head = [_text(node["name"]) + ":"] if node.get("name") else []
            return head + _steps(node["itemListElement"])
        return _steps(node.get("text") or node.get("name") or "")
    return []


def _servings(value):
    if isinstance(value, list):
        value = next((v for v in value if re.search(r"\d", str(v))), value[0] if value else "")
    m = re.search(r"\d+", str(value or ""))
    n = int(m.group()) if m else 4
    return n if 1 <= n <= 50 else 4


def parse_recipe_html(page, url):
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            try:  # sommige sites zetten er losse controletekens in
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", block.strip()))
            except ValueError:
                continue
        r = _find_recipe(data)
        if not r:
            continue
        ingredients = [_text(x) for x in (r.get("recipeIngredient") or r.get("ingredients") or [])]
        ingredients = [x for x in ingredients if x]
        if not ingredients:
            continue
        steps = _steps(r.get("recipeInstructions") or "")
        return {"title": _text(r.get("name")) or "Recept", "servings": _servings(r.get("recipeYield")),
                "ingredients": ingredients[:80], "steps": "\n".join(steps)[:8000] or None, "source_url": url}
    return None


def public_host(host):
    """Alleen gewone internetadressen ophalen, niet je eigen netwerk."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ApiError(400, "Die website is niet te vinden")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise ApiError(400, "Alleen links naar websites op internet")
    return True


def fetch_recipe(url):
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError(400, "Plak een link die begint met https://")
    public_host(parsed.hostname)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml", "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.6"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(4_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            raise ApiError(502, "Die website laat geen recepten ophalen. Vul de ingrediënten dan zelf in.")
        raise ApiError(502, f"De website gaf een fout ({e.code})")
    except Exception:  # noqa: BLE001
        raise ApiError(502, "De website reageert niet")
    recipe = parse_recipe_html(raw.decode(charset, "replace"), url)
    if not recipe:
        raise ApiError(422, "Geen recept gevonden op die pagina. Vul de ingrediënten dan zelf in.")
    return recipe


def add_grocery(db, name, uid):
    """Zet iets op de lijst. Staat het er al op, dan gebeurt er niets."""
    name = clean_name(name)
    row = db.execute("SELECT id, name, status FROM groceries WHERE status IN ('open', 'basket') "
                     "AND lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return dict(row), False
    cur = db.execute("INSERT INTO groceries(name, added_by) VALUES (?, ?)", (name, uid))
    return {"id": cur.lastrowid, "name": name, "status": "open"}, True


def background_generator():
    while True:
        try:
            db = connect()
            generate_recurring(db)
            maybe_send_monthly_report(db)
            maybe_send_task_reminders(db)
            # Afgevinkte taken en gekochte boodschappen ruimen zichzelf op.
            db.execute("DELETE FROM tasks WHERE done = 1 AND done_at < datetime('now', '-90 days')")
            db.execute("DELETE FROM groceries WHERE status = 'bought' AND bought_at < datetime('now', '-180 days')")
            db.execute("DELETE FROM meal_plan WHERE day < date('now', '-60 days')")
            db.commit()
            db.close()
            push_ha_states()
        except Exception as e:  # noqa: BLE001
            print("Achtergrondtaak:", repr(e), file=sys.stderr)
        time.sleep(60)


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


def notify_assigned(db, task, me, previous=None, extra=None):
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
    if extra:
        msg += f" ({extra})"
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


# ---------------------------------------------------------------- Home Assistant-sensoren en -koppeling

HA_NET = ipaddress.ip_network("172.30.32.0/23")  # Home Assistant, Supervisor en andere add-ons (zoals Node-RED)
PUSH_LOCK = threading.Lock()
_push = {"key": None, "at": 0.0, "err": None, "dirty": False}
_hostname = None


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "persoon"


def ha_states(db):
    today = date.today().isoformat()
    users = list_users(db)
    names = {u["id"]: u["name"] for u in users}
    bal = balances(db)
    states, used = {}, set()
    for u in users:
        sl = slugify(u["name"])
        while sl in used:
            sl += "_2"
        used.add(sl)
        states[f"sensor.kostendeler_saldo_{sl}"] = {
            "state": f"{bal.get(u['id'], 0) / 100:.2f}",
            "attributes": {"friendly_name": f"Saldo {u['name']}", "unit_of_measurement": "EUR",
                           "device_class": "monetary", "icon": "mdi:scale-balance"}}
    due = [dict(r) for r in db.execute(
        "SELECT title, due_date, assigned_to FROM tasks WHERE done = 0 AND due_date IS NOT NULL AND due_date <= ? "
        "ORDER BY due_date, id", (today,))]
    late = [t for t in due if t["due_date"] < today]
    label = lambda t: t["title"] + (f" ({names[t['assigned_to']]})" if t["assigned_to"] in names else "")  # noqa: E731
    states["sensor.kostendeler_taken_vandaag"] = {
        "state": str(len(due)),
        "attributes": {"friendly_name": "Taken voor vandaag", "icon": "mdi:checkbox-marked-circle-outline",
                       "unit_of_measurement": "taken", "te_laat": len(late),
                       "per_persoon": {u["name"]: sum(1 for t in due if t["assigned_to"] in (None, u["id"])) for u in users},
                       "taken": [label(t) for t in due][:20]}}
    states["sensor.kostendeler_taken_te_laat"] = {
        "state": str(len(late)),
        "attributes": {"friendly_name": "Taken te laat", "icon": "mdi:alert-circle-outline",
                       "unit_of_measurement": "taken", "taken": [label(t) for t in late][:20]}}
    items = [r[0] for r in db.execute("SELECT name FROM groceries WHERE status = 'open' ORDER BY id")]
    states["sensor.kostendeler_boodschappen"] = {
        "state": str(len(items)),
        "attributes": {"friendly_name": "Boodschappenlijst", "icon": "mdi:cart-outline",
                       "unit_of_measurement": "items", "items": items[:40]}}
    meal = menu_today(db)
    states["sensor.kostendeler_eten_vandaag"] = {
        "state": (meal or "Niets gepland")[:250],
        "attributes": {"friendly_name": "Eten vandaag", "icon": "mdi:silverware-fork-knife"}}
    return states


def push_ha_states(force=False):
    """Zet saldo, taken en boodschappen als sensoren in Home Assistant (alleen als er iets veranderde)."""
    if not SUPERVISOR_TOKEN:
        return
    _push["dirty"] = True
    if not PUSH_LOCK.acquire(blocking=False):
        return  # de lopende ronde pakt de wijziging mee
    try:
        while _push["dirty"]:
            _push["dirty"] = False
            db = connect()
            try:
                states = ha_states(db)
            finally:
                db.close()
            key = json.dumps(states, sort_keys=True)
            # Na een herstart van Home Assistant zijn de sensoren weg, dus elke 10 minuten opnieuw.
            if not force and key == _push["key"] and time.time() - _push["at"] < 600:
                continue
            for entity_id, st in states.items():
                ha_call("POST", "/states/" + entity_id, st)
            _push.update(key=key, at=time.time(), err=None)
    except ApiError as e:
        if e.message != _push["err"]:
            print("Sensoren bijwerken mislukt:", e.message, file=sys.stderr)
            _push["err"] = e.message
    finally:
        PUSH_LOCK.release()


def push_soon():
    threading.Thread(target=push_ha_states, daemon=True).start()


def ha_api_key(db, rotate=False):
    key = kv_get(db, "api_key")
    if rotate or not key:
        key = secrets.token_urlsafe(24)
        kv_set(db, "api_key", key)
        db.commit()
    return key


def addon_hostname():
    global _hostname
    if _hostname is None and SUPERVISOR_TOKEN:
        try:
            _hostname = ha_call("GET", "/addons/self/info", base="http://supervisor")["data"]["hostname"]
        except Exception:  # noqa: BLE001
            return None
    return _hostname


def from_ha_network(ip):
    try:
        addr = ipaddress.ip_address(ip)
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        return addr in HA_NET
    except ValueError:
        return False


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
    users = set(user_ids(db))
    assigned = body.get("assigned_to") or None
    if assigned is not None and assigned not in users:
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
    points = body.get("points", 1)
    if isinstance(points, bool) or not isinstance(points, int) or not 1 <= points <= 5:
        raise ApiError(400, "Kies 1 tot 5 punten")
    rotation = body.get("rotation") or None
    turn = override = None
    if rotation is not None:
        if (not isinstance(rotation, list) or len(rotation) < 2 or len(set(rotation)) != len(rotation)
                or any(u not in users for u in rotation)):
            raise ApiError(400, "Kies minstens twee mensen voor om de beurt")
        if unit is None:
            raise ApiError(400, "Om de beurt werkt alleen bij een terugkerende taak")
        turn = body.get("turn_user") if body.get("turn_user") in rotation else rotation[0]
        if assigned not in rotation:
            assigned = turn
        if body.get("next_override") in rotation:
            override = body["next_override"]
    return {"title": title, "assigned_to": assigned, "due_date": due, "repeat_unit": unit, "repeat_every": every,
            "points": points, "rotation": json.dumps(rotation) if rotation else None,
            "turn_user": turn, "next_override": override}


TASK_FIELDS = ("title", "assigned_to", "due_date", "repeat_unit", "repeat_every", "points", "rotation",
               "turn_user", "next_override")


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
        if urlparse(self.path).path.startswith("/api/ha/"):
            self.dispatch_ha(method, urlparse(self.path).path)
            return
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
            if method != "GET":
                push_soon()
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            print("Fout:", repr(e), file=sys.stderr)
            self.send_json(500, {"error": "Er ging iets mis op de server"})
        finally:
            db.close()

    def dispatch_ha(self, method, path):
        """Voor automatiseringen in Home Assistant of Node-RED (bijv. een NFC-sticker), met een sleutel."""
        if not ("*" in ALLOWED_IPS or from_ha_network(self.client_address[0])):
            self.send_json(403, {"error": "Alleen bereikbaar vanuit Home Assistant"})
            return
        db = connect()
        try:
            key = kv_get(db, "api_key")
            auth = self.headers.get("Authorization", "")
            if not key or not hmac.compare_digest(auth.encode(), ("Bearer " + key).encode()):
                raise ApiError(401, "Ongeldige sleutel")
            if method != "POST":
                raise ApiError(405, "Gebruik POST")
            body = self.read_json()
            uid = body.get("user") if body.get("user") in user_ids(db) else None
            if path == "/api/ha/groceries":
                item, created = add_grocery(db, body.get("name"), uid)
                db.commit()
                payload = {"ok": True, "created": created, "item": item}
            elif path == "/api/ha/tasks/complete":
                generate_recurring(db)
                row = find_open_task(db, body.get("task"))
                if not row:
                    raise ApiError(404, "Taak niet gevonden")
                complete_task(db, row, uid or row["assigned_to"], date.today())
                db.commit()
                payload = {"ok": True, "task": task_row(db, row["id"])}
            else:
                raise ApiError(404, "Niet gevonden")
            self.send_json(200, payload)
            push_soon()
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
            body = self.read_json()
            data = validate_expense(db, body)
            repeat = body.get("repeat") or None
            if repeat not in (None, "month"):
                raise ApiError(400, "Ongeldige herhaling")
            rid = month = None
            if repeat:
                # Meteen een vaste uitgave van maken: deze telt als eerste keer, daarna elke maand op dezelfde dag.
                month = data["date"][:7]
                cur = db.execute(
                    "INSERT INTO recurring(description, amount, paid_by, category_id, day, start_month, last_month, "
                    "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (data["description"], data["amount"], data["paid_by"], data["category_id"],
                     int(data["date"][8:10]), month, month, me))
                rid = cur.lastrowid
                db.executemany("INSERT INTO recurring_shares(recurring_id, user_id, amount) VALUES (?, ?, ?)",
                               [(rid, u, c) for u, c in data["shares"].items()])
            cur = db.execute(
                "INSERT INTO expenses(description, amount, paid_by, date, category_id, created_by, recurring_id, "
                "recurring_month) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (data["description"], data["amount"], data["paid_by"], data["date"],
                 data["category_id"], me, rid, month),
            )
            eid = cur.lastrowid
            db.executemany(
                "INSERT INTO expense_shares(expense_id, user_id, amount) VALUES (?, ?, ?)",
                [(eid, uid, c) for uid, c in data["shares"].items()],
            )
            db.commit()
            if repeat:
                generate_recurring(db)  # datum in een eerdere maand: tussenliggende maanden aanvullen
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
            if "iban" in body:
                iban = None
                if str(body["iban"] or "").strip():
                    iban = valid_iban(body["iban"])
                    if not iban:
                        raise ApiError(400, "Dit IBAN klopt niet. Controleer het nummer.")
                db.execute("UPDATE users SET iban = ? WHERE id = ?", (iban, me))
            if "iban_name" in body:
                name = re.sub(r"\s+", " ", str(body["iban_name"] or "")).strip()[:70] or None
                db.execute("UPDATE users SET iban_name = ? WHERE id = ?", (name, me))
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
            cols = ", ".join(TASK_FIELDS)
            cur = db.execute(f"INSERT INTO tasks({cols}, created_by) VALUES ({', '.join('?' * len(TASK_FIELDS))}, ?)",
                             [data[k] for k in TASK_FIELDS] + [me])
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
            cols = ", ".join(TASK_FIELDS)
            cur = db.execute(
                f"INSERT INTO tasks(id, {cols}, done, done_at, done_by, created_by, created_at) "
                f"VALUES (?, {', '.join('?' * len(TASK_FIELDS))}, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                [tid] + [data[k] for k in TASK_FIELDS] + [done, done_at, done_by, created_by, created_at])
            db.commit()
            return 201, {"id": cur.lastrowid}

        m = re.fullmatch(r"/tasks/(\d+)/(complete|reopen|undo|transfer)", path)
        if m and method == "POST":
            tid, action = int(m.group(1)), m.group(2)
            row = task_row(db, tid)
            if not row:
                raise ApiError(404, "Taak niet gevonden")
            body = self.read_json()
            result = {}
            if action == "reopen":
                db.execute("UPDATE tasks SET done = 0, done_at = NULL, done_by = NULL WHERE id = ?", (tid,))
                db.execute("DELETE FROM task_log WHERE id = (SELECT id FROM task_log WHERE task_id = ? "
                           "ORDER BY id DESC LIMIT 1)", (tid,))
            elif action == "complete":
                if not row["done"]:
                    today = date.fromisoformat(parse_date(body["today"])) if body.get("today") else date.today()
                    result["log_id"] = complete_task(db, row, me, today)
            elif action == "undo":
                # Afvinken terugdraaien: oude toestand terug en de punten eraf.
                prev = body.get("previous") if isinstance(body.get("previous"), dict) else {}
                users = set(user_ids(db))
                rot = row["rotation"] or []
                due = parse_date(prev["due_date"]) if prev.get("due_date") else None
                assigned = prev.get("assigned_to") if prev.get("assigned_to") in users else None
                turn = prev.get("turn_user") if prev.get("turn_user") in rot else None
                override = prev.get("next_override") if prev.get("next_override") in rot else None
                done_at, done_by = clean_done(db, prev)
                db.execute("UPDATE tasks SET due_date=?, assigned_to=?, turn_user=?, next_override=?, done=?, "
                           "done_at=?, done_by=? WHERE id=?",
                           (due, assigned, turn, override, 1 if prev.get("done") else 0, done_at, done_by, tid))
                if isinstance(body.get("log_id"), int):
                    db.execute("DELETE FROM task_log WHERE id = ? AND task_id = ?", (body["log_id"], tid))
            else:  # transfer: iemand anders neemt deze beurt over, eventueel tegen een vergoeding
                users = {u["id"]: u["name"] for u in list_users(db)}
                to = body.get("to")
                if to not in users or to == row["assigned_to"]:
                    raise ApiError(400, "Kies iemand anders")
                payer = row["assigned_to"] or me
                amount = parse_amount(body["amount"]) if body.get("amount") else 0
                if amount and payer == to:
                    raise ApiError(400, "Je kunt jezelf niet betalen")
                override = row["next_override"]
                if row["rotation"] and body.get("mode") == "swap" and payer in row["rotation"]:
                    override = payer  # ruilen: de ander doet nu, jij de volgende beurt van de ander
                db.execute("UPDATE tasks SET assigned_to = ?, next_override = ? WHERE id = ?", (to, override, tid))
                if amount:
                    cat = db.execute("SELECT id FROM categories WHERE name = 'Overig'").fetchone()
                    cur = db.execute(
                        "INSERT INTO expenses(description, amount, paid_by, date, category_id, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (f"{row['title']} overgenomen door {users[to]}"[:200], amount, to, date.today().isoformat(),
                         cat[0] if cat else None, me))
                    db.execute("INSERT INTO expense_shares(expense_id, user_id, amount) VALUES (?, ?, ?)",
                               (cur.lastrowid, payer, amount))
                    result["expense_id"] = cur.lastrowid
            db.commit()
            task = task_row(db, tid)
            if action == "transfer":
                notify_assigned(db, task, me, extra=f"je krijgt {euro(amount)}" if amount else None)
            return 200, {"task": task, "previous": row, **result}

        m = re.fullmatch(r"/tasks/(\d+)", path)
        if m:
            tid = int(m.group(1))
            row = task_row(db, tid)
            if not row:
                raise ApiError(404, "Taak niet gevonden")
            if method == "PUT":
                data = validate_task(db, self.read_json())
                db.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in TASK_FIELDS)} WHERE id=?",
                           [data[k] for k in TASK_FIELDS] + [tid])
                db.commit()
                task = task_row(db, tid)
                if not task["done"]:
                    notify_assigned(db, task, me, previous=row["assigned_to"])
                return 200, task
            if method == "DELETE":
                db.execute("DELETE FROM tasks WHERE id = ?", (tid,))
                db.commit()
                return 200, {"ok": True, "deleted": row}

        # -- boodschappen
        if path == "/groceries" and method == "GET":
            return 200, grocery_lists(db)

        if path == "/groceries" and method == "POST":
            item, created = add_grocery(db, self.read_json().get("name"), me)
            db.commit()
            return (201 if created else 200), {"item": item, "created": created}

        if path == "/groceries/restore" and method == "POST":
            body = self.read_json()
            gid = body.get("id")
            if not isinstance(gid, int) or isinstance(gid, bool) or db.execute(
                    "SELECT 1 FROM groceries WHERE id = ?", (gid,)).fetchone():
                gid = None
            status = body.get("status") if body.get("status") in ("open", "basket") else "open"
            cur = db.execute("INSERT INTO groceries(id, name, status, added_by) VALUES (?, ?, ?, ?)",
                             (gid, clean_name(body.get("name")), status, me))
            db.commit()
            return 201, {"id": cur.lastrowid}

        if path == "/groceries/checkout" and method == "POST":
            eid = self.read_json().get("expense_id")
            if eid is not None and not (isinstance(eid, int) and db.execute(
                    "SELECT 1 FROM expenses WHERE id = ?", (eid,)).fetchone()):
                raise ApiError(400, "Onbekende uitgave")
            cur = db.execute("UPDATE groceries SET status = 'bought', bought_at = CURRENT_TIMESTAMP, expense_id = ? "
                             "WHERE status = 'basket'", (eid,))
            if not cur.rowcount:
                raise ApiError(400, "Je mandje is leeg")
            db.commit()
            return 200, {"ok": True, "count": cur.rowcount}

        m = re.fullmatch(r"/groceries/(\d+)(/check)?", path)
        if m:
            gid = int(m.group(1))
            row = db.execute("SELECT id, name, status FROM groceries WHERE id = ? AND status != 'bought'",
                             (gid,)).fetchone()
            if not row:
                raise ApiError(404, "Staat niet (meer) op de lijst")
            if m.group(2) and method == "POST":
                checked = bool(self.read_json().get("checked"))
                db.execute("UPDATE groceries SET status = ?, checked_by = ?, checked_at = "
                           "CASE WHEN ? THEN CURRENT_TIMESTAMP END WHERE id = ?",
                           ("basket" if checked else "open", me if checked else None, checked, gid))
                db.commit()
                return 200, {"ok": True}
            if not m.group(2) and method == "PUT":
                db.execute("UPDATE groceries SET name = ? WHERE id = ?", (clean_name(self.read_json().get("name")), gid))
                db.commit()
                return 200, {"ok": True}
            if not m.group(2) and method == "DELETE":
                db.execute("DELETE FROM groceries WHERE id = ?", (gid,))
                db.commit()
                return 200, {"ok": True, "deleted": dict(row)}

        if path == "/staples" and method == "POST":
            name = clean_name(self.read_json().get("name"))
            if db.execute("SELECT 1 FROM staples WHERE lower(name) = lower(?)", (name,)).fetchone():
                raise ApiError(400, "Staat al bij je vaste spullen")
            pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM staples").fetchone()[0]
            cur = db.execute("INSERT INTO staples(name, position) VALUES (?, ?)", (name, pos))
            db.commit()
            return 201, {"id": cur.lastrowid, "name": name}

        m = re.fullmatch(r"/staples/(\d+)", path)
        if m and method == "DELETE":
            db.execute("DELETE FROM staples WHERE id = ?", (int(m.group(1)),))
            db.commit()
            return 200, {"ok": True}

        # -- recepten
        if path == "/recipes/import" and method == "POST":
            return 200, fetch_recipe(self.read_json().get("url"))

        if path in ("/recipes", "/recipes/restore") and method == "POST":
            body = self.read_json()
            data = validate_recipe(body)
            rid = body.get("id") if path == "/recipes/restore" else None
            if not isinstance(rid, int) or isinstance(rid, bool) or recipe_row(db, rid):
                rid = None
            cur = db.execute("INSERT INTO recipes(id, title, servings, ingredients, steps, source_url, created_by) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (rid, data["title"], data["servings"], data["ingredients"], data["steps"],
                              data["source_url"], me))
            db.commit()
            return 201, recipe_row(db, cur.lastrowid)

        m = re.fullmatch(r"/recipes/(\d+)(/add)?", path)
        if m:
            rid = int(m.group(1))
            row = recipe_row(db, rid)
            if not row:
                raise ApiError(404, "Recept niet gevonden")
            if m.group(2) and method == "POST":
                # Gekozen ingrediënten (al omgerekend naar het aantal personen) op de lijst.
                items = self.read_json().get("items")
                if not isinstance(items, list) or not items:
                    raise ApiError(400, "Kies minstens één ingrediënt")
                added = 0
                for name in items[:80]:
                    name = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
                    if name:
                        added += add_grocery(db, name, me)[1]
                db.execute("UPDATE recipes SET used_count = used_count + 1, last_used = CURRENT_TIMESTAMP "
                           "WHERE id = ?", (rid,))
                db.commit()
                return 200, {"ok": True, "added": added}
            if not m.group(2) and method == "PUT":
                data = validate_recipe(self.read_json())
                db.execute("UPDATE recipes SET title=?, servings=?, ingredients=?, steps=?, source_url=? WHERE id=?",
                           (data["title"], data["servings"], data["ingredients"], data["steps"], data["source_url"], rid))
                db.commit()
                return 200, recipe_row(db, rid)
            if not m.group(2) and method == "DELETE":
                db.execute("DELETE FROM recipes WHERE id = ?", (rid,))
                db.commit()
                return 200, {"ok": True, "deleted": row}

        # -- weekmenu
        if path == "/menu/add" and method == "POST":
            body = self.read_json()
            items, days = body.get("items"), body.get("days")
            if not isinstance(items, list) or not isinstance(days, list) or not days:
                raise ApiError(400, "Niets om toe te voegen")
            days = [parse_date(d) for d in days[:31]]
            added = 0
            for name in items[:200]:
                name = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
                if name:
                    added += add_grocery(db, name, me)[1]
            marks = ",".join("?" * len(days))
            db.execute(f"UPDATE recipes SET used_count = used_count + 1, last_used = CURRENT_TIMESTAMP WHERE id IN "
                       f"(SELECT recipe_id FROM meal_plan WHERE day IN ({marks}) AND listed = 0)", days)
            db.execute(f"UPDATE meal_plan SET listed = 1 WHERE day IN ({marks})", days)
            db.commit()
            return 200, {"ok": True, "added": added}

        m = re.fullmatch(r"/menu/(\d{4}-\d{2}-\d{2})", path)
        if m:
            day = parse_date(m.group(1))
            if method == "PUT":
                body = self.read_json()
                rid = body.get("recipe_id")
                title = None
                if rid is not None:
                    if isinstance(rid, bool) or not isinstance(rid, int) or not recipe_row(db, rid):
                        raise ApiError(400, "Recept niet gevonden")
                else:
                    title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:80]
                    if not title:
                        raise ApiError(400, "Kies een recept of vul iets in")
                servings = body.get("servings", 2)
                if isinstance(servings, bool) or not isinstance(servings, int) or not 1 <= servings <= 50:
                    raise ApiError(400, "Kies 1 tot 50 personen")
                old = db.execute("SELECT recipe_id, servings, listed FROM meal_plan WHERE day = ?", (day,)).fetchone()
                # Zelfde recept en aantal personen: wat al op de lijst staat blijft gemarkeerd.
                listed = 1 if old and old["listed"] and old["recipe_id"] == rid and old["servings"] == servings else 0
                db.execute("INSERT INTO meal_plan(day, recipe_id, title, servings, listed) VALUES (?, ?, ?, ?, ?) "
                           "ON CONFLICT(day) DO UPDATE SET recipe_id = excluded.recipe_id, title = excluded.title, "
                           "servings = excluded.servings, listed = excluded.listed", (day, rid, title, servings, listed))
                db.commit()
                return 200, {"ok": True}
            if method == "DELETE":
                db.execute("DELETE FROM meal_plan WHERE day = ?", (day,))
                db.commit()
                return 200, {"ok": True}

        # -- koppeling met Home Assistant (sleutel en adres voor automatiseringen)
        if path == "/ha-info" and method in ("GET", "POST"):
            key = ha_api_key(db, rotate=method == "POST")
            return 200, {"available": bool(SUPERVISOR_TOKEN), "key": key, "host": addon_hostname(), "port": PORT,
                         "sensors": sorted(ha_states(db))}

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
