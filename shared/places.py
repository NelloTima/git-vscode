"""Places in Mozambique and neighbouring countries, plus small formatting helpers.

Shared by ViajaMoz (bus tickets) and AlugaMoz (vehicle hire).
"""
from __future__ import annotations

import math
import unicodedata

ROAD_FACTOR = 1.30            # straight line -> road distance in Mozambique

# name -> (lat, lon, province, country)
LOCATIONS: dict[str, tuple[float, float, str, str]] = {
    "Maputo": (-25.9692, 32.5732, "Maputo Cidade", "MZ"),
    "Aeroporto de Maputo": (-25.9208, 32.5726, "Maputo Cidade", "MZ"),
    "Matola": (-25.9622, 32.4589, "Maputo", "MZ"),
    "Boane": (-26.0450, 32.3270, "Maputo", "MZ"),
    "Marracuene": (-25.7389, 32.6750, "Maputo", "MZ"),
    "Namaacha": (-25.9833, 32.0167, "Maputo", "MZ"),
    "Ponta do Ouro": (-26.8436, 32.8986, "Maputo", "MZ"),
    "Bilene": (-25.2833, 33.2333, "Gaza", "MZ"),
    "Xai-Xai": (-25.0519, 33.6442, "Gaza", "MZ"),
    "Chókwè": (-24.5333, 32.9833, "Gaza", "MZ"),
    "Maxixe": (-23.8597, 35.3472, "Inhambane", "MZ"),
    "Inhambane": (-23.8650, 35.3833, "Inhambane", "MZ"),
    "Tofo": (-23.8514, 35.5444, "Inhambane", "MZ"),
    "Vilankulo": (-21.9950, 35.3167, "Inhambane", "MZ"),
    "Beira": (-19.8436, 34.8389, "Sofala", "MZ"),
    "Parque da Gorongosa": (-18.9667, 34.3500, "Sofala", "MZ"),
    "Chimoio": (-19.1164, 33.4833, "Manica", "MZ"),
    "Tete": (-16.1564, 33.5867, "Tete", "MZ"),
    "Quelimane": (-17.8786, 36.8883, "Zambézia", "MZ"),
    "Nampula": (-15.1165, 39.2666, "Nampula", "MZ"),
    "Ilha de Moçambique": (-15.0342, 40.7358, "Nampula", "MZ"),
    "Nacala": (-14.5428, 40.6728, "Nampula", "MZ"),
    "Cuamba": (-14.8031, 36.5372, "Niassa", "MZ"),
    "Lichinga": (-13.3128, 35.2406, "Niassa", "MZ"),
    "Pemba": (-12.9740, 40.5178, "Cabo Delgado", "MZ"),
    "Nelspruit (África do Sul)": (-25.4753, 30.9694, "Mpumalanga", "ZA"),
    "Joanesburgo (África do Sul)": (-26.2041, 28.0473, "Gauteng", "ZA"),
    "Mbabane (Eswatini)": (-26.3054, 31.1367, "Hhohho", "SZ"),
    "Harare (Zimbabwe)": (-17.8292, 31.0522, "Harare", "ZW"),
    "Blantyre (Malawi)": (-15.7861, 35.0058, "Southern", "MW"),
}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def match_location(name: str) -> str | None:
    """Case/accent-tolerant lookup ("maputo", "ilha", "joburg" ...)."""
    if not name:
        return None
    n = fold(name)
    aliases = {"joburg": "Joanesburgo (África do Sul)", "johannesburg": "Joanesburgo (África do Sul)",
               "jhb": "Joanesburgo (África do Sul)", "mbombela": "Nelspruit (África do Sul)",
               "gorongosa": "Parque da Gorongosa", "aeroporto": "Aeroporto de Maputo",
               "airport": "Aeroporto de Maputo", "ilha": "Ilha de Moçambique",
               "swaziland": "Mbabane (Eswatini)", "eswatini": "Mbabane (Eswatini)"}
    if n in aliases:
        return aliases[n]
    for loc in LOCATIONS:
        if fold(loc) == n:
            return loc
    for loc in LOCATIONS:
        if fold(loc).startswith(n) or n in fold(loc):
            return loc
    return None


def fold(s: str) -> str:
    """Lower-case and strip accents: 'Chókwè' -> 'chokwe'."""
    decomposed = unicodedata.normalize("NFKD", s.strip().lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def distance_km(origin: str, destination: str) -> int:
    o, d = LOCATIONS[origin], LOCATIONS[destination]
    km = haversine_km(o[:2], d[:2]) * ROAD_FACTOR
    return max(5, round(km))


def is_cross_border(origin: str, destination: str) -> bool:
    return LOCATIONS[origin][3] != "MZ" or LOCATIONS[destination][3] != "MZ"


def mzn(value: int | float) -> str:
    """Format as Mozambican currency: 12 500,00 MT."""
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} MT"


def find_places(text: str) -> list[str]:
    """Places mentioned in free text, in the order they appear ("de Maputo para a Beira")."""
    folded = fold(text)
    hits: list[tuple[int, str]] = []
    taken: set[int] = set()
    for loc in sorted(LOCATIONS, key=len, reverse=True):  # longest names first
        key = fold(loc.split(" (")[0])
        idx = folded.find(key)
        if idx >= 0 and not taken & set(range(idx, idx + len(key))):
            hits.append((idx, loc))
            taken |= set(range(idx, idx + len(key)))
    return [loc for _, loc in sorted(hits)]
