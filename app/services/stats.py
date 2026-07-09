"""Live per-room booking statistics.

Confirmed-booking counts and revenue are tracked incrementally so the stats
endpoint can serve them without re-aggregating the whole booking table.
"""
import threading

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Booking

_stats: dict[int, dict] = {}
_lock = threading.Lock()


def record_create(room_id: int, price_cents: int) -> None:
    with _lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        _stats[room_id] = {"count": current["count"] + 1, "revenue": current["revenue"] + price_cents}


def record_cancel(room_id: int, price_cents: int) -> None:
    with _lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        _stats[room_id] = {"count": max(0, current["count"] - 1), "revenue": current["revenue"] - price_cents}


def get(room_id: int, db: Session | None = None) -> dict:
    current = _stats.get(room_id)
    if current is None:
        if db is not None:
            count = (
                db.query(Booking)
                .filter(Booking.room_id == room_id, Booking.status == "confirmed")
                .count()
            )
            revenue = (
                db.query(func.sum(Booking.price_cents))
                .filter(Booking.room_id == room_id, Booking.status == "confirmed")
                .scalar()
            ) or 0
            current = {"count": count, "revenue": revenue}
            with _lock:
                _stats[room_id] = current
        else:
            current = {"count": 0, "revenue": 0}
    return current
