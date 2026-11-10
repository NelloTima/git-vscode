"""Intercity bus ticketing: operators, timetables, seat maps, seat holds, tickets.

Model
-----
- `trips` is a timetable row: operator + route + departure time + bus class, running on
  given weekdays. A *departure* is (trip_id, travel_date).
- `tickets` is one purchase (PNR) for one departure; `ticket_seats` holds its seats.
  A partial UNIQUE index on active seats makes double-selling impossible, even under
  concurrent requests. Unpaid tickets hold their seats for HOLD_MINUTES, then expire.
"""
from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared import places
from shared.storage import Database, now as now_iso, short_code

DB = Database(os.environ.get("VIAJAMOZ_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "viajamoz.db")))

TZ = ZoneInfo("Africa/Maputo")
HOLD_MINUTES = 10
MAX_SEATS_PER_TICKET = 6
BOOKING_CUTOFF_MIN = 30          # online sales close 30 min before departure
BOOKING_HORIZON_DAYS = 90
FESTIVE_SURCHARGE = 0.15         # 15 Dez – 10 Jan
# Demo only: pretend part of each bus is already sold so seat maps look realistic.
# MUST be "0" in production (see README).
DEMO_FILL = os.environ.get("DEMO_FILL", "1") != "0"

PROMOS = {
    "BEMVINDO": {"pct": 10, "max": 300, "label": "10% na primeira viagem (máx. 300 MT)"},
    "ESTUDANTE": {"pct": 15, "max": 500, "label": "15% para estudantes com cartão (máx. 500 MT)"},
}


def now() -> datetime:
    return datetime.now(TZ)


# ------------------------------------------------------------ bus classes --

def _rows(n_rows: int, pattern: tuple[int, int], back_row: int = 0) -> list[list[int | None]]:
    left, right = pattern
    rows, n = [], 1
    for _ in range(n_rows):
        row: list[int | None] = list(range(n, n + left)) + [None] + list(range(n + left, n + left + right))
        n += left + right
        rows.append(row)
    if back_row:
        rows.append(list(range(n, n + back_row)))
    return rows


@dataclass(frozen=True)
class BusClass:
    slug: str
    name: str
    fare_mult: float
    layout: tuple
    amenities: str

    @property
    def seats(self) -> int:
        return sum(1 for row in self.layout for s in row if s)


BUS_CLASSES: dict[str, BusClass] = {b.slug: b for b in [
    BusClass("executivo", "Executivo 2+1", 1.35, tuple(map(tuple, _rows(12, (2, 1)))),
             "Ar condicionado,Bancos reclináveis,WC,Tomadas USB,Wi-Fi,Água"),
    BusClass("semiluxo", "Semi-luxo AC 2+2", 1.0, tuple(map(tuple, _rows(11, (2, 2), back_row=5))),
             "Ar condicionado,Bancos reclináveis,TV,Tomadas USB"),
    BusClass("standard", "Standard 2+2", 0.85, tuple(map(tuple, _rows(13, (2, 2), back_row=5))),
             "Ventilação,Porta-bagagens"),
    BusClass("minibus", "Minibus 15 lugares", 0.9,
             ((1, 2), (3, 4, None, 5), (6, 7, None, 8), (9, 10, None, 11), (12, 13, 14, 15)),
             "Ar condicionado,Rápido"),
]}

# --------------------------------------------------------------- network --
# (a, b, duration_minutes, semi-luxo fare MT). Both directions are created.
CORRIDORS = [
    ("Maputo", "Xai-Xai", 210, 700), ("Maputo", "Chókwè", 210, 650),
    ("Maputo", "Bilene", 180, 550), ("Maputo", "Ponta do Ouro", 120, 450),
    ("Maputo", "Maxixe", 390, 1300), ("Maputo", "Inhambane", 420, 1400),
    ("Maputo", "Vilankulo", 600, 1900), ("Maputo", "Beira", 1020, 3300),
    ("Maputo", "Chimoio", 960, 3200), ("Maputo", "Tete", 1320, 4300),
    ("Maputo", "Quelimane", 1560, 4600), ("Maputo", "Nampula", 2040, 5800),
    ("Maputo", "Nelspruit (África do Sul)", 240, 1300), ("Maputo", "Joanesburgo (África do Sul)", 480, 2300),
    ("Maputo", "Mbabane (Eswatini)", 240, 900),
    ("Inhambane", "Vilankulo", 270, 800), ("Beira", "Chimoio", 180, 550), ("Beira", "Tete", 540, 1600),
    ("Beira", "Quelimane", 540, 1500), ("Chimoio", "Tete", 360, 1100), ("Chimoio", "Harare (Zimbabwe)", 420, 1800),
    ("Tete", "Blantyre (Malawi)", 300, 1200), ("Nampula", "Pemba", 420, 1100), ("Nampula", "Nacala", 210, 500),
    ("Nampula", "Ilha de Moçambique", 180, 450), ("Nampula", "Cuamba", 420, 900),
    ("Nampula", "Quelimane", 600, 1500), ("Cuamba", "Lichinga", 360, 900),
]

# Fictional demo operators: name, base city, rating, phone
OPERATORS = [
    ("Kanimambo Express", "Maputo", 4.6, "+258841000101"),
    ("Expresso Índico", "Maputo", 4.4, "+258851000102"),
    ("Zambeze Linhas", "Beira", 4.3, "+258861000103"),
    ("Rovuma Transportes", "Nampula", 4.2, "+258871000104"),
    ("Limpopo Coach", "Maputo", 4.1, "+258821000105"),
    ("Lebombo Cross-Border", "Maputo", 4.5, "+258841000106"),
    ("Savana Bus", "Chimoio", 4.0, "+258861000107"),
    ("Sol do Norte", "Nampula", 4.3, "+258871000108"),
]

BOARDING = {
    "Maputo": ["Terminal da Junta (Av. de Moçambique)", "Xiquelene (Praça dos Combatentes)",
               "Baixa (Av. 25 de Setembro)", "Portagem de Maputo"],
    "Matola": ["Matola Gare", "Shoprite Matola"],
    "Beira": ["Terminal Rodoviário da Beira", "Praça do Maquinino", "Inchope (cruzamento)"],
}


def points_for(city: str) -> list[str]:
    return BOARDING.get(city, [f"Terminal Rodoviário de {city.split(' (')[0]}", f"Centro de {city.split(' (')[0]}"])


def round_fare(v: float) -> int:
    return int(round(v / 10.0) * 10)


SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, city TEXT, rating REAL DEFAULT 4.0,
    phone TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id INTEGER NOT NULL REFERENCES operators(id),
    origin TEXT NOT NULL, destination TEXT NOT NULL,
    depart TEXT NOT NULL,                -- HH:MM local time
    duration_min INTEGER NOT NULL,
    bus_class TEXT NOT NULL,
    fare INTEGER NOT NULL,               -- MT per seat, IVA included
    weekdays TEXT NOT NULL DEFAULT '0123456',  -- Monday=0
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips(origin, destination, active);
CREATE TABLE IF NOT EXISTS tickets (
    pnr TEXT PRIMARY KEY,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    travel_date TEXT NOT NULL,
    boarding TEXT, dropping TEXT,
    contact_name TEXT, phone TEXT, email TEXT,
    fare_total INTEGER NOT NULL, discount INTEGER NOT NULL DEFAULT 0, promo TEXT,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,                -- reservado | confirmado | cancelado | expirado
    hold_expires INTEGER,                -- epoch seconds while status = reservado
    payment_method TEXT,
    refund INTEGER DEFAULT 0,
    boarded_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_dep ON tickets(trip_id, travel_date, status);
CREATE TABLE IF NOT EXISTS ticket_seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pnr TEXT NOT NULL REFERENCES tickets(pnr),
    trip_id INTEGER NOT NULL, travel_date TEXT NOT NULL, seat INTEGER NOT NULL,
    passenger TEXT, gender TEXT, age INTEGER, doc TEXT,
    active INTEGER                       -- 1 while held/sold, NULL when released
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_seat ON ticket_seats(trip_id, travel_date, seat) WHERE active = 1;
"""


def init() -> None:
    DB.init(SCHEMA)
    with DB.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 0:
            _seed(conn)


def _seed(conn: sqlite3.Connection) -> None:
    for name, city, rating, phone in OPERATORS:
        conn.execute("INSERT OR IGNORE INTO operators (name, city, rating, phone) VALUES (?,?,?,?)",
                     (name, city, rating, phone))
    ops = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM operators")}
    rnd = random.Random(2580)
    south = ["Kanimambo Express", "Expresso Índico", "Limpopo Coach"]
    centre = ["Zambeze Linhas", "Savana Bus", "Kanimambo Express"]
    north = ["Rovuma Transportes", "Sol do Norte"]
    for a, b, dur, fare in CORRIDORS:
        cross = places.LOCATIONS[b][3] != "MZ"
        if cross:
            pool = ["Lebombo Cross-Border", "Kanimambo Express"]
        elif a in ("Nampula", "Cuamba"):
            pool = north
        elif a in ("Beira", "Chimoio", "Tete"):
            pool = centre
        else:
            pool = south
        if dur <= 240:        # short route: several departures, minibuses + buses
            slots = [("05:30", "minibus"), ("07:00", "semiluxo"), ("09:30", "minibus"),
                     ("12:00", "standard"), ("14:30", "minibus"), ("16:30", "semiluxo")]
        elif dur <= 720:
            slots = [("04:00", "semiluxo"), ("06:00", "executivo"), ("08:30", "standard"), ("21:00", "semiluxo")]
        else:                 # long haul leaves before dawn, plus one overnight
            slots = [("03:30", "semiluxo"), ("04:30", "executivo"), ("05:00", "standard"), ("18:00", "executivo")]
        if cross:
            slots = [("06:00", "executivo"), ("08:00", "semiluxo"), ("14:00", "executivo")]
        for o, d in ((a, b), (b, a)):
            for i, (dep, cls) in enumerate(slots):
                op = pool[i % len(pool)]
                weekdays = "0123456" if rnd.random() > 0.15 else "01245"   # a few don't run every day
                f = round_fare(fare * BUS_CLASSES[cls].fare_mult)
                conn.execute("INSERT INTO trips (operator_id, origin, destination, depart, duration_min, bus_class,"
                             " fare, weekdays) VALUES (?,?,?,?,?,?,?,?)",
                             (ops[op], o, d, dep, dur + rnd.choice((-15, 0, 0, 20)), cls, f, weekdays))


# ---------------------------------------------------------------- queries --

class BookingError(Exception):
    pass


def departure_dt(trip, travel_date: date) -> datetime:
    h, m = map(int, trip["depart"].split(":"))
    return datetime(travel_date.year, travel_date.month, travel_date.day, h, m, tzinfo=TZ)


def arrival_dt(trip, travel_date: date) -> datetime:
    return departure_dt(trip, travel_date) + timedelta(minutes=trip["duration_min"])


def is_festive(d: date) -> bool:
    return (d.month == 12 and d.day >= 15) or (d.month == 1 and d.day <= 10)


def seat_fare(trip, d: date) -> int:
    return round_fare(trip["fare"] * (1 + FESTIVE_SURCHARGE)) if is_festive(d) else trip["fare"]


def runs_on(trip, d: date) -> bool:
    return str(d.weekday()) in trip["weekdays"]


def bookable(trip, d: date) -> bool:
    if not runs_on(trip, d) or not trip["active"]:
        return False
    t = now()
    return t + timedelta(minutes=BOOKING_CUTOFF_MIN) < departure_dt(trip, d) <= t + timedelta(days=BOOKING_HORIZON_DAYS)


def get_trip(conn, trip_id: int):
    return conn.execute("SELECT t.*, o.name AS operator, o.rating, o.phone AS operator_phone FROM trips t "
                        "JOIN operators o ON o.id = t.operator_id WHERE t.id = ?", (trip_id,)).fetchone()


def expire_holds(conn) -> None:
    stale = [r[0] for r in conn.execute("SELECT pnr FROM tickets WHERE status='reservado' AND hold_expires < ?",
                                        (int(time.time()),))]
    for pnr in stale:
        conn.execute("UPDATE ticket_seats SET active = NULL WHERE pnr = ?", (pnr,))
        conn.execute("UPDATE tickets SET status='expirado' WHERE pnr = ?", (pnr,))


def demo_taken(trip_id: int, d: date, capacity: int) -> set[int]:
    if not DEMO_FILL:
        return set()
    h = int(hashlib.sha256(f"{trip_id}:{d.isoformat()}".encode()).hexdigest(), 16)
    rnd = random.Random(h)
    days_ahead = (d - now().date()).days
    ratio = rnd.uniform(0.15, 0.45) + (0.25 if days_ahead <= 2 else 0) + (0.2 if is_festive(d) else 0)
    return set(rnd.sample(range(1, capacity + 1), k=min(capacity - 2, int(capacity * ratio))))


def taken_seats(conn, trip, d: date) -> set[int]:
    rows = conn.execute("SELECT seat FROM ticket_seats WHERE trip_id=? AND travel_date=? AND active=1",
                        (trip["id"], d.isoformat()))
    return {r[0] for r in rows} | demo_taken(trip["id"], d, BUS_CLASSES[trip["bus_class"]].seats)


def search(origin: str, destination: str, d: date) -> list[dict]:
    with DB.connect() as conn:
        expire_holds(conn)
        trips = conn.execute("SELECT t.*, o.name AS operator, o.rating FROM trips t JOIN operators o "
                             "ON o.id = t.operator_id WHERE t.origin=? AND t.destination=? AND t.active=1 "
                             "AND o.active=1 ORDER BY t.depart", (origin, destination)).fetchall()
        out = []
        for t in trips:
            if not bookable(t, d):
                continue
            bc = BUS_CLASSES[t["bus_class"]]
            dep, arr = departure_dt(t, d), arrival_dt(t, d)
            out.append({
                "id": t["id"], "operator": t["operator"], "rating": t["rating"], "bus_class": t["bus_class"],
                "class_name": bc.name, "amenities": bc.amenities.split(","), "depart": t["depart"],
                "arrive": arr.strftime("%H:%M"), "arrive_day_offset": (arr.date() - dep.date()).days,
                "duration_min": t["duration_min"], "fare": seat_fare(t, d),
                "seats_left": bc.seats - len(taken_seats(conn, t, d)), "capacity": bc.seats,
                "slot": _slot(t["depart"]),
            })
    return out


def _slot(hhmm: str) -> str:
    h = int(hhmm[:2])
    return "madrugada" if h < 6 else "manha" if h < 12 else "tarde" if h < 18 else "noite"


def routes_from(origin: str) -> list[str]:
    with DB.connect() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT destination FROM trips WHERE origin=? AND active=1 "
                                           "ORDER BY destination", (origin,))]


def cheapest(origin: str, destination: str) -> int | None:
    with DB.connect() as conn:
        r = conn.execute("SELECT MIN(fare) FROM trips WHERE origin=? AND destination=? AND active=1",
                         (origin, destination)).fetchone()
    return r[0]


# ------------------------------------------------------------ hold & sell --

def new_pnr() -> str:
    return short_code("VM")


def hold(trip_id: int, d: date, seats: list[int], boarding: str, dropping: str) -> str:
    """Reserve seats for HOLD_MINUTES. Raises BookingError with a customer-facing message."""
    seats = sorted(set(seats))
    if not seats:
        raise BookingError("Escolha pelo menos um lugar.")
    if len(seats) > MAX_SEATS_PER_TICKET:
        raise BookingError(f"Máximo de {MAX_SEATS_PER_TICKET} lugares por bilhete.")
    with DB.connect() as conn:
        expire_holds(conn)
        trip = get_trip(conn, trip_id)
        if not trip or not bookable(trip, d):
            raise BookingError("Esta viagem já não está à venda para essa data.")
        bc = BUS_CLASSES[trip["bus_class"]]
        if any(s < 1 or s > bc.seats for s in seats):
            raise BookingError("Lugar inválido.")
        if boarding not in points_for(trip["origin"]) or dropping not in points_for(trip["destination"]):
            raise BookingError("Escolha o ponto de embarque e de desembarque.")
        clash = set(seats) & demo_taken(trip_id, d, bc.seats)
        if clash:
            raise BookingError(f"O lugar {min(clash)} acabou de ser vendido. Escolha outro.")
        fare = seat_fare(trip, d)
        pnr = new_pnr()
        conn.execute("INSERT INTO tickets (pnr, trip_id, travel_date, boarding, dropping, fare_total, amount, status,"
                     " hold_expires, created_at) VALUES (?,?,?,?,?,?,?, 'reservado', ?, ?)",
                     (pnr, trip_id, d.isoformat(), boarding, dropping, fare * len(seats), fare * len(seats),
                      int(time.time()) + HOLD_MINUTES * 60, now_iso()))
        try:
            for s in seats:
                conn.execute("INSERT INTO ticket_seats (pnr, trip_id, travel_date, seat, active) VALUES (?,?,?,?,1)",
                             (pnr, trip_id, d.isoformat(), s))
        except sqlite3.IntegrityError:
            conn.rollback()
            raise BookingError("Um dos lugares acabou de ser reservado por outra pessoa. Escolha outro.")
    return pnr


def apply_promo(code: str, fare_total: int) -> tuple[int, str | None]:
    p = PROMOS.get((code or "").strip().upper())
    if not p:
        return 0, None
    return min(p["max"], round(fare_total * p["pct"] / 100)), code.strip().upper()


def load(pnr: str):
    with DB.connect() as conn:
        expire_holds(conn)
        t = conn.execute("SELECT * FROM tickets WHERE pnr=?", (pnr,)).fetchone()
        if not t:
            return None, None, []
        trip = get_trip(conn, t["trip_id"])
        seats = conn.execute("SELECT * FROM ticket_seats WHERE pnr=? ORDER BY seat", (pnr,)).fetchall()
    return t, trip, seats


def refund_quote(trip, travel_date: date, amount: int) -> tuple[int, int, str]:
    """(refund MT, percent, rule text) if cancelled now."""
    hours = (departure_dt(trip, travel_date) - now()).total_seconds() / 3600
    if hours <= 0:
        return 0, 0, "A viagem já partiu."
    if hours >= 24:
        return round(amount * 0.9), 90, "Mais de 24h antes: reembolso de 90%."
    if hours >= 6:
        return round(amount * 0.5), 50, "Entre 6h e 24h antes: reembolso de 50%."
    return 0, 0, "Menos de 6h antes da partida: sem reembolso."


def cancel(pnr: str) -> int:
    with DB.connect() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE pnr=?", (pnr,)).fetchone()
        if not t or t["status"] not in ("confirmado", "reservado"):
            raise BookingError("Este bilhete não pode ser cancelado.")
        trip = get_trip(conn, t["trip_id"])
        d = date.fromisoformat(t["travel_date"])
        if departure_dt(trip, d) <= now():
            raise BookingError("A viagem já partiu.")
        refund = refund_quote(trip, d, t["amount"])[0] if t["status"] == "confirmado" else 0
        conn.execute("UPDATE ticket_seats SET active=NULL WHERE pnr=?", (pnr,))
        conn.execute("UPDATE tickets SET status='cancelado', refund=? WHERE pnr=?", (refund, pnr))
    return refund


def tracking(trip, d: date) -> dict:
    """Schedule-based trip status (no GPS yet)."""
    dep, arr, t = departure_dt(trip, d), arrival_dt(trip, d), now()
    if t < dep:
        mins = int((dep - t).total_seconds() // 60)
        when = f"{mins // 60}h {mins % 60:02d}min" if mins < 48 * 60 else f"{mins // 1440} dias"
        return {"state": "programado", "pct": 0, "text": f"Parte daqui a {when}."}
    if t < arr:
        pct = int((t - dep) / (arr - dep) * 100)
        left = int((arr - t).total_seconds() // 60)
        return {"state": "em_viagem", "pct": pct,
                "text": f"Em viagem · chegada prevista às {arr.strftime('%H:%M')} (faltam {left // 60}h {left % 60:02d}min)."}
    return {"state": "chegou", "pct": 100, "text": f"Chegada prevista às {arr.strftime('%H:%M')} de {arr.strftime('%d/%m')}."}


def manifest(trip_id: int, d: date) -> list:
    with DB.connect() as conn:
        return conn.execute(
            "SELECT s.seat, s.passenger, s.gender, s.age, s.doc, t.pnr, t.phone, t.boarding, t.dropping, t.boarded_at "
            "FROM ticket_seats s JOIN tickets t ON t.pnr = s.pnr "
            "WHERE s.trip_id=? AND s.travel_date=? AND s.active=1 AND t.status='confirmado' ORDER BY s.seat",
            (trip_id, d.isoformat())).fetchall()
