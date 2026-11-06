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
import sys
from datetime import date
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

SCHEMA_VERSION = 2
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
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()
    db.close()


def user_ids(db):
    return [r["id"] for r in db.execute("SELECT id FROM users")]


def list_users(db):
    return [dict(r) for r in db.execute("SELECT id, name FROM users ORDER BY created_at, name")]


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
                   e.created_by, e.created_at
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


def validate_expense(db, body):
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
        "date": parse_date(body.get("date")),
        "category_id": category_id,
        "shares": clean,
    }


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
            return 200, self.summary(db, require_month(qs))

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
                db.execute("DELETE FROM expenses WHERE id = ?", (eid,))
                db.commit()
                return 200, {"ok": True}

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
            db.execute("DELETE FROM settlements WHERE id = ?", (int(m.group(1)),))
            db.commit()
            return 200, {"ok": True}

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

    def summary(self, db, month):
        total = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE substr(date,1,7) = ?",
            (month,)).fetchone()[0]
        per_category = [dict(r) for r in db.execute(
            """SELECT e.category_id, SUM(e.amount) AS total, COUNT(*) AS count
               FROM expenses e WHERE substr(e.date,1,7) = ?
               GROUP BY e.category_id ORDER BY total DESC""", (month,))]
        cat_shares = {}
        for r in db.execute(
                """SELECT e.category_id, s.user_id, SUM(s.amount) AS share
                   FROM expenses e JOIN expense_shares s ON s.expense_id = e.id
                   WHERE substr(e.date,1,7) = ? GROUP BY e.category_id, s.user_id""", (month,)):
            cat_shares.setdefault(r["category_id"], {})[r["user_id"]] = r["share"]
        for c in per_category:
            c["shares"] = cat_shares.get(c["category_id"], {})
        per_user = {uid: {"paid": 0, "share": 0} for uid in user_ids(db)}
        for r in db.execute(
                "SELECT paid_by, SUM(amount) s FROM expenses WHERE substr(date,1,7)=? GROUP BY paid_by",
                (month,)):
            per_user.setdefault(r["paid_by"], {"paid": 0, "share": 0})["paid"] = r["s"]
        for r in db.execute(
                """SELECT s.user_id, SUM(s.amount) sh FROM expenses e
                   JOIN expense_shares s ON s.expense_id = e.id
                   WHERE substr(e.date,1,7)=? GROUP BY s.user_id""", (month,)):
            per_user.setdefault(r["user_id"], {"paid": 0, "share": 0})["share"] = r["sh"]
        history = [dict(r) for r in db.execute(
            """SELECT substr(date,1,7) AS month, SUM(amount) AS total FROM expenses
               WHERE substr(date,1,7) <= ? GROUP BY month ORDER BY month DESC LIMIT 12""",
            (month,))]
        history.reverse()
        return {"month": month, "total": total, "per_category": per_category,
                "per_user": per_user, "history": history}

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


def main():
    init_db()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Kostendeler draait op poort {PORT}, database: {DB_PATH}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
