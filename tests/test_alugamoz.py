"""AlugaMoz (vehicle hire) tests."""
import re
import unittest
from datetime import date, timedelta

import _env  # noqa: F401  (must be first)
from alugamoz import app as appmod, assistant, pricing
from shared import places


class PricingTest(unittest.TestCase):
    def test_distance_reasonable(self):
        km = places.distance_km("Maputo", "Bilene")
        self.assertTrue(120 < km < 220, km)
        self.assertTrue(1400 < places.distance_km("Maputo", "Nampula") < 2400)

    def test_round_trip_costs_more_and_has_iva(self):
        a = pricing.quote("Maputo", "Xai-Xai", "ida", "minibus", 15)
        b = pricing.quote("Maputo", "Xai-Xai", "ida_volta", "minibus", 15)
        self.assertGreater(b.total, a.total)
        self.assertEqual(b.iva, round(b.subtotal * 0.16))
        self.assertEqual(b.deposit, round(b.total * 0.30))

    def test_multiple_vehicles_for_big_groups(self):
        q = pricing.quote("Maputo", "Matola", "ida_volta", "minibus", 40)
        self.assertEqual(q.vehicles, 3)

    def test_recommendation(self):
        self.assertEqual(pricing.recommend_category(3), "executivo")
        self.assertEqual(pricing.recommend_category(12), "minibus")
        self.assertEqual(pricing.recommend_category(28), "coaster")
        self.assertEqual(pricing.recommend_category(200), "autocarro")

    def test_cross_border_fee_and_long_trip_nights(self):
        q = pricing.quote("Maputo", "Joanesburgo (África do Sul)", "ida_volta", "minibus", 10)
        self.assertGreater(q.border_fee, 0)
        self.assertGreaterEqual(q.days, 3)
        self.assertGreater(q.driver_allowance, 0)

    def test_local_hourly(self):
        q = pricing.quote("Maputo", "Maputo", "local", "luxo", 2, hours=5)
        self.assertEqual(q.base, 5 * pricing.CATEGORIES["luxo"].per_hour)

    def test_minimum_charges(self):
        q = pricing.quote("Maputo", "Bilene", "ida_volta", "autocarro", 50)
        self.assertGreaterEqual(q.base, pricing.CATEGORIES["autocarro"].day_min)
        t = pricing.quote("Aeroporto de Maputo", "Matola", "ida", "executivo", 2)
        self.assertEqual(t.base, round(pricing.CATEGORIES["executivo"].day_min * pricing.SHORT_TRIP_MIN_RATE))

    def test_match_location(self):
        self.assertEqual(places.match_location("chokwe"), "Chókwè")
        self.assertEqual(places.match_location("joburg"), "Joanesburgo (África do Sul)")
        self.assertIsNone(places.match_location("Lisboa"))

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            pricing.quote("Lisboa", "Maputo", "ida", None, 3)

    def test_mzn_format(self):
        self.assertEqual(places.mzn(12500), "12 500,00 MT")


class WebTest(unittest.TestCase):
    def setUp(self):
        appmod.app.config["TESTING"] = True
        self.c = appmod.app.test_client()

    def csrf(self, path="/veiculo/1"):
        html = self.c.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def test_pages_render(self):
        for path in ["/", "/parceiros", "/empresas", "/reserva", "/admin/login",
                     "/pesquisa?origem=Maputo&destino=Bilene&tipo=ida_volta&passageiros=30",
                     "/pesquisa?origem=Beira&tipo=local&horas=6&passageiros=4",
                     "/veiculo/1?origem=Maputo&destino=Tofo&tipo=ida_volta&passageiros=10"]:
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_search_prefers_local_vehicles(self):
        html = self.c.get("/pesquisa?origem=Nampula&destino=Ilha+de+Mo%C3%A7ambique&tipo=ida_volta&passageiros=12").get_data(as_text=True)
        first = html.index("Melhor escolha")
        self.assertIn("Nampula", html[first:first + 1500])

    def test_search_excludes_far_fleet_and_handles_big_groups(self):
        html = self.c.get("/pesquisa?origem=Maputo&destino=Bilene&tipo=ida_volta&passageiros=30").get_data(as_text=True)
        self.assertNotIn("Nampula", html.split("results-title")[1].split("</main>")[0])
        html = self.c.get("/pesquisa?origem=Maputo&destino=Xai-Xai&tipo=ida_volta&passageiros=120").get_data(as_text=True)
        self.assertIn("viaturas para 120 passageiros", html)
        html = self.c.get("/pesquisa?origem=Lichinga&destino=Cuamba&tipo=ida&passageiros=10").get_data(as_text=True)
        self.assertNotIn("0 viatura(s)", html)  # no local fleet: nearest hub is offered

    def test_post_without_csrf_rejected(self):
        self.assertEqual(self.c.post("/reservar", data={"vehicle_id": 1}).status_code, 400)

    def booking_form(self, **over):
        d = {"csrf": self.csrf(), "vehicle_id": "4", "nome": "Ana Machava", "telefone": "84 555 1234",
             "origem": "Maputo", "destino": "Bilene", "tipo": "ida_volta", "passageiros": "14",
             "data": (date.today() + timedelta(days=5)).isoformat(), "pagamento": "mpesa"}
        d.update(over)
        return d

    def test_full_booking_flow_with_mpesa(self):
        r = self.c.post("/reservar", data=self.booking_form())
        self.assertEqual(r.status_code, 302)
        ref = r.headers["Location"].rsplit("/", 1)[-1]
        html = self.c.get(f"/reserva/{ref}").get_data(as_text=True)
        self.assertIn(ref, html)
        self.assertIn("Confirmada", html)
        # another visitor cannot see it without ref + phone
        other = appmod.app.test_client()
        self.assertEqual(other.get(f"/reserva/{ref}").status_code, 302)
        self.assertEqual(other.get(f"/reserva?ref={ref}&telefone=845551234").status_code, 302)

    def test_booking_validation(self):
        r = self.c.post("/reservar", data=self.booking_form(telefone="123", data="2000-01-01"), follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("Número de telemóvel inválido", html)
        self.assertIn("data de partida", html)

    def test_bank_transfer_stays_pending_then_admin_confirms(self):
        r = self.c.post("/reservar", data=self.booking_form(pagamento="transferencia"))
        ref = r.headers["Location"].rsplit("/", 1)[-1]
        self.assertIn("Pagar o sinal agora", self.c.get(f"/reserva/{ref}").get_data(as_text=True))
        tok = self.csrf("/admin/login")
        self.c.post("/admin/login", data={"csrf": tok, "senha": "segredo"})
        self.assertIn(ref, self.c.get("/admin").get_data(as_text=True))
        self.c.post(f"/admin/reserva/{ref}", data={"csrf": tok, "estado": "confirmada",
                                                   "pagamento_estado": "sinal_pago"})
        self.assertIn("pago ✔", self.c.get(f"/reserva/{ref}").get_data(as_text=True))

    def test_admin_requires_login(self):
        self.assertEqual(self.c.get("/admin").status_code, 302)
        tok = self.csrf("/admin/login")
        r = self.c.post("/admin/login", data={"csrf": tok, "senha": "errada"}, follow_redirects=True)
        self.assertIn("Senha errada", r.get_data(as_text=True))

    def test_partner_signup(self):
        tok = self.csrf("/parceiros")
        r = self.c.post("/parceiros", data={"csrf": tok, "empresa": "Xiluva Transportes", "contacto": "Zito",
                                            "telefone": "87 111 2222", "cidade": "Xai-Xai", "frota": "4",
                                            "categorias": ["minibus", "coaster"]}, follow_redirects=True)
        self.assertIn("Recebemos o seu registo", r.get_data(as_text=True))

    def test_api_quote(self):
        r = self.c.post("/api/cotacao", json={"origem": "maputo", "destino": "ponta do ouro",
                                              "tipo": "ida_volta", "passageiros": 20})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["category"], "coaster")
        self.assertEqual(self.c.post("/api/cotacao", json={"origem": "Lisboa", "destino": "Maputo"}).status_code, 400)

    def test_chat_fallback_without_ai(self):
        r = self.c.post("/api/assistente", json={"mensagem": "Quero alugar de Maputo para Bilene com 30 pessoas"})
        self.assertIn("Total estimado", r.json["reply"])
        self.assertEqual(r.json["quote"]["category"], "coaster")
        r = self.c.post("/api/assistente", json={"mensagem": "olá"})
        self.assertIn("de onde parte", r.json["reply"])




if __name__ == "__main__":
    unittest.main()
