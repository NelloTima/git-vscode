"""AlugaMoz — aluguer de autocarros, minibuses e viaturas com motorista em Moçambique."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for

from alugamoz import assistant, fleet, pricing
from shared import payments, places
from shared.storage import now as now_iso
from shared.web import admin_required, create_app, parse_date

DB = fleet.DB
TZ = ZoneInfo("Africa/Maputo")

BRAND = {
    "name": os.environ.get("BRAND_NAME", "AlugaMoz"),
    "tagline": "Aluguer de autocarros e viaturas com motorista em todo Moçambique",
    "color": "#1d4e89",
    "whatsapp": os.environ.get("WHATSAPP_NUMBER", "258840000000"),
    "whatsapp_text": "Olá, quero alugar um transporte",
    "phone": os.environ.get("SUPPORT_PHONE", "+258 84 000 0000"),
    "email": os.environ.get("SUPPORT_EMAIL", "reservas@alugamoz.co.mz"),
    "sister_name": "ViajaMoz",
    "sister_url": os.environ.get("VIAJAMOZ_URL", "http://localhost:5001"),
    "sister_pitch": "Viaja sozinho ou em pequeno grupo? Compre bilhetes de autocarro na",
}


def today() -> date:
    return datetime.now(TZ).date()


app = create_app(__name__, brand=BRAND, cookie="alugamoz_session", today=today)

BOOKING_STATUSES = ["pendente", "confirmada", "motorista_atribuido", "em_viagem", "concluida", "cancelada"]
STATUS_LABELS = {"pendente": "Pendente", "confirmada": "Confirmada", "motorista_atribuido": "Motorista atribuído",
                 "em_viagem": "Em viagem", "concluida": "Concluída", "cancelada": "Cancelada"}


@app.context_processor
def _ctx():
    return {"categories": pricing.CATEGORIES, "locations": list(places.LOCATIONS), "trip_types": pricing.TRIP_TYPES,
            "occasions": pricing.OCCASIONS, "status_labels": STATUS_LABELS}


POPULAR_ROUTES = [
    ("Maputo", "Bilene", "Fim-de-semana na lagoa", 30),
    ("Maputo", "Ponta do Ouro", "Praia e mergulho", 15),
    ("Maputo", "Tofo", "Férias na praia", 15),
    ("Maputo", "Nelspruit (África do Sul)", "Compras e turismo", 15),
    ("Beira", "Parque da Gorongosa", "Safari", 6),
    ("Nampula", "Ilha de Moçambique", "Património mundial", 15),
    ("Tete", "Tete", "Transporte de trabalhadores", 50),
    ("Maputo", "Xai-Xai", "Casamento / lobolo em família", 60),
]


@app.get("/")
def home():
    routes = []
    for o, d, label, pax in POPULAR_ROUTES:
        tipo = "local" if o == d else "ida_volta"
        q = pricing.quote(o, d, tipo, None, pax, hours=10 if tipo == "local" else 0)
        routes.append({"origin": o, "destination": d, "label": label, "pax": pax, "tipo": tipo, "q": q})
    with DB.connect() as conn:
        stats = {
            "vehicles": conn.execute("SELECT COUNT(*) FROM vehicles WHERE active=1").fetchone()[0],
            "partners": conn.execute("SELECT COUNT(*) FROM partners WHERE status='aprovado'").fetchone()[0],
            "cities": len({r[0] for r in conn.execute("SELECT city FROM vehicles")}),
        }
    return render_template("index.html", routes=routes, stats=stats)


def _search_params():
    a = request.args
    origin = places.match_location(a.get("origem", "")) or "Maputo"
    tipo = a.get("tipo") if a.get("tipo") in pricing.TRIP_TYPES else "ida_volta"
    dest = places.match_location(a.get("destino", "")) or (origin if tipo == "local" else None)
    try:
        pax = max(1, min(500, int(a.get("passageiros") or 1)))
    except ValueError:
        pax = 1
    try:
        hours = max(4, min(72, int(a.get("horas") or 8)))
    except ValueError:
        hours = 8
    start = parse_date(a.get("data")) or today() + timedelta(days=3)
    end = parse_date(a.get("data_fim"))
    if end and end < start:
        end = None
    cat = a.get("categoria") if a.get("categoria") in pricing.CATEGORIES else None
    return {"origin": origin, "destination": dest, "tipo": tipo, "pax": pax, "hours": hours,
            "start": start, "end": end, "category": cat, "occasion": a.get("ocasiao", "")}


@app.get("/pesquisa")
def search():
    p = _search_params()
    if not p["destination"]:
        flash("Escolha um destino da lista.", "erro")
        return redirect(url_for("home"))
    rows = fleet.search_fleet(p["origin"], p["pax"] if not p["category"] else 0, p["category"])
    results = []
    for v in rows:
        n = pricing.vehicles_needed(v["category"], p["pax"]) if v["seats"] < p["pax"] else 1
        if n > 4:
            continue
        q = pricing.quote(p["origin"], p["destination"], p["tipo"], v["category"], p["pax"],
                          p["start"], p["end"], p["hours"])
        results.append({"v": v, "q": q})
    results.sort(key=lambda r: (r["v"]["distance_km"] > 60, r["q"].vehicles, r["q"].total))
    return render_template("search.html", p=p, results=results, recommended=pricing.recommend_category(p["pax"]))


@app.get("/veiculo/<int:vid>")
def vehicle(vid: int):
    with DB.connect() as conn:
        v = conn.execute("SELECT v.*, p.company, p.rating AS partner_rating FROM vehicles v "
                         "LEFT JOIN partners p ON p.id = v.partner_id WHERE v.id = ? AND v.active = 1",
                         (vid,)).fetchone()
    if not v:
        abort(404)
    p = _search_params()
    if not request.args.get("origem"):
        p["origin"] = v["city"]
    if not p["destination"]:
        p["destination"] = p["origin"]
        p["tipo"] = "local"
    q = pricing.quote(p["origin"], p["destination"], p["tipo"], v["category"], p["pax"],
                      p["start"], p["end"], p["hours"])
    return render_template("vehicle.html", v=v, p=p, q=q)


def _remember(ref: str) -> None:
    session["my_bookings"] = (session.get("my_bookings", []) + [ref])[-20:]


@app.post("/reservar")
def book():
    f = request.form
    errors = []
    name = f.get("nome", "").strip()[:120]
    phone = payments.normalize_phone(f.get("telefone", ""))
    origin = places.match_location(f.get("origem", ""))
    tipo = f.get("tipo", "ida_volta")
    dest = places.match_location(f.get("destino", "")) or (origin if tipo == "local" else None)
    start = parse_date(f.get("data"))
    end = parse_date(f.get("data_fim"))
    method = f.get("pagamento", "mpesa")
    try:
        vid = int(f.get("vehicle_id", "0"))
        pax = int(f.get("passageiros", "1"))
        hours = int(f.get("horas") or 0)
    except ValueError:
        abort(400)
    if len(name) < 3:
        errors.append("Indique o seu nome completo.")
    if not phone:
        errors.append("Número de telemóvel inválido (ex.: 84 123 4567).")
    if not origin or not dest or tipo not in pricing.TRIP_TYPES:
        errors.append("Origem/destino inválidos.")
    if not start or start < today():
        errors.append("Escolha uma data de partida a partir de hoje.")
    if method not in payments.METHODS:
        errors.append("Escolha um método de pagamento.")
    with DB.connect() as conn:
        v = conn.execute("SELECT * FROM vehicles WHERE id=? AND active=1", (vid,)).fetchone()
    if not v:
        errors.append("Veículo indisponível.")
    if errors:
        for e in errors:
            flash(e, "erro")
        return redirect(request.referrer or url_for("home"))

    q = pricing.quote(origin, dest, tipo, v["category"], pax, start, end, hours)
    ref = fleet.new_ref()
    with DB.connect() as conn:
        conn.execute(
            """INSERT INTO bookings (ref, vehicle_id, category, customer_name, phone, email, origin, destination,
               trip_type, start_date, end_date, pickup_time, pickup_address, passengers, vehicles, occasion,
               notes, km, total, deposit, payment_method, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ref, vid, v["category"], name, phone, f.get("email", "").strip()[:120] or None, origin, dest,
             tipo, start.isoformat(), end.isoformat() if end else None, f.get("hora", "")[:5],
             f.get("endereco", "").strip()[:300], q.passengers, q.vehicles, f.get("ocasiao", "")[:60],
             f.get("notas", "").strip()[:1000], q.km, q.total, q.deposit, method, now_iso()))

    result = payments.charge(method, f.get("telefone_pagamento") or phone, q.deposit, ref)
    DB.record_payment(ref, method, phone, q.deposit, result)
    if result.status == "pago":
        with DB.connect() as conn:
            conn.execute("UPDATE bookings SET payment_status='sinal_pago', status='confirmada' WHERE ref=?", (ref,))
    flash(result.message, "ok" if result.ok else "erro")
    _remember(ref)
    return redirect(url_for("booking", ref=ref))


@app.get("/reserva")
def track():
    ref = request.args.get("ref", "").strip().upper()
    phone = payments.normalize_phone(request.args.get("telefone", ""))
    if ref and phone:
        with DB.connect() as conn:
            ok = conn.execute("SELECT 1 FROM bookings WHERE ref=? AND phone=?", (ref, phone)).fetchone()
        if ok:
            _remember(ref)
            return redirect(url_for("booking", ref=ref))
        flash("Não encontrámos essa reserva com esse número.", "erro")
    return render_template("track.html")


@app.get("/reserva/<ref>")
def booking(ref: str):
    if ref not in session.get("my_bookings", []) and not session.get("admin"):
        return redirect(url_for("track", ref=ref))
    with DB.connect() as conn:
        b = conn.execute("SELECT b.*, v.title, v.plate, v.seats FROM bookings b LEFT JOIN vehicles v "
                         "ON v.id = b.vehicle_id WHERE b.ref = ?", (ref,)).fetchone()
        pays = conn.execute("SELECT * FROM payments WHERE booking_ref=? ORDER BY id DESC", (ref,)).fetchall()
    if not b:
        abort(404)
    return render_template("booking.html", b=b, pays=pays, steps=BOOKING_STATUSES[:-1])


@app.post("/reserva/<ref>/pagar")
def pay_again(ref: str):
    if ref not in session.get("my_bookings", []):
        abort(403)
    method = request.form.get("pagamento", "mpesa")
    with DB.connect() as conn:
        b = conn.execute("SELECT * FROM bookings WHERE ref=?", (ref,)).fetchone()
    if not b or b["payment_status"] != "pendente":
        return redirect(url_for("booking", ref=ref))
    r = payments.charge(method, request.form.get("telefone_pagamento", b["phone"]), b["deposit"], ref)
    DB.record_payment(ref, method, b["phone"], b["deposit"], r)
    if r.status == "pago":
        with DB.connect() as conn:
            conn.execute("UPDATE bookings SET payment_status='sinal_pago', status='confirmada', payment_method=?"
                         " WHERE ref=?", (method, ref))
    flash(r.message, "ok" if r.ok else "erro")
    return redirect(url_for("booking", ref=ref))


@app.route("/parceiros", methods=["GET", "POST"])
def partners():
    if request.method == "POST":
        f = request.form
        phone = payments.normalize_phone(f.get("telefone", ""))
        company, contact = f.get("empresa", "").strip(), f.get("contacto", "").strip()
        city = places.match_location(f.get("cidade", ""))
        if not (company and contact and phone and city):
            flash("Preencha empresa, contacto, cidade e um telemóvel moçambicano válido.", "erro")
            return redirect(url_for("partners"))
        try:
            size = max(1, min(1000, int(f.get("frota") or 1)))
        except ValueError:
            size = 1
        cats = ",".join(c for c in f.getlist("categorias") if c in pricing.CATEGORIES)
        with DB.connect() as conn:
            conn.execute("INSERT INTO partners (company, contact_name, phone, email, city, nuit, fleet_size,"
                         " categories, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (company[:120], contact[:120], phone, f.get("email", "")[:120], city,
                          f.get("nuit", "")[:20], size, cats, now_iso()))
        flash("Recebemos o seu registo! A nossa equipa liga em 48h para verificar documentos e viaturas.", "ok")
        return redirect(url_for("partners"))
    return render_template("partners.html")


@app.get("/empresas")
def corporate():
    return render_template("corporate.html")


# ----------------------------------------------------------------- admin --

@app.get("/admin")
@admin_required
def admin():
    status = request.args.get("estado", "")
    with DB.connect() as conn:
        q = "SELECT b.*, v.title FROM bookings b LEFT JOIN vehicles v ON v.id=b.vehicle_id"
        args: tuple = ()
        if status in BOOKING_STATUSES:
            q += " WHERE b.status=?"
            args = (status,)
        bookings = conn.execute(q + " ORDER BY b.id DESC LIMIT 200", args).fetchall()
        kpi = conn.execute("""SELECT COUNT(*) n,
                COALESCE(SUM(CASE WHEN status!='cancelada' THEN total END),0) gmv,
                COALESCE(SUM(CASE WHEN payment_status='sinal_pago' THEN deposit END),0) paid,
                SUM(status='pendente') pending FROM bookings""").fetchone()
        partners_pending = conn.execute("SELECT * FROM partners WHERE status='pendente' ORDER BY id DESC").fetchall()
        leads = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("admin.html", bookings=bookings, kpi=kpi, partners_pending=partners_pending,
                           leads=leads, statuses=BOOKING_STATUSES, current=status)


@app.post("/admin/reserva/<ref>")
@admin_required
def admin_update(ref: str):
    st = request.form.get("estado")
    pay = request.form.get("pagamento_estado")
    with DB.connect() as conn:
        if st in BOOKING_STATUSES:
            conn.execute("UPDATE bookings SET status=? WHERE ref=?", (st, ref))
        if pay in ("pendente", "sinal_pago", "pago_total", "reembolsado"):
            conn.execute("UPDATE bookings SET payment_status=? WHERE ref=?", (pay, ref))
    return redirect(url_for("admin", estado=request.args.get("estado", "")))


@app.post("/admin/parceiro/<int:pid>")
@admin_required
def admin_partner(pid: int):
    st = request.form.get("estado")
    if st in ("aprovado", "rejeitado"):
        with DB.connect() as conn:
            conn.execute("UPDATE partners SET status=? WHERE id=?", (st, pid))
    return redirect(url_for("admin"))


# ------------------------------------------------------------------- API --

@app.post("/api/cotacao")
def api_quote():
    d = request.get_json(silent=True) or {}
    out = assistant.tool_cotacao({
        "origem": d.get("origem", ""), "destino": d.get("destino", ""), "tipo": d.get("tipo", "ida_volta"),
        "passageiros": d.get("passageiros", 1), "categoria": d.get("categoria") or "auto",
        "data_inicio": d.get("data", ""), "data_fim": d.get("data_fim", ""), "horas": d.get("horas", 0)})
    return jsonify(out), (400 if "erro" in out else 200)


@app.post("/api/assistente")
def api_chat():
    d = request.get_json(silent=True) or {}
    out = assistant.engine.chat(session.get("chat_sid"), str(d.get("mensagem", "")))
    session["chat_sid"] = out["session"]
    return jsonify({"reply": out["reply"], "quote": out.get("quote")})


fleet.init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=bool(os.environ.get("DEBUG")))
