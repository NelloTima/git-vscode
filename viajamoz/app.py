"""ViajaMoz — bilhetes de autocarro online em Moçambique."""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

import segno
from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for

from shared import payments, places
from shared.storage import now as now_iso
from shared.web import admin_required, create_app, parse_date
from viajamoz import assistant, tickets

DB = tickets.DB

BRAND = {
    "name": os.environ.get("BRAND_NAME", "ViajaMoz"),
    "tagline": "Bilhetes de autocarro online para todo Moçambique e países vizinhos",
    "color": "#0b6e4f",
    "whatsapp": os.environ.get("WHATSAPP_NUMBER", "258840000000"),
    "whatsapp_text": "Olá, preciso de ajuda com um bilhete",
    "phone": os.environ.get("SUPPORT_PHONE", "+258 84 000 0000"),
    "email": os.environ.get("SUPPORT_EMAIL", "apoio@viajamoz.co.mz"),
    "sister_name": "AlugaMoz",
    "sister_url": os.environ.get("ALUGAMOZ_URL", "http://localhost:5002"),
    "sister_pitch": "Grupo grande? Alugue o autocarro inteiro na",
}

app = create_app(__name__, brand=BRAND, cookie="viajamoz_session", today=lambda: tickets.now().date())


@app.context_processor
def _ctx():
    return {"bus_classes": tickets.BUS_CLASSES}


POPULAR = [
    ("Maputo", "Beira"), ("Maputo", "Inhambane"), ("Maputo", "Xai-Xai"), ("Maputo", "Joanesburgo (África do Sul)"),
    ("Maputo", "Vilankulo"), ("Beira", "Chimoio"), ("Nampula", "Pemba"), ("Maputo", "Nampula"),
    ("Beira", "Tete"), ("Maputo", "Tete"), ("Nampula", "Nacala"), ("Maputo", "Ponta do Ouro"),
]


@app.get("/")
def home():
    popular = [{"o": o, "d": d, "from": tickets.cheapest(o, d)} for o, d in POPULAR]
    with DB.connect() as conn:
        ops = conn.execute("SELECT o.*, COUNT(t.id) AS n FROM operators o JOIN trips t ON t.operator_id=o.id "
                           "WHERE o.active=1 GROUP BY o.id ORDER BY o.rating DESC").fetchall()
        n_routes = conn.execute("SELECT COUNT(DISTINCT origin || destination) FROM trips WHERE active=1").fetchone()[0]
    return render_template("index.html", popular=popular, operators=ops, n_routes=n_routes,
                           origins=assistant.CITIES, promos=tickets.PROMOS,
                           tomorrow=tickets.now().date() + timedelta(days=1))


@app.get("/viagens")
def trips():
    origin = places.match_location(request.args.get("de", ""))
    dest = places.match_location(request.args.get("para", ""))
    day = parse_date(request.args.get("data")) or tickets.now().date()
    if not origin or not dest or origin == dest:
        flash("Escolha cidades de partida e de destino diferentes.", "erro")
        return redirect(url_for("home"))
    results = tickets.search(origin, dest, day)
    week = [day + timedelta(days=i) for i in range(-2, 5) if day + timedelta(days=i) >= tickets.now().date()][:6]
    alternatives = [] if results else tickets.routes_from(origin)[:8]
    return render_template("trips.html", origin=origin, dest=dest, day=day, results=results, week=week,
                           alternatives=alternatives, festive=tickets.is_festive(day))


def _departure_or_404(trip_id: int, d: str):
    day = parse_date(d)
    with DB.connect() as conn:
        trip = tickets.get_trip(conn, trip_id)
    if not trip or not day:
        abort(404)
    return trip, day


@app.get("/viagem/<int:trip_id>/<d>")
def seats(trip_id: int, d: str):
    trip, day = _departure_or_404(trip_id, d)
    if not tickets.bookable(trip, day):
        flash("Esta viagem já não está à venda para esta data.", "erro")
        return redirect(url_for("trips", de=trip["origin"], para=trip["destination"], data=day.isoformat()))
    with DB.connect() as conn:
        tickets.expire_holds(conn)
        taken = tickets.taken_seats(conn, trip, day)
    bc = tickets.BUS_CLASSES[trip["bus_class"]]
    return render_template("seats.html", trip=trip, day=day, bc=bc, taken=taken, fare=tickets.seat_fare(trip, day),
                           arrive=tickets.arrival_dt(trip, day), boarding=tickets.points_for(trip["origin"]),
                           dropping=tickets.points_for(trip["destination"]), max_seats=tickets.MAX_SEATS_PER_TICKET)


@app.post("/viagem/<int:trip_id>/<d>")
def seats_hold(trip_id: int, d: str):
    trip, day = _departure_or_404(trip_id, d)
    try:
        chosen = [int(x) for x in request.form.getlist("lugar")]
        pnr = tickets.hold(trip_id, day, chosen, request.form.get("embarque", ""), request.form.get("desembarque", ""))
    except ValueError:
        abort(400)
    except tickets.BookingError as e:
        flash(str(e), "erro")
        return redirect(url_for("seats", trip_id=trip_id, d=day.isoformat()))
    _remember(pnr)
    return redirect(url_for("checkout", pnr=pnr))


def _remember(pnr: str) -> None:
    session["my_tickets"] = (session.get("my_tickets", []) + [pnr])[-30:]


def _own(pnr: str) -> bool:
    return pnr in session.get("my_tickets", []) or bool(session.get("admin"))


@app.route("/pagamento/<pnr>", methods=["GET", "POST"])
def checkout(pnr: str):
    if not _own(pnr):
        abort(403)
    t, trip, seat_rows = tickets.load(pnr)
    if not t:
        abort(404)
    if t["status"] == "confirmado":
        return redirect(url_for("ticket", pnr=pnr))
    if t["status"] != "reservado":
        flash("O tempo para pagar terminou e os lugares foram libertados. Escolha de novo.", "erro")
        return redirect(url_for("seats", trip_id=t["trip_id"], d=t["travel_date"]))
    day = date.fromisoformat(t["travel_date"])
    cross = places.is_cross_border(trip["origin"], trip["destination"])

    def page(form):
        return render_template("checkout.html", t=t, trip=trip, seats=seat_rows, day=day, cross=cross, form=form,
                               hold_left=max(0, t["hold_expires"] - int(time.time())))

    if request.method == "GET":
        return page({})

    f = request.form
    errors, pax = [], []
    for s in seat_rows:
        name = f.get(f"nome_{s['seat']}", "").strip()[:120]
        doc = f.get(f"doc_{s['seat']}", "").strip()[:30]
        gender = f.get(f"sexo_{s['seat']}", "")
        try:
            age = int(f.get(f"idade_{s['seat']}", "0"))
        except ValueError:
            age = 0
        if len(name) < 3:
            errors.append(f"Lugar {s['seat']}: indique o nome completo do passageiro.")
        if not 0 < age < 120:
            errors.append(f"Lugar {s['seat']}: idade inválida.")
        if cross and len(doc) < 5:
            errors.append(f"Lugar {s['seat']}: viagem internacional exige número de passaporte.")
        pax.append((name, gender if gender in ("F", "M") else None, age, doc or None, s["id"]))
    phone = payments.normalize_phone(f.get("telefone", ""))
    if not phone:
        errors.append("Telemóvel de contacto inválido (ex.: 84 123 4567).")
    method = f.get("pagamento", "mpesa")
    if method not in payments.METHODS or method == "numerario":
        errors.append("Escolha um método de pagamento.")
    discount, promo = tickets.apply_promo(f.get("promo", ""), t["fare_total"])
    if f.get("promo", "").strip() and not promo:
        errors.append("Código promocional inválido.")
    if errors:
        for e in errors:
            flash(e, "erro")
        return page(f)

    amount = t["fare_total"] - discount
    with DB.connect() as conn:
        for name, gender, age, doc, sid in pax:
            conn.execute("UPDATE ticket_seats SET passenger=?, gender=?, age=?, doc=? WHERE id=?",
                         (name, gender, age, doc, sid))
        conn.execute("UPDATE tickets SET contact_name=?, phone=?, email=?, discount=?, promo=?, amount=?,"
                     " payment_method=? WHERE pnr=?",
                     (pax[0][0], phone, f.get("email", "").strip()[:120] or None, discount, promo, amount, method, pnr))
    result = payments.charge(method, f.get("telefone_pagamento") or phone, amount, pnr)
    DB.record_payment(pnr, method, phone, amount, result)
    with DB.connect() as conn:
        if result.status == "pago":
            # Confirm only if the hold is still ours (it may have expired during a slow payment).
            n = conn.execute("UPDATE tickets SET status='confirmado', hold_expires=NULL WHERE pnr=? AND "
                             "status='reservado'", (pnr,)).rowcount
            if not n:
                flash("Pagamento recebido, mas a reserva expirou. A equipa vai contactá-lo para reembolso "
                      "ou novo lugar.", "erro")
                return redirect(url_for("ticket", pnr=pnr))
        elif result.status == "pendente" and method == "transferencia":
            # Bank transfer: keep the seats up to 24h (and at least 3h before departure) while it clears.
            conn.execute("UPDATE tickets SET hold_expires=? WHERE pnr=?",
                         (min(int(time.time()) + 24 * 3600,
                              int(tickets.departure_dt(trip, day).timestamp()) - 3 * 3600), pnr))
    flash(result.message, "ok" if result.ok else "erro")
    if result.status == "pago" or method == "transferencia":
        return redirect(url_for("ticket", pnr=pnr))
    return redirect(url_for("checkout", pnr=pnr))


@app.get("/bilhete/<pnr>")
def ticket(pnr: str):
    if not _own(pnr):
        return redirect(url_for("find_ticket", pnr=pnr))
    t, trip, seat_rows = tickets.load(pnr)
    if not t:
        abort(404)
    day = date.fromisoformat(t["travel_date"])
    qr = segno.make(f"{pnr}|{trip['id']}|{t['travel_date']}|{','.join(str(s['seat']) for s in seat_rows)}",
                    error="m").svg_inline(scale=4, dark="#14201b")
    refund = tickets.refund_quote(trip, day, t["amount"]) if t["status"] == "confirmado" else None
    dep = tickets.departure_dt(trip, day)
    return render_template("ticket.html", t=t, trip=trip, seats=seat_rows, day=day, qr=qr, refund=refund,
                           depart=dep, arrive=tickets.arrival_dt(trip, day), report=dep - timedelta(minutes=30),
                           track=tickets.tracking(trip, day), bc=tickets.BUS_CLASSES[trip["bus_class"]])


@app.post("/bilhete/<pnr>/cancelar")
def cancel_ticket(pnr: str):
    if not _own(pnr):
        abort(403)
    try:
        refund = tickets.cancel(pnr)
    except tickets.BookingError as e:
        flash(str(e), "erro")
    else:
        flash(f"Bilhete cancelado. Reembolso de {places.mzn(refund)} para o número usado no pagamento "
              f"em até 48h." if refund else "Bilhete cancelado.", "ok")
    return redirect(url_for("ticket", pnr=pnr))


@app.get("/bilhetes")
def find_ticket():
    pnr = request.args.get("pnr", "").strip().upper()
    phone = payments.normalize_phone(request.args.get("telefone", ""))
    if pnr and phone:
        with DB.connect() as conn:
            ok = conn.execute("SELECT 1 FROM tickets WHERE pnr=? AND phone=?", (pnr, phone)).fetchone()
        if ok:
            _remember(pnr)
            return redirect(url_for("ticket", pnr=pnr))
        flash("Não encontrámos um bilhete com esse código e telemóvel.", "erro")
    mine = []
    if session.get("my_tickets"):
        with DB.connect() as conn:
            q = ",".join("?" * len(session["my_tickets"]))
            mine = conn.execute(f"SELECT k.*, t.origin, t.destination, t.depart FROM tickets k JOIN trips t "
                                f"ON t.id=k.trip_id WHERE k.pnr IN ({q}) AND k.status IN ('confirmado','cancelado')"
                                f" ORDER BY k.travel_date DESC", session["my_tickets"]).fetchall()
    return render_template("find_ticket.html", mine=mine)


@app.route("/operadores", methods=["GET", "POST"])
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
            fleet = max(1, min(1000, int(f.get("frota") or 1)))
        except ValueError:
            fleet = 1
        routes = f.get("rotas", "").strip()[:500]
        with DB.connect() as conn:
            conn.execute("INSERT INTO partners (company, contact_name, phone, email, city, nuit, fleet_size,"
                         " categories, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (company[:120], contact[:120], phone, f.get("email", "")[:120], city,
                          f.get("nuit", "")[:20], fleet, routes, now_iso()))
        flash("Recebemos o seu registo! A nossa equipa liga em 48h para configurar os seus horários.", "ok")
        return redirect(url_for("partners"))
    return render_template("partners.html")


# ----------------------------------------------------------------- admin --

@app.get("/admin")
@admin_required
def admin():
    with DB.connect() as conn:
        tickets.expire_holds(conn)
        tix = conn.execute("SELECT k.*, t.origin, t.destination, t.depart, o.name AS operator, "
                           "(SELECT GROUP_CONCAT(seat) FROM ticket_seats s WHERE s.pnr=k.pnr) AS seat_list "
                           "FROM tickets k JOIN trips t ON t.id=k.trip_id JOIN operators o ON o.id=t.operator_id "
                           "WHERE k.status != 'expirado' ORDER BY k.created_at DESC LIMIT 100").fetchall()
        kpi = conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(amount),0) revenue,
                (SELECT COUNT(*) FROM ticket_seats s JOIN tickets k2 ON k2.pnr=s.pnr
                 WHERE k2.status='confirmado') seats FROM tickets WHERE status='confirmado'""").fetchone()
        departures = conn.execute(
            "SELECT k.trip_id, k.travel_date, t.origin, t.destination, t.depart, o.name AS operator, "
            "COUNT(s.id) AS sold FROM tickets k "
            "JOIN ticket_seats s ON s.pnr=k.pnr AND s.active=1 JOIN trips t ON t.id=k.trip_id "
            "JOIN operators o ON o.id=t.operator_id WHERE k.status='confirmado' AND k.travel_date >= ? "
            "GROUP BY k.trip_id, k.travel_date ORDER BY k.travel_date, t.depart LIMIT 50",
            (tickets.now().date().isoformat(),)).fetchall()
        applications = conn.execute("SELECT * FROM partners WHERE status='pendente' ORDER BY id DESC").fetchall()
        leads = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("admin.html", tix=tix, kpi=kpi, departures=departures, applications=applications,
                           leads=leads)


@app.post("/admin/bilhete/<pnr>/confirmar")
@admin_required
def admin_confirm_ticket(pnr: str):
    """Confirm a bank-transfer ticket once the money is in the account."""
    with DB.connect() as conn:
        n = conn.execute("UPDATE tickets SET status='confirmado', hold_expires=NULL WHERE pnr=? AND "
                         "status='reservado'", (pnr,)).rowcount
    flash(f"{pnr} confirmado." if n else f"{pnr}: a reserva já expirou ou não existe.", "ok" if n else "erro")
    return redirect(url_for("admin"))


@app.post("/admin/operador/<int:pid>")
@admin_required
def admin_partner(pid: int):
    st = request.form.get("estado")
    if st in ("aprovado", "rejeitado"):
        with DB.connect() as conn:
            conn.execute("UPDATE partners SET status=? WHERE id=?", (st, pid))
    return redirect(url_for("admin"))


@app.get("/admin/manifesto/<int:trip_id>/<d>")
@admin_required
def admin_manifest(trip_id: int, d: str):
    trip, day = _departure_or_404(trip_id, d)
    return render_template("manifest.html", trip=trip, day=day, rows=tickets.manifest(trip_id, day),
                           bc=tickets.BUS_CLASSES[trip["bus_class"]])


@app.post("/admin/embarque")
@admin_required
def admin_board():
    pnr = request.form.get("pnr", "").strip().upper()
    with DB.connect() as conn:
        n = conn.execute("UPDATE tickets SET boarded_at=? WHERE pnr=? AND status='confirmado' AND boarded_at IS NULL",
                         (now_iso(), pnr)).rowcount
    flash(f"Bilhete {pnr} marcado como embarcado." if n else f"{pnr}: bilhete inválido, cancelado ou já embarcado.",
          "ok" if n else "erro")
    return redirect(request.referrer or url_for("admin"))


# ------------------------------------------------------------------- API --

@app.post("/api/assistente")
def api_chat():
    d = request.get_json(silent=True) or {}
    out = assistant.engine.chat(session.get("chat_sid"), str(d.get("mensagem", "")))
    session["chat_sid"] = out["session"]
    return jsonify({"reply": out["reply"], "trips": out.get("trips")})


tickets.init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=bool(os.environ.get("DEBUG")))
