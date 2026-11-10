"""Vehicle categories and the hire quote engine for AlugaMoz.

All prices are in Meticais (MZN) and include IVA (16%) in the final total.
Distances are estimated from coordinates (great-circle x road factor), so any
pair of locations can be quoted without maintaining a distance table.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date

from shared.places import (LOCATIONS, ROAD_FACTOR, distance_km, fold, haversine_km,  # noqa: F401
                           is_cross_border, match_location, mzn)

_fold = fold

IVA_RATE = 0.16
DRIVER_NIGHT_ALLOWANCE = 1500  # subsídio de pernoita do motorista, per night
CROSS_BORDER_FEE = 8500        # documentação, seguro e taxas fronteiriças
KM_PER_DAY = 550               # max distance a driver should cover in one day
DEPOSIT_RATE = 0.30            # sinal para confirmar a reserva
SHORT_TRIP_KM = 40             # one-way distance treated as an urban transfer
SHORT_TRIP_MIN_RATE = 0.40     # urban transfer minimum = 40% of the day minimum

# Cities where partners have bases (vehicles start from here).
HUB_CITIES = ["Maputo", "Matola", "Xai-Xai", "Inhambane", "Beira", "Chimoio",
              "Tete", "Quelimane", "Nampula", "Pemba", "Lichinga"]


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    seats: int
    per_km: int       # MZN per km
    per_hour: int     # MZN per hour (aluguer local)
    day_min: int      # minimum charge per day
    examples: str
    ideal_for: str


CATEGORIES: dict[str, Category] = {c.slug: c for c in [
    Category("executivo", "Carro Executivo", 4, 45, 900, 3500,
             "Toyota Corolla, Mercedes C-Class",
             "Transfer de aeroporto, reuniões, viagens de negócios"),
    Category("suv", "SUV / 4x4", 6, 70, 1400, 6500,
             "Toyota Prado, Fortuner, Land Cruiser",
             "Estradas de terra, turismo, projectos no terreno"),
    Category("pickup", "Pickup 4x4 Cabine Dupla", 4, 60, 1200, 5500,
             "Toyota Hilux, Ford Ranger, Isuzu D-Max",
             "Obras, ONGs, mineração, zonas rurais"),
    Category("minibus", "Minibus 15 lugares", 15, 75, 1500, 7500,
             "Toyota Quantum / Hiace",
             "Família, igreja, lobolo, pequenas excursões"),
    Category("coaster", "Mini-autocarro 26–30 lugares", 30, 110, 2200, 12000,
             "Toyota Coaster, Higer, King Long",
             "Casamentos, escolas, equipas de empresa"),
    Category("autocarro", "Autocarro 50–65 lugares", 65, 160, 3200, 20000,
             "Marcopolo, Yutong, Scania",
             "Grandes excursões, eventos, transporte de trabalhadores"),
    Category("luxo", "Carro de Luxo / Casamento", 4, 95, 3500, 14000,
             "Mercedes E/S-Class, BMW Série 5, clássicos decorados",
             "Noivos, lobolo, eventos VIP"),
]}

TRIP_TYPES = {
    "ida": "Só ida",
    "ida_volta": "Ida e volta",
    "local": "Aluguer local (por horas)",
}

OCCASIONS = ["Casamento", "Lobolo", "Funeral", "Igreja / Culto", "Excursão escolar",
             "Transporte de trabalhadores", "Turismo / Férias", "Transfer de aeroporto",
             "Evento desportivo", "Outro"]


def recommend_category(passengers: int) -> str:
    for slug in ("executivo", "suv", "minibus", "coaster", "autocarro"):
        if passengers <= CATEGORIES[slug].seats:
            return slug
    return "autocarro"


def vehicles_needed(category: str, passengers: int) -> int:
    return max(1, math.ceil(passengers / CATEGORIES[category].seats))


@dataclass
class Quote:
    origin: str
    destination: str
    trip_type: str
    category: str
    category_name: str
    passengers: int
    vehicles: int
    km: int
    days: int
    hours: int
    base: int
    driver_allowance: int
    border_fee: int
    subtotal: int
    iva: int
    total: int
    deposit: int
    notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def quote(origin: str, destination: str, trip_type: str, category: str | None,
          passengers: int, start: date | None = None, end: date | None = None,
          hours: int = 0) -> Quote:
    """Return a price estimate. Raises ValueError on bad input."""
    if origin not in LOCATIONS:
        raise ValueError(f"Origem desconhecida: {origin}")
    if trip_type not in TRIP_TYPES:
        raise ValueError(f"Tipo de viagem inválido: {trip_type}")
    if trip_type != "local" and destination not in LOCATIONS:
        raise ValueError(f"Destino desconhecido: {destination}")
    passengers = max(1, int(passengers))
    category = category if category in CATEGORIES else recommend_category(passengers)
    cat = CATEGORIES[category]
    n_vehicles = vehicles_needed(category, passengers)
    notes: list[str] = []

    if trip_type == "local":
        destination = destination if destination in LOCATIONS else origin
        hours = max(4, int(hours or 4))
        days = max(1, math.ceil(hours / 10))
        km = 0
        per_vehicle = max(cat.per_hour * hours, cat.day_min * days if hours > 10 else 0)
        nights = 0
        notes.append(f"Aluguer local de {hours}h (mínimo 4h), até 80 km por dia incluídos.")
    else:
        one_way = distance_km(origin, destination)
        km = one_way * 2 if trip_type == "ida_volta" else one_way
        driving_days = max(1, math.ceil(km / KM_PER_DAY))
        booked_days = 1
        if start and end and end >= start:
            booked_days = (end - start).days + 1
        days = max(driving_days, booked_days)
        if trip_type == "ida":
            # vehicle must return empty: charge 50% of the return leg
            km_charged = km + one_way * 0.5
            notes.append("Só ida inclui 50% do regresso do veículo vazio.")
        else:
            km_charged = km
        # Short urban trips (airport transfers, Maputo<->Matola) use a lower minimum.
        minimum = round(cat.day_min * SHORT_TRIP_MIN_RATE) if one_way <= SHORT_TRIP_KM else cat.day_min * days
        per_vehicle = max(round(km_charged * cat.per_km), minimum)
        nights = days - 1
        if km > KM_PER_DAY and trip_type == "ida_volta" and booked_days < driving_days:
            notes.append(f"Viagem longa: estimámos {driving_days} dia(s) de condução.")

    base = per_vehicle * n_vehicles
    allowance = DRIVER_NIGHT_ALLOWANCE * nights * n_vehicles
    border = CROSS_BORDER_FEE * n_vehicles if trip_type != "local" and is_cross_border(origin, destination) else 0
    if border:
        notes.append("Viagem internacional: passaportes válidos e lista de passageiros 72h antes.")
    if n_vehicles > 1:
        notes.append(f"{passengers} passageiros: precisa de {n_vehicles} veículos da categoria {cat.name}.")
    subtotal = base + allowance + border
    iva = round(subtotal * IVA_RATE)
    total = subtotal + iva
    deposit = round(total * DEPOSIT_RATE)
    notes.append("Combustível, portagens e motorista incluídos.")

    return Quote(origin, destination, trip_type, category, cat.name, passengers, n_vehicles,
                 km, days, hours if trip_type == "local" else 0, base, allowance, border,
                 subtotal, iva, total, deposit, notes)
