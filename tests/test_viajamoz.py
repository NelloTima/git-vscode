"""ViajaMoz (bus tickets) tests: search, seat holds, double-booking, payment, cancellation, manifest."""
import re
import time
import unittest
from datetime import date, timedelta
from unittest import mock

import _env  # noqa: F401  (must be first)
from viajamoz import app as appmod, assistant, tickets

TOMORROW = tickets.now().date() + timedelta(days=1)


def first_trip(origin="Maputo", dest="Beira", d=TOMORROW):
    trips = tickets.search(origin, dest, d)
    assert trips, f"no trips {origin}->{dest} {d}"
    return trips[0]


def free_seats(trip_id, d, n):
    with tickets.DB.connect() as conn:
        trip = tickets.get_trip(conn, trip_id)
        taken = tickets.taken_seats(conn, trip, d)
    cap = tickets.BUS_CLASSES[trip["bus_class"]].seats
    return [s for s in range(1, cap + 1) if s not in taken][:n]


def points(trip_id):
    with tickets.DB.connect() as conn:
        t = tickets.get_trip(conn, trip_id)
    return tickets.points_for(t["origin"])[0], tickets.points_for(t["destination"])[0]


class EngineTest(unittest.TestCase):
    def test_layout_capacities(self):
        self.assertEqual(tickets.BUS_CLASSES["executivo"].seats, 36)
        self.assertEqual(tickets.BUS_CLASSES["semiluxo"].seats, 49)
        self.assertEqual(tickets.BUS_CLASSES["minibus"].seats, 15)

    def test_every_corridor_is_bookable_both_ways(self):
        for a, b, *_ in tickets.CORRIDORS:
            self.assertTrue(tickets.search(a, b, TOMORROW + timedelta(days=1)) or
                            tickets.search(a, b, TOMORROW + timedelta(days=2)), f"{a}->{b}")
            self.assertTrue(tickets.cheapest(b, a), f"{b}->{a}")

    def test_search_results_shape(self):
        t = first_trip()
        self.assertGreater(t["fare"], 1000)
        self.assertGreaterEqual(t["arrive_day_offset"], 0)
        self.assertLessEqual(t["seats_left"], t["capacity"])

    def test_festive_surcharge(self):
        trip = {"fare": 1000}
        self.assertEqual(tickets.seat_fare(trip, date(2026, 12, 20)), 1150)
        self.assertEqual(tickets.seat_fare(trip, date(2026, 11, 20)), 1000)

    def test_no_sales_after_cutoff_or_in_past(self):
        self.assertEqual(tickets.search("Maputo", "Beira", tickets.now().date() - timedelta(days=1)), [])
        with tickets.DB.connect() as conn:
            trip = conn.execute("SELECT * FROM trips LIMIT 1").fetchone()
        soon = tickets.now() + timedelta(minutes=10)
        fake = dict(trip)
        fake["depart"] = soon.strftime("%H:%M")
        fake["weekdays"] = "0123456"
        if soon.date() == tickets.now().date():
            self.assertFalse(tickets.bookable(fake, soon.date()))

    def test_hold_prevents_double_booking(self):
        t = first_trip()
        seat = free_seats(t["id"], TOMORROW, 1)
        b, d = points(t["id"])
        tickets.hold(t["id"], TOMORROW, seat, b, d)
        with self.assertRaises(tickets.BookingError):
            tickets.hold(t["id"], TOMORROW, seat, b, d)

    def test_hold_validation(self):
        t = first_trip()
        b, d = points(t["id"])
        with self.assertRaises(tickets.BookingError):
            tickets.hold(t["id"], TOMORROW, [], b, d)
        with self.assertRaises(tickets.BookingError):
            tickets.hold(t["id"], TOMORROW, list(range(1, 9)), b, d)
        with self.assertRaises(tickets.BookingError):
            tickets.hold(t["id"], TOMORROW, [999], b, d)
        with self.assertRaises(tickets.BookingError):
            tickets.hold(t["id"], TOMORROW, free_seats(t["id"], TOMORROW, 1), "Algures", d)

    def test_demo_occupied_seat_cannot_be_held(self):
        t = first_trip()
        demo = tickets.demo_taken(t["id"], TOMORROW, t["capacity"])
        if demo:
            b, d = points(t["id"])
            with self.assertRaises(tickets.BookingError):
                tickets.hold(t["id"], TOMORROW, [min(demo)], b, d)

    def test_expired_hold_releases_seat(self):
        t = first_trip("Maputo", "Xai-Xai")
        seat = free_seats(t["id"], TOMORROW, 1)
        b, d = points(t["id"])
        pnr = tickets.hold(t["id"], TOMORROW, seat, b, d)
        with mock.patch.object(tickets.time, "time", return_value=time.time() + 11 * 60):
            tickets.hold(t["id"], TOMORROW, seat, b, d)   # succeeds once the first hold expired
        tk, _, _ = tickets.load(pnr)
        self.assertEqual(tk["status"], "expirado")

    def test_refund_policy(self):
        t = first_trip()
        with tickets.DB.connect() as conn:
            trip = tickets.get_trip(conn, t["id"])
        dep = tickets.departure_dt(trip, TOMORROW + timedelta(days=5))
        with mock.patch.object(tickets, "now", return_value=dep - timedelta(hours=30)):
            self.assertEqual(tickets.refund_quote(trip, (dep).date(), 1000)[0], 900)
        with mock.patch.object(tickets, "now", return_value=dep - timedelta(hours=10)):
            self.assertEqual(tickets.refund_quote(trip, dep.date(), 1000)[0], 500)
        with mock.patch.object(tickets, "now", return_value=dep - timedelta(hours=2)):
            self.assertEqual(tickets.refund_quote(trip, dep.date(), 1000)[0], 0)

    def test_tracking_states(self):
        t = first_trip()
        with tickets.DB.connect() as conn:
            trip = tickets.get_trip(conn, t["id"])
        dep = tickets.departure_dt(trip, TOMORROW)
        with mock.patch.object(tickets, "now", return_value=dep + timedelta(minutes=trip["duration_min"] / 2)):
            tr = tickets.tracking(trip, TOMORROW)
        self.assertEqual(tr["state"], "em_viagem")
        self.assertTrue(40 <= tr["pct"] <= 60)

    def test_promo(self):
        self.assertEqual(tickets.apply_promo("bemvindo", 1000), (100, "BEMVINDO"))
        self.assertEqual(tickets.apply_promo("BEMVINDO", 10000)[0], 300)
        self.assertEqual(tickets.apply_promo("FALSO", 1000), (0, None))


class TicketWebTest(unittest.TestCase):
    def setUp(self):
        appmod.app.config["TESTING"] = True
        self.c = appmod.app.test_client()

    def csrf(self, path):
        return re.search(r'name="csrf" value="([^"]+)"', self.c.get(path).get_data(as_text=True)).group(1)

    def buy(self, origin="Maputo", dest="Inhambane", n=2, pay="mpesa", phone="84 555 1234", day=TOMORROW, **extra):
        t = first_trip(origin, dest, day)
        seats = free_seats(t["id"], day, n)
        b, d = points(t["id"])
        path = f"/viagem/{t['id']}/{day}"
        r = self.c.post(path, data={"csrf": self.csrf(path), "lugar": seats, "embarque": b, "desembarque": d})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        pnr = r.headers["Location"].rsplit("/", 1)[-1]
        form = {"csrf": self.csrf(f"/pagamento/{pnr}"), "telefone": phone, "pagamento": pay}
        for s in seats:
            form.update({f"nome_{s}": f"Passageiro {s}", f"idade_{s}": "30", f"sexo_{s}": "F"})
        form.update(extra)
        r = self.c.post(f"/pagamento/{pnr}", data=form)
        return pnr, seats, r

    def test_pages(self):
        for p in ["/", f"/viagens?de=Maputo&para=Beira&data={TOMORROW}", "/bilhetes", "/operadores",
                  f"/viagens?de=Maputo&para=Lichinga&data={TOMORROW}"]:
            self.assertEqual(self.c.get(p).status_code, 200, p)
        self.assertEqual(self.c.get("/viagens?de=Maputo&para=Maputo").status_code, 302)

    def test_seat_page_shows_map(self):
        t = first_trip()
        html = self.c.get(f"/viagem/{t['id']}/{TOMORROW}").get_data(as_text=True)
        self.assertEqual(html.count('name="lugar"'), t["capacity"])
        self.assertIn("Terminal da Junta", html)

    def test_full_purchase_with_promo(self):
        pnr, seats, r = self.buy(promo="BEMVINDO")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith(f"/bilhete/{pnr}"))
        html = self.c.get(f"/bilhete/{pnr}").get_data(as_text=True)
        self.assertIn("Confirmado", html)
        self.assertIn("<svg", html)          # QR code
        self.assertIn("BEMVINDO", html)
        tk, _, _ = tickets.load(pnr)
        self.assertEqual(tk["amount"], tk["fare_total"] - tk["discount"])
        self.assertGreater(tk["discount"], 0)
        # seats are no longer offered
        html = self.c.get(f"/viagem/{tk['trip_id']}/{TOMORROW}").get_data(as_text=True)
        for s in seats:
            self.assertRegex(html, rf'value="{s}" disabled')

    def test_checkout_validation_keeps_hold(self):
        pnr, _, r = self.buy(phone="123")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Telemóvel de contacto inválido", r.get_data(as_text=True))
        self.assertEqual(tickets.load(pnr)[0]["status"], "reservado")

    def test_cross_border_requires_passport(self):
        pnr, _, r = self.buy("Maputo", "Joanesburgo (África do Sul)", n=1)
        self.assertIn("passaporte", r.get_data(as_text=True))
        self.assertEqual(tickets.load(pnr)[0]["status"], "reservado")

    def test_other_users_cannot_open_ticket(self):
        pnr, _, _ = self.buy()
        other = appmod.app.test_client()
        self.assertEqual(other.get(f"/bilhete/{pnr}").status_code, 302)
        self.assertEqual(other.get(f"/pagamento/{pnr}").status_code, 403)
        r = other.get(f"/bilhetes?pnr={pnr}&telefone=845551234")
        self.assertTrue(r.headers["Location"].endswith(f"/bilhete/{pnr}"))

    def test_cancel_refunds_and_frees_seats(self):
        day = TOMORROW + timedelta(days=5)   # > 24h away: 90% refund
        pnr, seats, _ = self.buy(day=day)
        tok = self.csrf(f"/bilhete/{pnr}")
        self.c.post(f"/bilhete/{pnr}/cancelar", data={"csrf": tok})
        tk, _, _ = tickets.load(pnr)
        self.assertEqual(tk["status"], "cancelado")
        self.assertEqual(tk["refund"], round(tk["amount"] * 0.9))
        with tickets.DB.connect() as conn:
            trip = tickets.get_trip(conn, tk["trip_id"])
            self.assertFalse(set(seats) & tickets.taken_seats(conn, trip, day))

    def test_bank_transfer_then_admin_confirms_and_boards(self):
        pnr, _, r = self.buy(pay="transferencia")
        tk, _, _ = tickets.load(pnr)
        self.assertEqual(tk["status"], "reservado")
        self.assertGreater(tk["hold_expires"], time.time() + 3600)
        tok = self.csrf("/admin/login")
        self.c.post("/admin/login", data={"csrf": tok, "senha": "segredo"})
        self.c.post(f"/admin/bilhete/{pnr}/confirmar", data={"csrf": tok})
        self.assertEqual(tickets.load(pnr)[0]["status"], "confirmado")
        html = self.c.get(f"/admin/manifesto/{tk['trip_id']}/{TOMORROW}").get_data(as_text=True)
        self.assertIn(pnr, html)
        self.c.post("/admin/embarque", data={"csrf": tok, "pnr": pnr.lower()})
        self.assertIsNotNone(tickets.load(pnr)[0]["boarded_at"])

    def test_chat_fallback_finds_trips(self):
        r = self.c.post("/api/assistente", json={"mensagem": "autocarro de Maputo para a Beira amanhã"})
        self.assertIn("Maputo → Beira", r.json["reply"])
        self.assertTrue(r.json["trips"]["viagens"])
        r = self.c.post("/api/assistente", json={"mensagem": "alugar autocarro Maputo Bilene 40 pessoas"})
        self.assertIn("AlugaMoz", r.json["reply"])   # whole-bus hire lives on the sister site

    def test_assistant_trip_tool(self):
        out = assistant.tool_procurar_viagens({"origem": "nampula", "destino": "pemba", "data": TOMORROW.isoformat()})
        self.assertEqual(out["origem"], "Nampula")
        self.assertTrue(out["viagens"][0]["link_lugares"].startswith("/viagem/"))
        self.assertIn("erro", assistant.tool_procurar_viagens({"origem": "Paris", "destino": "Beira", "data": ""}))


if __name__ == "__main__":
    unittest.main()
