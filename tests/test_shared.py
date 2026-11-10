"""Shared code tests: payments, places, chat engine, app isolation."""
import unittest
from types import SimpleNamespace
from unittest import mock

import _env  # noqa: F401  (must be first)
from shared import chat, payments, places
from alugamoz import app as hire_app, assistant as hire_assistant
from viajamoz import app as ticket_app, assistant as ticket_assistant


class PlacesTest(unittest.TestCase):
    def test_find_places_in_order(self):
        self.assertEqual(places.find_places("de Chókwè para a ilha de moçambique"),
                         ["Chókwè", "Ilha de Moçambique"])
        self.assertEqual(places.find_places("Maputo -> Joanesburgo"), ["Maputo", "Joanesburgo (África do Sul)"])


class PaymentsTest(unittest.TestCase):
    def test_phone_normalisation(self):
        self.assertEqual(payments.normalize_phone("84 123 4567"), "+258841234567")
        self.assertEqual(payments.normalize_phone("+258 86-123-4567"), "+258861234567")
        self.assertIsNone(payments.normalize_phone("81 123 4567"))
        self.assertIsNone(payments.normalize_phone("12345"))

    def test_operator_mismatch_rejected(self):
        r = payments.charge("mpesa", "861234567", 1000, "AM-TEST01")
        self.assertFalse(r.ok)
        self.assertIn("Vodacom", r.message)

    def test_simulated_success(self):
        r = payments.charge("emola", "861234567", 1000, "AM-TEST01")
        self.assertTrue(r.ok and r.simulated)
        self.assertEqual(r.status, "pago")

    def test_bank_transfer_pending(self):
        r = payments.charge("transferencia", "", 1000, "AM-TEST01")
        self.assertEqual(r.status, "pendente")
        self.assertIn("AM-TEST01", r.message)



class EngineTest(unittest.TestCase):
    """Drives the tool-use loop with a fake Claude client (no network)."""

    def run_loop(self, engine, tool_name, tool_input):
        call = SimpleNamespace(type="tool_use", id="tu_1", name=tool_name, input=tool_input)
        first = SimpleNamespace(stop_reason="tool_use", content=[call])
        final = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Feito.")])
        fake = mock.MagicMock()
        fake.beta.messages.create.side_effect = [first, final]
        with mock.patch.object(engine, "client", return_value=fake), mock.patch.object(chat, "ai_enabled", return_value=True):
            out = engine.chat(None, "pergunta")
        return out, fake.beta.messages.create.call_args_list

    def test_hire_quote_round_trip(self):
        out, calls = self.run_loop(hire_assistant.engine, "cotacao", {
            "origem": "Maputo", "destino": "Tofo", "tipo": "ida_volta", "passageiros": 12,
            "categoria": "auto", "data_inicio": "", "data_fim": "", "horas": 0})
        self.assertEqual(out["reply"], "Feito.")
        self.assertEqual(out["quote"]["category"], "minibus")
        second = calls[1].kwargs
        self.assertEqual(second["model"], "claude-opus-5-5")
        self.assertEqual([t["name"] for t in second["tools"]], ["cotacao", "procurar_veiculos", "registar_contacto"])
        result = second["messages"][-2]["content"][0]   # history grows after the call: [-1] is the answer
        self.assertEqual(result["tool_use_id"], "tu_1")
        self.assertFalse(result["is_error"])
        self.assertTrue(second["messages"][0]["content"].startswith("[Hoje: "))

    def test_ticket_search_round_trip(self):
        from viajamoz import tickets
        from datetime import timedelta
        d = (tickets.now().date() + timedelta(days=2)).isoformat()
        out, calls = self.run_loop(ticket_assistant.engine, "procurar_viagens",
                                   {"origem": "Maputo", "destino": "Beira", "data": d})
        self.assertTrue(out["trips"]["viagens"])
        self.assertEqual([t["name"] for t in calls[0].kwargs["tools"]], ["procurar_viagens", "registar_contacto"])

    def test_lead_tool_validates_phone(self):
        tool = chat.lead_tool(hire_app.DB)
        self.assertIn("erro", tool({"nome": "A", "telefone": "999", "resumo": ""}))
        self.assertTrue(tool({"nome": "Ana", "telefone": "845551234", "resumo": "Casamento"})["ok"])


class IsolationTest(unittest.TestCase):
    def test_separate_databases_and_cookies(self):
        self.assertNotEqual(hire_app.DB.path, ticket_app.DB.path)
        self.assertNotEqual(hire_app.app.config["SESSION_COOKIE_NAME"], ticket_app.app.config["SESSION_COOKIE_NAME"])
        with ticket_app.DB.connect() as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tickets", names)
        self.assertNotIn("vehicles", names)

    def test_sites_link_to_each_other(self):
        hire_home = hire_app.app.test_client().get("/").get_data(as_text=True)
        ticket_home = ticket_app.app.test_client().get("/").get_data(as_text=True)
        self.assertTrue(hire_app.BRAND["sister_url"] in hire_home)      # AlugaMoz links to ViajaMoz
        self.assertTrue(ticket_app.BRAND["sister_url"] in ticket_home)  # ViajaMoz links to AlugaMoz
        self.assertNotEqual(hire_app.BRAND["sister_url"], ticket_app.BRAND["sister_url"])


if __name__ == "__main__":
    unittest.main()
