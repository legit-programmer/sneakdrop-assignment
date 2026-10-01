# Notes

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`

No database server needed - it uses a local SQLite file (`sneakdrop.db`).

## How to run

```bash
cd src
uvicorn app:app --port 8000
```

Open http://127.0.0.1:8000/?user=alice (use a different `?user=` in another tab to act as a second buyer).

Optional environment variables: `TOTAL_STOCK` (20), `HOLD_SECONDS` (300), `WEBHOOK_SECRET`, `BASE_URL` (where the fake payment company sends webhooks, default `http://127.0.0.1:8000`), `DB_PATH`.
To start a fresh sale, stop the server and delete `src/sneakdrop.db`.

## How it works

Everything is in `src/app.py`.

**No overselling.** Stock is never stored as a number that can drift. It is computed:
`pairs left = 20 - (active holds + paid holds)`. Every action that changes stock runs inside a
SQLite `BEGIN IMMEDIATE` transaction, which takes the database write lock, so two buyers can never
both see "1 left" and both take it. This also holds if you run several server processes.

**Holds (5 min).** `Buy` creates a hold row with `expires_at`. A background loop (every 1 second),
plus every request, marks old holds as `expired`, which frees the pair.

**Limits.** A user can't buy if they have an active hold, or already have 2 paid holds.

**Waiting line.** If stock is 0 a user can join the line. When a pair frees up, the first person in
line (by join time) is removed from the line and gets a new 5-minute hold automatically.

**Fake payments.** `Pay` calls `/fake-pay/{hold_id}`, which acts like a payment company: it sends a
signed (HMAC) `payment.succeeded` webhook to `/webhooks/payment` after a random delay, sometimes sends
it twice, and sometimes sends a `payment.pending` after the success (wrong order).
The webhook handler:
- rejects bad signatures,
- stores every `event_id`, so a duplicate does nothing (idempotent),
- only `payment.succeeded` changes state, so order doesn't matter,
- if the payment arrives after the hold expired, it is marked `late_refund` instead of selling a
  pair that may already belong to someone else.

**Page.** `/` is plain text: pairs left, your hold countdown, your waiting line position, pairs bought.
It polls `/api/state` every second.

## API

| Method | Path | Body |
|---|---|---|
| POST | `/api/buy` | `{"user_id": "alice"}` |
| POST | `/api/waitlist` | `{"user_id": "alice"}` |
| GET | `/api/state?user_id=alice` | |
| POST | `/fake-pay/{hold_id}` | |
| POST | `/webhooks/payment` | signed event, `X-Signature` header |
