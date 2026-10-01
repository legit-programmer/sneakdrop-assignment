import hashlib
import hmac
import json
import os
import random
import sqlite3
import threading
import time
import urllib.request
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

DB_PATH = os.environ.get("DB_PATH", "sneakdrop.db")
TOTAL_STOCK = int(os.environ.get("TOTAL_STOCK", "20"))
HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "300"))
MAX_PER_USER = 2
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret").encode()
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

app = FastAPI()

def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class tx:
    def __enter__(self):
        self.db = connect()
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, *_):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        self.db.close()


def init_db():
    db = connect()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS holds (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS waitlist (
            user_id TEXT PRIMARY KEY,
            joined_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_events (
            event_id TEXT PRIMARY KEY,
            hold_id TEXT NOT NULL,
            type TEXT NOT NULL,
            result TEXT NOT NULL,
            received_at REAL NOT NULL
        );
    """)
    db.close()


init_db()


def stock_left(db):
    used = db.execute(
        "SELECT COUNT(*) FROM holds WHERE status IN ('active', 'paid')"
    ).fetchone()[0]
    return TOTAL_STOCK - used


def user_counts(db, user_id):
    row = db.execute(
        """SELECT SUM(status = 'active') AS active, SUM(status = 'paid') AS paid
           FROM holds WHERE user_id = ?""",
        (user_id,),
    ).fetchone()
    return (row["active"] or 0), (row["paid"] or 0)


def create_hold(db, user_id):
    now = time.time()
    hold_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO holds VALUES (?, ?, 'active', ?, ?)",
        (hold_id, user_id, now + HOLD_SECONDS, now),
    )
    return hold_id


def sweep(db):
    db.execute(
        "UPDATE holds SET status = 'expired' WHERE status = 'active' AND expires_at <= ?",
        (time.time(),),
    )
    while stock_left(db) > 0:
        nxt = db.execute(
            "SELECT user_id FROM waitlist ORDER BY joined_at LIMIT 1"
        ).fetchone()
        if not nxt:
            break
        user_id = nxt["user_id"]
        db.execute("DELETE FROM waitlist WHERE user_id = ?", (user_id,))
        active, paid = user_counts(db, user_id)
        if active == 0 and paid < MAX_PER_USER:
            create_hold(db, user_id)


def background_sweeper():
    while True:
        try:
            with tx() as db:
                sweep(db)
        except Exception as e:
            print("sweeper error:", e)
        time.sleep(1)


threading.Thread(target=background_sweeper, daemon=True).start()


def check_can_take_pair(db, user_id):
    active, paid = user_counts(db, user_id)
    if active:
        raise HTTPException(409, "You already hold a pair")
    if paid >= MAX_PER_USER:
        raise HTTPException(409, "You already bought the maximum of 2 pairs")


@app.post("/api/buy")
def buy(body: dict):
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        raise HTTPException(400, "user_id required")
    with tx() as db:
        sweep(db)
        check_can_take_pair(db, user_id)
        if stock_left(db) <= 0:
            raise HTTPException(409, "Sold out - you can join the waiting line")
        hold_id = create_hold(db, user_id)
    return {"hold_id": hold_id, "expires_in": HOLD_SECONDS}


@app.post("/api/waitlist")
def join_waitlist(body: dict):
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        raise HTTPException(400, "user_id required")
    with tx() as db:
        sweep(db)
        check_can_take_pair(db, user_id)
        if stock_left(db) > 0:
            raise HTTPException(409, "Stock is available - just click Buy")
        db.execute(
            "INSERT OR IGNORE INTO waitlist VALUES (?, ?)", (user_id, time.time())
        )
    return {"ok": True}


@app.get("/api/state")
def state(user_id: str = ""):
    with tx() as db:
        sweep(db)
        stock = stock_left(db)
        hold = db.execute(
            "SELECT id, expires_at FROM holds WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        _, paid = user_counts(db, user_id)
        pos = None
        me = db.execute(
            "SELECT joined_at FROM waitlist WHERE user_id = ?", (user_id,)
        ).fetchone()
        if me:
            pos = db.execute(
                "SELECT COUNT(*) FROM waitlist WHERE joined_at <= ?", (me["joined_at"],)
            ).fetchone()[0]
    return {
        "stock": stock,
        "hold_id": hold["id"] if hold else None,
        "hold_seconds_left": max(0, int(hold["expires_at"] - time.time())) if hold else None,
        "waitlist_position": pos,
        "pairs_bought": paid,
    }


def sign(payload: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET, payload, hashlib.sha256).hexdigest()


def send_webhook(event: dict, delay: float):
    time.sleep(delay)
    payload = json.dumps(event).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/webhooks/payment",
        data=payload,
        headers={"Content-Type": "application/json", "X-Signature": sign(payload)},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print("webhook delivery failed:", e)


@app.post("/fake-pay/{hold_id}")
def fake_pay(hold_id: str):
    succeeded = {"event_id": uuid.uuid4().hex, "hold_id": hold_id, "type": "payment.succeeded"}
    pending = {"event_id": uuid.uuid4().hex, "hold_id": hold_id, "type": "payment.pending"}
    delay = random.uniform(0.2, 3)
    threading.Thread(target=send_webhook, args=(succeeded, delay)).start()
    if random.random() < 0.3:
        threading.Thread(target=send_webhook, args=(succeeded, delay + random.uniform(0, 2))).start()
    if random.random() < 0.3:
        threading.Thread(target=send_webhook, args=(pending, delay + 1)).start()
    return {"ok": True, "message": "Payment submitted, waiting for confirmation"}


@app.post("/webhooks/payment")
async def payment_webhook(request: Request):
    payload = await request.body()
    if not hmac.compare_digest(sign(payload), request.headers.get("X-Signature", "")):
        raise HTTPException(401, "bad signature")
    return handle_payment_event(json.loads(payload))


def handle_payment_event(event: dict):
    with tx() as db:
        seen = db.execute(
            "SELECT result FROM payment_events WHERE event_id = ?", (event["event_id"],)
        ).fetchone()
        if seen:
            return {"result": seen["result"], "duplicate": True}

        hold = db.execute("SELECT * FROM holds WHERE id = ?", (event["hold_id"],)).fetchone()
        if event["type"] != "payment.succeeded":
            result = "ignored"
        elif not hold:
            result = "unknown_hold"
        elif hold["status"] == "paid":
            result = "already_paid"
        elif hold["status"] == "active" and hold["expires_at"] > time.time():
            db.execute("UPDATE holds SET status = 'paid' WHERE id = ?", (hold["id"],))
            result = "paid"
        else:
            result = "late_refund"

        db.execute(
            "INSERT INTO payment_events VALUES (?, ?, ?, ?, ?)",
            (event["event_id"], event["hold_id"], event["type"], result, time.time()),
        )
    print(f"webhook {event['type']} hold={event['hold_id'][:8]} -> {result}")
    return {"result": result}


PAGE = """<!doctype html>
<html><body style="font-family: monospace">
<h2>Sneaker Drop</h2>
<p>User: <input id="user" placeholder="your name"></p>
<p>Pairs left: <b id="stock">?</b></p>
<p>Your hold: <span id="hold">none</span></p>
<p>Waiting line position: <span id="pos">-</span></p>
<p>Pairs you bought: <span id="bought">0</span></p>
<button onclick="act('/api/buy')">Buy</button>
<button onclick="act('/api/waitlist')">Join waiting line</button>
<button onclick="pay()">Pay</button>
<p id="msg"></p>
<script>
let holdId = null;
const user = document.getElementById('user');
user.value = new URLSearchParams(location.search).get('user') || '';
const msg = t => document.getElementById('msg').textContent = t;

async function act(url) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify({user_id: user.value})});
  const j = await r.json();
  msg(r.ok ? 'OK' : j.detail);
  refresh();
}
async function pay() {
  if (!holdId) return msg('No hold to pay for');
  const j = await (await fetch('/fake-pay/' + holdId, {method: 'POST'})).json();
  msg(j.message);
}
async function refresh() {
  const s = await (await fetch('/api/state?user_id=' + encodeURIComponent(user.value))).json();
  holdId = s.hold_id;
  const left = s.hold_seconds_left;
  document.getElementById('stock').textContent = s.stock;
  document.getElementById('hold').textContent = holdId
    ? Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0') + ' left to pay'
    : 'none';
  document.getElementById('pos').textContent = s.waitlist_position === null ? '-' : s.waitlist_position;
  document.getElementById('bought').textContent = s.pairs_bought;
}
setInterval(refresh, 1000);
refresh();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE
