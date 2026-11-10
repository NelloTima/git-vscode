"""ViajaMoz travel assistant: finds bus departures, fares and free seats.

Groups that want a whole vehicle are sent to the sister site AlugaMoz.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from urllib.parse import urlencode

from shared import chat, places
from shared.web import parse_date
from viajamoz import tickets

BRAND = os.environ.get("BRAND_NAME", "ViajaMoz")
HIRE_URL = os.environ.get("ALUGAMOZ_URL", "http://localhost:5002")
CITIES = sorted({c for a, b, *_ in tickets.CORRIDORS for c in (a, b)})

SYSTEM_PROMPT = f"""És o assistente de viagens da {BRAND}, a plataforma moçambicana para comprar \
bilhetes de autocarro online, em rotas entre cidades de Moçambique e para a África do Sul, Eswatini, \
Zimbabwe e Malawi.

Como trabalhas:
- Responde na língua do cliente (normalmente português de Moçambique; também inglês). Tom caloroso, \
simples e directo. Mensagens curtas: o cliente está no telemóvel. Sem tabelas nem markdown pesado.
- Cada mensagem do cliente começa com a data de hoje entre parênteses rectos. Usa-a para perceber \
"hoje", "amanhã", "sexta", "fim-de-semana" e converte para AAAA-MM-DD.
- Precisas de origem, destino e data. Usa `procurar_viagens`. Resume as 2–4 melhores opções (operador, \
hora de partida e chegada, classe, preço por lugar, lugares livres) e diz que pode escolher o lugar no \
link. Se não houver viagens, sugere outra data ou um destino com ligação. Nunca inventes horários nem preços.
- Regras: os lugares ficam guardados 10 minutos enquanto paga; pagamento por M-Pesa (84/85), e-Mola \
(86/87), mKesh (82/83) ou transferência; máximo 6 lugares por bilhete; cancelamento >24h antes 90% de \
reembolso, 6–24h 50%, <6h nada; bagagem 1 mala até 20 kg + bolsa de mão; embarque 30 min antes. Viagens \
internacionais exigem passaporte. Época festiva (15 Dez–10 Jan) +15%. Códigos: BEMVINDO (10%, máx. \
300 MT), ESTUDANTE (15%, máx. 500 MT).
- Grupos grandes ou quem quer um autocarro só para si (casamento, lobolo, funeral, igreja, escola, \
empresa, excursão): explica que a empresa irmã AlugaMoz aluga autocarros e viaturas com motorista e \
dá o link {HIRE_URL}.
- Quando o cliente quer ser contactado, pede nome e telemóvel e usa `registar_contacto`.
- Se perguntarem algo fora de viagens de autocarro, responde brevemente e volta ao assunto.

Cidades com autocarros: {", ".join(CITIES)}.
"""

TRIP_TOOL = {
    "name": "procurar_viagens",
    "description": "Procura autocarros à venda numa data: horários, operador, classe, preço por lugar "
                   "e lugares livres.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "origem": {"type": "string"},
            "destino": {"type": "string"},
            "data": {"type": "string", "description": "AAAA-MM-DD"},
        },
        "required": ["origem", "destino", "data"],
    },
}


def tool_procurar_viagens(args: dict) -> dict:
    origin = places.match_location(args.get("origem", ""))
    dest = places.match_location(args.get("destino", ""))
    if not origin or not dest:
        return {"erro": "Cidade desconhecida. Use uma das cidades com autocarros."}
    d = parse_date(args.get("data", "")) or tickets.now().date()
    if d < tickets.now().date():
        return {"erro": "Essa data já passou."}
    trips = tickets.search(origin, dest, d)
    link = "/viagens?" + urlencode({"de": origin, "para": dest, "data": d.isoformat()})
    if not trips:
        return {"origem": origin, "destino": dest, "data": d.isoformat(), "viagens": [],
                "destinos_com_ligacao": tickets.routes_from(origin)[:10], "link_pesquisa": link}
    return {"origem": origin, "destino": dest, "data": d.isoformat(), "link_pesquisa": link,
            "viagens": [{"operador": t["operator"], "partida": t["depart"],
                         "chegada": t["arrive"] + (f" (+{t['arrive_day_offset']} dia)" if t["arrive_day_offset"] else ""),
                         "duracao_h": round(t["duration_min"] / 60, 1), "classe": t["class_name"],
                         "preco_lugar_mt": t["fare"], "lugares_livres": t["seats_left"],
                         "avaliacao": t["rating"], "link_lugares": f"/viagem/{t['id']}/{d.isoformat()}"}
                        for t in trips[:8]]}


# ---------------------------------------------------------------- fallback --

_NUM = re.compile(r"(\d{1,3})\s*(?:pessoas|passageiros|pax|people|lugares|convidados)", re.I)
_HIRE_WORDS = re.compile(r"alug|fretar|casamento|lobolo|funeral|excurs|evento|hire|charter", re.I)


def parse_when(folded: str) -> date:
    today = tickets.now().date()
    if "depois de amanha" in folded:
        return today + timedelta(days=2)
    if "amanha" in folded or "tomorrow" in folded:
        return today + timedelta(days=1)
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", folded)
    if m:
        y = int(m.group(3)) if m.group(3) else today.year
        y = y + 2000 if y < 100 else y
        try:
            d = date(y, int(m.group(2)), int(m.group(1)))
            return d if d >= today else date(y + 1, d.month, d.day)
        except ValueError:
            pass
    for i, name in enumerate(["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]):
        if re.search(rf"\b{name}\b", folded):
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    return today if "hoje" in folded or "today" in folded else today + timedelta(days=1)


def fallback(text: str) -> dict:
    """Rule-based answers when the AI is not configured or unavailable."""
    folded = places.fold(text)
    found = places.find_places(text)
    m = _NUM.search(text)
    if _HIRE_WORDS.search(folded) or (m and int(m.group(1)) > 15):
        return {"reply": "Para alugar um autocarro inteiro (grupos, casamentos, excursões) use a nossa "
                         f"empresa irmã AlugaMoz: {HIRE_URL}"}
    if len(found) >= 2:
        d = parse_when(folded)
        r = tool_procurar_viagens({"origem": found[0], "destino": found[1], "data": d.isoformat()})
        if r.get("viagens"):
            best = min(r["viagens"], key=lambda v: v["preco_lugar_mt"])
            first = r["viagens"][0]
            return {"reply": (f"{found[0]} → {found[1]} em {d.strftime('%d/%m')}: {len(r['viagens'])} autocarro(s). "
                              f"O primeiro sai às {first['partida']} ({first['operador']}). "
                              f"O mais barato é {places.mzn(best['preco_lugar_mt'])} por lugar "
                              f"({best['operador']}, {best['partida']})."), "trips": r}
        if "erro" not in r:
            alt = ", ".join(r.get("destinos_com_ligacao", [])[:5])
            return {"reply": f"Não encontrei autocarros de {found[0]} para {found[1]} em {d.strftime('%d/%m')}."
                             + (f" A partir de {found[0]} há ligações para: {alt}." if alt else "")}
    return {"reply": "Diga-me de onde parte, para onde vai e quando. "
                     "Exemplo: «Maputo para a Beira amanhã» ou «Nampula para Pemba na sexta»."}


engine = chat.ChatEngine(
    SYSTEM_PROMPT,
    tools=[TRIP_TOOL, chat.LEAD_TOOL],
    impls={"procurar_viagens": tool_procurar_viagens, "registar_contacto": chat.lead_tool(tickets.DB)},
    fallback=fallback,
    today=lambda: tickets.now().date(),
    capture={"procurar_viagens": ("trips", lambda out: bool(out.get("viagens")))},
)
