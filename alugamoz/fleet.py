"""AlugaMoz fleet: database, partner vehicles and fleet search."""
from __future__ import annotations

import os
import random
import sqlite3

from shared import places
from shared.storage import Database, now, short_code

DB = Database(os.environ.get("ALUGAMOZ_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "alugamoz.db")))
MAX_PICKUP_KM = 250  # furthest a partner vehicle travels to start a job

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER REFERENCES partners(id),
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    plate TEXT,
    city TEXT NOT NULL,
    seats INTEGER NOT NULL,
    year INTEGER,
    features TEXT,
    rating REAL DEFAULT 4.5,
    trips INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT UNIQUE NOT NULL,
    vehicle_id INTEGER REFERENCES vehicles(id),
    category TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    trip_type TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    pickup_time TEXT,
    pickup_address TEXT,
    passengers INTEGER NOT NULL,
    vehicles INTEGER NOT NULL DEFAULT 1,
    occasion TEXT,
    notes TEXT,
    km INTEGER,
    total INTEGER NOT NULL,
    deposit INTEGER NOT NULL,
    payment_method TEXT,
    payment_status TEXT NOT NULL DEFAULT 'pendente',
    status TEXT NOT NULL DEFAULT 'pendente',
    source TEXT DEFAULT 'web',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vehicles_city ON vehicles(city, category);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
"""


def new_ref() -> str:
    return short_code("AL-")


def init(seed: bool = True) -> None:
    DB.init(SCHEMA)
    with DB.connect() as conn:
        if seed and conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0:
            _seed(conn)


def search_fleet(city: str, passengers: int, category: str | None = None, limit: int = 30) -> list:
    """Vehicles in the city first, then the nearest hubs (sorted by distance)."""
    q = "SELECT v.*, p.company FROM vehicles v LEFT JOIN partners p ON p.id = v.partner_id WHERE v.active = 1"
    params: list = []
    if category:
        q += " AND v.category = ?"
        params.append(category)
    elif passengers:
        q += " AND v.seats >= ?"
        params.append(min(passengers, 50))  # bigger groups get several vehicles
    with DB.connect() as conn:
        rows = [dict(r) for r in conn.execute(q, params)]
    origin = places.LOCATIONS.get(city, places.LOCATIONS["Maputo"])
    for r in rows:
        loc = places.LOCATIONS.get(r["city"])
        r["distance_km"] = round(places.haversine_km(origin[:2], loc[:2]) * places.ROAD_FACTOR) if loc else 9999
    rows.sort(key=lambda r: (r["distance_km"] > 60, r["distance_km"], r["seats"], -r["rating"]))
    # Only offer vehicles that can realistically reach the client; fall back to the nearest hub.
    near = [r for r in rows if r["distance_km"] <= MAX_PICKUP_KM]
    if not near and rows:
        nearest = rows[0]["city"]
        near = [r for r in rows if r["city"] == nearest]
    return near[:limit]


def _seed(conn: sqlite3.Connection) -> None:
    partners = [
        ("Transportes Maputo Express", "Arlindo Cossa", "+258841234567", "Maputo", 18, 4.8),
        ("Matola Tours & Eventos", "Célia Macuácua", "+258861112223", "Matola", 9, 4.7),
        ("Beira Viagens Lda", "Jorge Sitoe", "+258823334445", "Beira", 12, 4.6),
        ("Norte Transportes Nampula", "Fátima Abdala", "+258875556667", "Nampula", 10, 4.7),
        ("Tete Mining Logistics", "Paulo Nhantumbo", "+258847778889", "Tete", 15, 4.5),
        ("Inhambane Coast Shuttles", "Rosa Mondlane", "+258852223334", "Inhambane", 6, 4.9),
        ("Pemba Safaris & Transfers", "Abdul Momade", "+258869990001", "Pemba", 7, 4.6),
        ("Chimoio Rent", "Ernesto Chiziane", "+258824445556", "Chimoio", 5, 4.4),
        ("Quelimane Mobilidade", "Luísa Tembe", "+258846667778", "Quelimane", 5, 4.5),
    ]
    for p in partners:
        conn.execute(
            "INSERT INTO partners (company, contact_name, phone, city, fleet_size, rating, status, created_at)"
            " VALUES (?,?,?,?,?,?, 'aprovado', ?)", (*p, now()))
    pid = {row["city"]: row["id"] for row in conn.execute("SELECT id, city FROM partners")}

    V = [
        # partner city, category, title, seats, year, features
        ("Maputo", "executivo", "Toyota Corolla Executivo", 4, 2022, "Ar condicionado,Wi-Fi,Água a bordo"),
        ("Maputo", "luxo", "Mercedes-Benz Classe E (Casamento)", 4, 2021, "Decoração floral,Motorista de fato,Champanhe"),
        ("Maputo", "suv", "Toyota Land Cruiser Prado", 6, 2020, "4x4,Ar condicionado,Bagageira grande"),
        ("Maputo", "minibus", "Toyota Quantum 15 lugares", 15, 2021, "Ar condicionado,Bancos reclináveis,Carregadores USB"),
        ("Maputo", "coaster", "Toyota Coaster 30 lugares", 30, 2019, "Ar condicionado,Microfone,Porta-bagagens"),
        ("Maputo", "autocarro", "Marcopolo Paradiso 60 lugares", 60, 2018, "Ar condicionado,WC,TV,Sistema de som"),
        ("Matola", "minibus", "Toyota Hiace GL 14 lugares", 14, 2020, "Ar condicionado,Sistema de som"),
        ("Matola", "autocarro", "Yutong ZK6122 55 lugares", 55, 2022, "Ar condicionado,Wi-Fi,Tomadas USB"),
        ("Matola", "luxo", "BMW Série 5 Branco (Noivos)", 4, 2020, "Fitas e flores,Motorista elegante"),
        ("Beira", "minibus", "Toyota Quantum Beira 15 lugares", 15, 2019, "Ar condicionado"),
        ("Beira", "coaster", "King Long 29 lugares", 29, 2020, "Ar condicionado,Microfone"),
        ("Beira", "suv", "Toyota Fortuner 4x4", 6, 2021, "4x4,Ideal para Gorongosa"),
        ("Chimoio", "pickup", "Toyota Hilux Cabine Dupla", 4, 2021, "4x4,Caixa de carga"),
        ("Tete", "pickup", "Ford Ranger 4x4 (Mineração)", 4, 2022, "4x4,Kit de segurança mineira,Rádio"),
        ("Tete", "autocarro", "Scania Touring 50 lugares (Staff)", 50, 2019, "Ar condicionado,Cintos de segurança"),
        ("Quelimane", "minibus", "Toyota Hiace Quelimane", 15, 2018, "Ar condicionado"),
        ("Nampula", "minibus", "Toyota Quantum Nampula", 15, 2020, "Ar condicionado,Bancos reclináveis"),
        ("Nampula", "coaster", "Toyota Coaster Nampula 30 lugares", 30, 2018, "Ar condicionado"),
        ("Nampula", "suv", "Toyota Land Cruiser 4x4 (Ilha)", 6, 2019, "4x4,Ideal para Ilha de Moçambique"),
        ("Inhambane", "minibus", "Shuttle Praia do Tofo 12 lugares", 12, 2021, "Ar condicionado,Suporte para pranchas"),
        ("Inhambane", "suv", "Toyota Prado Tofo/Vilankulo", 6, 2020, "4x4,Areia"),
        ("Pemba", "suv", "Land Cruiser Pemba Safaris", 6, 2020, "4x4,Geleira"),
        ("Pemba", "executivo", "Toyota Corolla Pemba Aeroporto", 4, 2021, "Ar condicionado"),
    ]
    rnd = random.Random(258)
    for city, cat, title, seats, year, feats in V:
        plate = f"A{rnd.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{rnd.choice('ABCDEFGHJKLMNPRSTUVWXYZ')} {rnd.randint(100, 999)} {city[:2].upper()}"
        conn.execute(
            "INSERT INTO vehicles (partner_id, category, title, plate, city, seats, year, features, rating, trips)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid[city], cat, title, plate, city, seats, year, feats,
             round(rnd.uniform(4.3, 5.0), 1), rnd.randint(12, 340)))
