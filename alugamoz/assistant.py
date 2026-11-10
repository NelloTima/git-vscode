"""AlugaMoz hire assistant: quotes whole-vehicle hire with driver and finds partner vehicles.

People who only need a seat or two are sent to the sister site ViajaMoz.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from shared import chat, places
from shared.web import parse_date
from alugamoz import fleet, pricing

BRAND = os.environ.get("BRAND_NAME", "AlugaMoz")
TICKETS_URL = os.environ.get("VIAJAMOZ_URL", "http://localhost:5001")
TZ = ZoneInfo("Africa/Maputo")

SYSTEM_PROMPT = f"""És o assistente de reservas da {BRAND}, a plataforma moçambicana para alugar \
autocarros, minibuses (tipo Quantum), Coasters, SUVs, pickups 4x4 e carros de luxo para casamentos, \
sempre com motorista, em todo o país e para os países vizinhos.

Como trabalhas:
- Responde na língua do cliente (normalmente português de Moçambique; também inglês). Tom caloroso, \
simples e directo, como um bom agente de viagens em Maputo. Mensagens curtas: o cliente está no telemóvel.
- Cada mensagem do cliente começa com a data de hoje entre parênteses rectos; usa-a para datas relativas.
- Para dar um preço precisas de: origem, destino (ou "aluguer local" com número de horas), data, número \
de passageiros e se é só ida ou ida e volta. Pergunta só o que falta, uma ou duas coisas de cada vez.
- Usa SEMPRE `cotacao` para preços. Nunca inventes valores. Apresenta o total em MT, diz que inclui \
motorista, combustível e IVA 16%, e que o sinal de 30% confirma a reserva.
- Usa `procurar_veiculos` para mostrar viaturas disponíveis perto da origem.
- Quem só precisa de 1 a ~5 lugares numa rota entre cidades poupa comprando bilhetes de autocarro na \
empresa irmã ViajaMoz ({TICKETS_URL}); diz isso com simpatia.
- Quando o cliente quer avançar ou ser contactado, pede nome e telemóvel (Vodacom 84/85, Movitel 86/87, \
Tmcel 82/83) e usa `registar_contacto`. Também pode reservar no site e pagar o sinal por M-Pesa, e-Mola, \
mKesh ou transferência.
- Conheces a realidade local: casamentos e lobolos, funerais, excursões escolares, igrejas, transporte \
de trabalhadores (minas de Tete, projectos de gás em Cabo Delgado), turismo (Tofo, Bilene, Ponta do Ouro, \
Vilankulo, Gorongosa, Ilha de Moçambique) e viagens à África do Sul, Eswatini, Zimbabwe e Malawi \
(passaporte e lista de passageiros 72h antes). Época alta: Dezembro–Janeiro, Páscoa e feriados.
- Não prometas disponibilidade garantida: fica confirmada depois do sinal e da confirmação do parceiro.
- Se perguntarem algo fora do aluguer de transporte, responde brevemente e volta ao assunto.

Locais conhecidos: {", ".join(places.LOCATIONS)}.
Categorias: {"; ".join(f"{c.slug} = {c.name} ({c.seats} lugares)" for c in pricing.CATEGORIES.values())}.
"""

QUOTE_TOOL = {
    "name": "cotacao",
    "description": "Calcula o preço estimado (MT, com IVA) de um aluguer com motorista.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "origem": {"type": "string", "description": "Cidade/local de partida"},
            "destino": {"type": "string", "description": "Destino; igual à origem para aluguer local"},
            "tipo": {"type": "string", "enum": list(pricing.TRIP_TYPES)},
            "passageiros": {"type": "integer"},
            "categoria": {"type": "string", "enum": [*pricing.CATEGORIES, "auto"],
                          "description": "'auto' escolhe a categoria pelo número de passageiros"},
            "data_inicio": {"type": "string", "description": "AAAA-MM-DD ou vazio"},
            "data_fim": {"type": "string", "description": "AAAA-MM-DD ou vazio"},
            "horas": {"type": "integer", "description": "Horas para aluguer local; 0 caso contrário"},
        },
        "required": ["origem", "destino", "tipo", "passageiros", "categoria", "data_inicio", "data_fim", "horas"],
    },
}

VEHICLES_TOOL = {
    "name": "procurar_veiculos",
    "description": "Lista viaturas activas dos parceiros numa cidade (ou a mais próxima) com lugares suficientes.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"cidade": {"type": "string"}, "passageiros": {"type": "integer"}},
        "required": ["cidade", "passageiros"],
    },
}


def tool_cotacao(args: dict) -> dict:
    origin = places.match_location(args.get("origem", ""))
    dest = places.match_location(args.get("destino", "")) or origin
    if not origin:
        return {"erro": f"Não conheço '{args.get('origem')}'. Locais: {', '.join(places.LOCATIONS)}"}
    if args.get("tipo") != "local" and not places.match_location(args.get("destino", "")):
        return {"erro": f"Não conheço o destino '{args.get('destino')}'. Peça um local próximo da lista."}
    cat = args.get("categoria")
    try:
        q = pricing.quote(origin, dest, args.get("tipo", "ida_volta"), None if cat == "auto" else cat,
                          int(args.get("passageiros") or 1), parse_date(args.get("data_inicio", "")),
                          parse_date(args.get("data_fim", "")), int(args.get("horas") or 0))
    except ValueError as e:
        return {"erro": str(e)}
    d = q.to_dict()
    d["total_formatado"] = places.mzn(q.total)
    d["sinal_formatado"] = places.mzn(q.deposit)
    params = {"origem": origin, "destino": dest, "tipo": q.trip_type, "passageiros": q.passengers,
              "categoria": q.category, "data": args.get("data_inicio") or "", "data_fim": args.get("data_fim") or ""}
    if q.trip_type == "local":
        params["horas"] = q.hours
    d["link_reserva"] = "/pesquisa?" + urlencode({k: v for k, v in params.items() if v != ""})
    return d


def tool_procurar_veiculos(args: dict) -> dict:
    city = places.match_location(args.get("cidade", "")) or "Maputo"
    pax = int(args.get("passageiros") or 1)
    rows = fleet.search_fleet(city, pax, limit=5)
    return {"cidade": city, "veiculos": [
        {"id": r["id"], "titulo": r["title"], "cidade": r["city"], "lugares": r["seats"],
         "categoria": pricing.CATEGORIES[r["category"]].name, "avaliacao": r["rating"],
         "link": f"/veiculo/{r['id']}"} for r in rows]}


# ---------------------------------------------------------------- fallback --

_NUM = re.compile(r"(\d{1,3})\s*(?:pessoas|passageiros|pax|people|lugares|convidados)", re.I)


def fallback(text: str) -> dict:
    """Rule-based answers when the AI is not configured or unavailable."""
    found = places.find_places(text)
    m = _NUM.search(text)
    pax = int(m.group(1)) if m else None
    if len(found) >= 2 and pax:
        tipo = "ida" if re.search(r"s[oó] ida|one way", text, re.I) else "ida_volta"
        q = tool_cotacao({"origem": found[0], "destino": found[1], "tipo": tipo, "passageiros": pax,
                          "categoria": "auto", "data_inicio": "", "data_fim": "", "horas": 0})
        if "erro" not in q:
            reply = (f"{found[0]} → {found[1]} ({pricing.TRIP_TYPES[tipo].lower()}), {pax} pessoas: "
                     f"{q['vehicles']}× {q['category_name']}, cerca de {q['km']} km. "
                     f"Total estimado {q['total_formatado']} (inclui motorista, combustível e IVA). "
                     f"Sinal de 30%: {q['sinal_formatado']}.")
            if pax <= 5:
                reply += f" Só {pax} pessoa(s)? Bilhetes de autocarro na ViajaMoz podem sair mais baratos: {TICKETS_URL}"
            return {"reply": reply, "quote": q}
    missing = []
    if len(found) < 1:
        missing.append("de onde parte")
    if len(found) < 2:
        missing.append("para onde vai")
    if not pax:
        missing.append("quantas pessoas são")
    return {"reply": "Para lhe dar um preço diga-me " + ", ".join(missing) +
                     ". Exemplo: «Maputo para Bilene, 30 pessoas, ida e volta»."}


engine = chat.ChatEngine(
    SYSTEM_PROMPT,
    tools=[QUOTE_TOOL, VEHICLES_TOOL, chat.LEAD_TOOL],
    impls={"cotacao": tool_cotacao, "procurar_veiculos": tool_procurar_veiculos,
           "registar_contacto": chat.lead_tool(fleet.DB)},
    fallback=fallback,
    today=lambda: datetime.now(TZ).date(),
    capture={"cotacao": ("quote", lambda out: "total" in out)},
)
