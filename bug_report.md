# Debugging Report — CoWork API Bug Fixes

This document catalogs every bug discovered in the CoWork codebase, explains why it
violates the business rules / API contract, and describes the exact fix applied.

---

## Table of Contents

1. [UTC Timezone Handling](#1-utc-timezone-handling)
2. [Access Token Expiration](#2-access-token-expiration)
3. [Revoked Token Check Uses Wrong Claim](#3-revoked-token-check-uses-wrong-claim)
4. [Refresh Token Not Single-Use](#4-refresh-token-not-single-use)
5. [Duplicate Registration Returns 200 Instead of 409](#5-duplicate-registration-returns-200-instead-of-409)
6. [Start Time Grace Window](#6-start-time-grace-window)
7. [Missing Minimum Duration Check](#7-missing-minimum-duration-check)
8. [Overlap Check Uses `<=` Instead of `<`](#8-overlap-check-uses--instead-of-)
9. [Race Condition — Double Booking](#9-race-condition--double-booking)
10. [Race Condition — Quota Exceeded](#10-race-condition--quota-exceeded)
11. [Race Condition — Duplicate Reference Codes](#11-race-condition--duplicate-reference-codes)
12. [Race Condition — Rate Limit Bypass](#12-race-condition--rate-limit-bypass)
13. [Race Condition — Lost Stats Updates](#13-race-condition--lost-stats-updates)
14. [Deadlock in Notification Locks](#14-deadlock-in-notification-locks)
15. [Booking List Sorted Descending](#15-booking-list-sorted-descending)
16. [Pagination Offset Wrong](#16-pagination-offset-wrong)
17. [Pagination Limit Hardcoded](#17-pagination-limit-hardcoded)
18. [Get Booking Overwrites `start_time` with `created_at`](#18-get-booking-overwrites-start_time-with-created_at)
19. [Cancel Refund Percent for <24h Is 50% Instead of 0%](#19-cancel-refund-percent-for-24h-is-50-instead-of-0)
20. [Refund Amount Inconsistency Between Response and RefundLog](#20-refund-amount-inconsistency-between-response-and-refundlog)
21. [Half-Cent Rounding Uses Banker's Rounding Instead of Half-Up](#21-half-cent-rounding-uses-bankers-rounding-instead-of-half-up)
22. [Usage Report Cache Not Invalidated on Booking Creation](#22-usage-report-cache-not-invalidated-on-booking-creation)
23. [Export `fetch_bookings_raw` Missing Org Filter](#23-export-fetch_bookings_raw-missing-org-filter)
24. [Stats Cache Returns 0 on Cold Start](#24-stats-cache-returns-0-on-cold-start)

---

## 1. UTC Timezone Handling

**File:** `app/timeutils.py:13`
**Rule:** 1

### Bug
```python
dt = dt.replace(tzinfo=None)
```
`datetime.replace(tzinfo=None)` strips the timezone offset **without converting**.
An input like `2026-07-11T15:00:00+05:00` is stored as `15:00` (local time) instead
of `10:00` (UTC).

### Fix
```python
dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
```
`astimezone(timezone.utc)` first converts to UTC, then `.replace(tzinfo=None)` strips
the tzinfo for naive-UTC storage.

---

## 2. Access Token Expiration

**File:** `app/auth.py:53`
**Rule:** 8

### Bug
```python
lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
```
`ACCESS_TOKEN_EXPIRE_MINUTES = 15`, so `15 * 60 = 900` minutes = **15 hours**.
The rule requires exactly **900 seconds** (15 minutes).

### Fix
```python
lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
```
Now `timedelta(minutes=15)` = 900 seconds.

---

## 3. Revoked Token Check Uses Wrong Claim

**File:** `app/auth.py:97`
**Rule:** 8

### Bug
```python
if payload.get("sub") in _revoked_tokens:
```
`revoke_access_token` stores the **`jti`** (token ID) in `_revoked_tokens`, but the
check reads the **`sub`** (user ID). Since `jti` and `sub` are never equal, the
check *never* matches — revoked tokens remain usable forever.

### Fix
```python
if payload.get("jti") in _revoked_tokens:
```

---

## 4. Refresh Token Not Single-Use

**File:** `app/routers/auth.py:77-93`
**Rule:** 8

### Bug
The refresh endpoint decodes the token and returns a new pair but never invalidates
the presented refresh token. The same refresh token can be reused indefinitely.

### Fix
Added `_used_refresh_tokens: set[str]` and `_refresh_lock` in `auth.py`. The new
`check_refresh_token(payload)` function atomically checks and records the `jti`.
If the `jti` is already in the set, it raises `401 UNAUTHORIZED`.

**Files changed:** `app/auth.py` (lines 26-27, 88-92), `app/routers/auth.py` (line 82)

---

## 5. Duplicate Registration Returns 200 Instead of 409

**File:** `app/routers/auth.py:37-43`
**Rule:** 15

### Bug
```python
if existing is not None:
    return {
        "user_id": existing.id,
        ...
    }
```
When a username already exists in the org, the endpoint returns the existing user
with status 200 instead of raising `409 USERNAME_TAKEN`.

### Fix
```python
if existing is not None:
    raise AppError(409, "USERNAME_TAKEN", "Username already taken")
```

---

## 6. Start Time Grace Window

**File:** `app/routers/bookings.py:72`
**Rule:** 2

### Bug
```python
if start <= now - timedelta(seconds=300):
```
Allows start times up to **5 minutes in the past**. The rule says "strictly in the
future at request time — no grace window."

### Fix
```python
if start <= now:
```

---

## 7. Missing Minimum Duration Check

**File:** `app/routers/bookings.py:79`
**Rule:** 2

### Bug
```python
if duration_hours > MAX_DURATION_HOURS:
    raise ...
```
The maximum (8h) was checked but the minimum (1h) was not. Zero-hour or negative
durations were accepted.

### Fix
```python
if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")
```

---

## 8. Overlap Check Uses `<=` Instead of `<`

**File:** `app/routers/bookings.py:37`
**Rule:** 3

### Bug
```python
if b.start_time <= end and start <= b.end_time:
```
Two intervals overlap iff `existing.start < new.end AND new.start < existing.end`.
The `<=` on both sides makes back-to-back bookings (e.g. 9-10 and 10-11) **conflict**
when they should be allowed.

### Fix
```python
if b.start_time < end and start < b.end_time:
```

---

## 9. Race Condition — Double Booking

**File:** `app/routers/bookings.py:30-39`
**Rule:** 3

### Bug
`_has_conflict` loads all confirmed bookings, then calls `_pricing_warmup()` which
sleeps 0.12 seconds, then checks overlap in Python. Two concurrent requests can both
pass the check before either commits, resulting in overlapping confirmed bookings.

Root cause: read-modify-write without atomicity, plus an artificial sleep that
widens the race window.

### Fix
- Removed the `_pricing_warmup()` call (and all other artificial `time.sleep()` calls).
- Added `_booking_lock = threading.Lock()` and wrapped the conflict check + quota
  check + booking creation in `with _booking_lock:`.

**File:** `app/routers/bookings.py`

---

## 10. Race Condition — Quota Exceeded

**File:** `app/routers/bookings.py:42-57`
**Rule:** 4

### Bug
`_check_quota` counts confirmed bookings, calls `_quota_audit()` (0.1s sleep), then
checks the count. Concurrent requests can bypass the 3-booking limit.

### Fix
Same fix as #9 — the quota check is inside the `_booking_lock`, so only one request
performs the check+create atomically. The artificial sleep was removed.

---

## 11. Race Condition — Duplicate Reference Codes

**File:** `app/services/reference.py`
**Rule:** 7

### Bug
```python
def next_reference_code() -> str:
    current = _counter["value"]
    _format_pause()  # 0.12s sleep widens race window
    _counter["value"] = current + 1
    return f"CW-{current:06d}"
```
Read-modify-write with no lock. Two concurrent calls can read the same `current`
value and produce identical codes.

### Fix
Added `threading.Lock()` and wrapped the increment in `with _lock:`. Removed the
artificial sleep.

---

## 12. Race Condition — Rate Limit Bypass

**File:** `app/services/ratelimit.py`
**Rule:** 5

### Bug
```python
bucket = _buckets.get(user_id, [])
bucket = [t for t in bucket if t > now - _WINDOW_SECONDS]
_settle_pause()  # 0.1s sleep
bucket.append(now)
_buckets[user_id] = bucket
```
Two concurrent requests from the same user can both read the same (non-full) bucket,
append, and both pass — exceeding the 20/60s limit.

### Fix
Added `threading.Lock()` around the entire read-trim-append-check sequence. Removed
the artificial sleep.

---

## 13. Race Condition — Lost Stats Updates

**File:** `app/services/stats.py:12-15`
**Rule:** 14

### Bug
```python
current = _stats.get(room_id, {"count": 0, "revenue": 0})
count, revenue = current["count"], current["revenue"]
_aggregate_pause()  # 0.1s sleep
_stats[room_id] = {"count": count + 1, "revenue": revenue + price_cents}
```
Two concurrent booking creates on the same room can both read the same `count`/`revenue`,
then both write back `count+1` — losing one increment entirely.

### Fix
Added `threading.Lock()` around the read-modify-write. Removed the artificial sleep.

---

## 14. Deadlock in Notification Locks

**File:** `app/services/notifications.py:24-35`
**Rule:** 16

### Bug
```python
def notify_created(booking):
    with _email_lock:
        _send_email("created", booking)
        with _audit_lock:          # email → audit
            _write_audit("created", booking)

def notify_cancelled(booking):
    with _audit_lock:
        _write_audit("cancelled", booking)
        with _email_lock:          # audit → email (REVERSED)
            _send_email("cancelled", booking)
```
`notify_created` acquires locks in order (email → audit) while `notify_cancelled`
acquires them in the opposite order (audit → email). If both are called concurrently,
each holds one lock and waits for the other — a classic ABBA deadlock.

### Fix
Changed both functions to acquire locks in the **same order** (email first, then
audit) and release before acquiring the other:

```python
def notify_cancelled(booking):
    with _email_lock:
        _send_email("cancelled", booking)
    with _audit_lock:
        _write_audit("cancelled", booking)
```

---

## 15. Booking List Sorted Descending

**File:** `app/routers/bookings.py:125`
**Rule:** 11

### Bug
```python
base.order_by(Booking.start_time.desc(), Booking.id.asc())
```
Items were sorted descending by `start_time`. The rule requires ascending.

### Fix
```python
base.order_by(Booking.start_time.asc(), Booking.id.asc())
```

---

## 16. Pagination Offset Wrong

**File:** `app/routers/bookings.py:126`
**Rule:** 11

### Bug
```python
.offset(page * limit)
```
For page=1, offset=10 — skipping the first 10 items. Page 1 returned items 11-20.
The correct formula is `(page - 1) * limit`.

### Fix
```python
.offset((page - 1) * limit)
```

---

## 17. Pagination Limit Hardcoded

**File:** `app/routers/bookings.py:127`
**Rule:** 11

### Bug
```python
.limit(10)
```
The `limit` query parameter was parsed but ignored; the limit was always 10.

### Fix
```python
.limit(limit)
```

---

## 18. Get Booking Overwrites `start_time` with `created_at`

**File:** `app/routers/bookings.py:166`
**Rule:** API Contract

### Bug
```python
response = serialize_booking(booking)
response["start_time"] = iso_utc(booking.created_at)
```
After serializing the booking (which correctly populates `start_time`), the next line
**overwrites** `start_time` with `created_at`. In responses, `start_time` always
equals `created_at`.

### Fix
Removed the overwriting line. The `serialize_booking` function already sets
`start_time` correctly.

---

## 19. Cancel Refund Percent for <24h Is 50% Instead of 0%

**File:** `app/routers/bookings.py:192-193`
**Rule:** 6

### Bug
```python
elif notice_hours >= 24:
    refund_percent = 50
else:
    refund_percent = 50   # ← should be 0
```
Both branches set 50%. The `else` (notice < 24h) should return 0%.

### Fix
```python
elif notice_hours >= 24:
    refund_percent = 50
else:
    refund_percent = 0
```

---

## 20. Refund Amount Inconsistency Between Response and RefundLog

**Files:** `app/routers/bookings.py:195`, `app/services/refunds.py`
**Rule:** 6

### Bug
The cancel response calculated `refund_amount_cents`:
```python
refund_amount_cents = round(booking.price_cents * (refund_percent / 100.0))
```
But `log_refund` recalculated independently using a different method:
```python
dollars = booking.price_cents / 100.0
refund_dollars = dollars * (percent / 100.0)
amount_cents = int(refund_dollars * 100)
```
These produce different results for fractional-cent values (e.g. 50% of 499 →
`round(249.5) = 250` vs `int(249.5) = 249`), violating the rule that "the amount
returned by the cancel response must equal the amount stored in the RefundLog."

### Fix
Changed `log_refund` to **accept the pre-calculated amount** instead of recalculating:

```python
def log_refund(db, booking, amount_cents):
    # stores amount_cents directly — no recalculation
```

Call site in `cancel_booking`:
```python
log_refund(db, booking, refund_amount_cents)
```

---

## 21. Half-Cent Rounding Uses Banker's Rounding Instead of Half-Up

**File:** `app/routers/bookings.py:195`
**Rule:** 6

### Bug
Python's built-in `round()` uses **banker's rounding** (round half to even):
`round(248.5) = 248`, `round(249.5) = 250`. This is incorrect for financial
calculations where half-cents should **always round up**.

### Fix
```python
refund_amount_cents = math.floor(booking.price_cents * (refund_percent / 100.0) + 0.5)
```
`math.floor(x + 0.5)` implements round-half-up for positive numbers:
- 249.5 → floor(250.0) = 250
- 248.5 → floor(249.0) = 249

Added `import math` to the file.

---

## 22. Usage Report Cache Not Invalidated on Booking Creation

**File:** `app/routers/bookings.py:108-109`
**Rule:** 12

### Bug
```python
cache.invalidate_availability(room.id, start.date().isoformat())
# cache.invalidate_report(user.org_id) was MISSING
```
Creating a new booking invalidated the availability cache but **not** the usage
report cache. The `/admin/usage-report` endpoint returned stale data until the cache
was cleared by a cancellation.

### Fix
```python
cache.invalidate_availability(room.id, start.date().isoformat())
cache.invalidate_report(user.org_id)  # added
```

---

## 23. Export `fetch_bookings_raw` Missing Org Filter

**File:** `app/services/export.py:22-29`
**Rule:** 9

### Bug
```python
def fetch_bookings_raw(db, room_id):
    return db.query(Booking).filter(Booking.room_id == room_id).all()
```
When `include_all=True` and `room_id` is specified, the function returns bookings
for that room **regardless of org**. An admin from org A could pass a room_id from
org B and read their data — a multi-tenancy breach.

### Fix
```python
def fetch_bookings_raw(db, org_id, room_id):
    return (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Room.org_id == org_id, Booking.room_id == room_id)
        .all()
    )
```
Also updated the call site in `app/routers/admin.py` and `generate_export` to pass
`org_id`.

---

## 24. Stats Cache Returns 0 on Cold Start

**Files:** `app/services/stats.py:24-25`, `app/routers/rooms.py:110`
**Rule:** 14

### Bug
```python
# stats.py
def get(room_id: int) -> dict:
    return _stats.get(room_id, {"count": 0, "revenue": 0})

# rooms.py
current = stats.get(room.id)
```
The in-memory `_stats` dict starts empty when the server boots. If the database
already has confirmed bookings (from a previous run), `GET /rooms/{id}/stats`
returns `{"count": 0, "revenue": 0}` — completely wrong. Rule 14 requires stats
to be "always consistent with the bookings themselves."

Additionally, `record_create` and `record_cancel` would start incrementing from
0/0 even though existing bookings exist, causing the cached numbers to diverge
from reality permanently.

### Fix
Modified `stats.get()` to accept an optional `db` session. On cache miss, it
queries the database for confirmed-booking count and revenue:

```python
def get(room_id: int, db: Session | None = None) -> dict:
    current = _stats.get(room_id)
    if current is None:
        if db is not None:
            count = db.query(Booking).filter(
                Booking.room_id == room_id,
                Booking.status == "confirmed"
            ).count()
            revenue = db.query(func.sum(Booking.price_cents)).filter(
                Booking.room_id == room_id,
                Booking.status == "confirmed"
            ).scalar() or 0
            current = {"count": count, "revenue": revenue}
            _stats[room_id] = current  # warm the cache
        else:
            current = {"count": 0, "revenue": 0}
    return current
```

Updated all call sites to pass `db`:
- `app/routers/rooms.py:110` — `stats.get(room.id, db)`
- `app/routers/bookings.py:107` — `stats.get(room.id, db)` before `record_create`
- `app/routers/bookings.py:202` — `stats.get(booking.room_id, db)` before `record_cancel`
