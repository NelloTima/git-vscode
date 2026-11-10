"""Claude-powered chat engine shared by the ViajaMoz and AlugaMoz assistants.

Each app supplies its own system prompt, tools and rule-based fallback; this module
runs the tool-use loop, keeps per-visitor conversation history and handles errors.

History is kept in process memory, so run one worker per app or move `_sessions`
to Redis before scaling out.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import date
from typing import Callable

from shared import payments
from shared.storage import Database, now

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 6
SESSION_TTL = 60 * 60 * 6
MAX_SESSIONS = 5000
WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]

LEAD_TOOL = {
    "name": "registar_contacto",
    "description": "Regista o pedido do cliente para a equipa ligar de volta.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "nome": {"type": "string"},
            "telefone": {"type": "string"},
            "resumo": {"type": "string", "description": "Resumo do pedido e preço indicado"},
        },
        "required": ["nome", "telefone", "resumo"],
    },
}


def lead_tool(db: Database) -> Callable[[dict], dict]:
    def registar_contacto(args: dict) -> dict:
        phone = payments.normalize_phone(args.get("telefone", ""))
        if not phone:
            return {"erro": "Número inválido. Precisa de um número moçambicano 82–87 com 9 dígitos."}
        with db.connect() as conn:
            conn.execute("INSERT INTO leads (name, phone, summary, created_at) VALUES (?,?,?,?)",
                         (args.get("nome", "")[:120], phone, args.get("resumo", "")[:2000], now()))
        return {"ok": True, "mensagem": "Registado. A equipa liga em até 30 minutos (08h–20h)."}
    return registar_contacto


def ai_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("FORCE_AI"))


class ChatEngine:
    """
    capture: {tool_name: (reply_key, predicate)} — when a tool result matches the
    predicate it is returned to the page under reply_key (e.g. to render a link card).
    """

    def __init__(self, system_prompt: str, tools: list[dict], impls: dict[str, Callable[[dict], dict]],
                 fallback: Callable[[str], dict], today: Callable[[], date],
                 capture: dict[str, tuple[str, Callable[[dict], bool]]] | None = None,
                 model: str | None = None):
        self.system_prompt = system_prompt
        self.tools = tools
        self.impls = impls
        self.fallback = fallback
        self.today = today
        self.capture = capture or {}
        self.model = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._client = None

    # -------------------------------------------------------------- state --
    def _session(self, sid: str | None) -> tuple[str, dict]:
        with self._lock:
            t = time.time()
            for k in [k for k, v in self._sessions.items() if t - v["t"] > SESSION_TTL]:
                del self._sessions[k]
            if len(self._sessions) > MAX_SESSIONS:
                for k in sorted(self._sessions, key=lambda k: self._sessions[k]["t"])[:len(self._sessions) - MAX_SESSIONS]:
                    del self._sessions[k]
            if not sid or sid not in self._sessions:
                sid = uuid.uuid4().hex
                self._sessions[sid] = {"messages": [], "t": t, "lock": threading.Lock()}
            self._sessions[sid]["t"] = t
            return sid, self._sessions[sid]

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    # --------------------------------------------------------------- chat --
    def chat(self, session_id: str | None, user_text: str) -> dict:
        user_text = (user_text or "").strip()[:2000]
        sid, sess = self._session(session_id)
        if not user_text:
            return {"session": sid, "reply": "Olá! Como posso ajudar com a sua viagem?"}
        if not ai_enabled():
            return {"session": sid, **self.fallback(user_text)}

        import anthropic

        with sess["lock"]:
            messages = sess["messages"]
            checkpoint = len(messages)
            d = self.today()
            # The date goes in the user turn, not the system prompt, so the cached prefix stays stable.
            messages.append({"role": "user",
                             "content": f"[Hoje: {d.isoformat()}, {WEEKDAYS_PT[d.weekday()]}]\n{user_text}"})
            captured: dict = {}
            try:
                for _ in range(MAX_TOOL_ROUNDS):
                    resp = self.client().beta.messages.create(
                        model=self.model,
                        max_tokens=4000,
                        system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
                        tools=self.tools,
                        messages=messages,
                        output_config={"effort": "low"},
                        betas=["server-side-fallback-2026-07-01"],
                        fallbacks="default",
                    )
                    messages.append({"role": "assistant", "content": resp.content})
                    if resp.stop_reason == "refusal":
                        return {"session": sid, "reply": "Desculpe, não consigo ajudar com isso. "
                                                         "Posso ajudar com a sua viagem?"}
                    if resp.stop_reason != "tool_use":
                        text = "".join(b.text for b in resp.content if b.type == "text").strip()
                        return {"session": sid, "reply": text or "Pode repetir, por favor?", **captured}

                    results = []
                    for block in resp.content:
                        if block.type != "tool_use":
                            continue
                        impl = self.impls.get(block.name)
                        try:
                            out = impl(block.input) if impl else {"erro": "ferramenta desconhecida"}
                        except Exception as e:  # a tool bug must not kill the chat
                            out = {"erro": f"falha interna: {e}"}
                        if block.name in self.capture:
                            key, ok = self.capture[block.name]
                            if ok(out):
                                captured[key] = out
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": json.dumps(out, ensure_ascii=False),
                                        "is_error": "erro" in out})
                    messages.append({"role": "user", "content": results})
                return {"session": sid, "reply": "Estou a demorar. Use a pesquisa no site ou fale connosco "
                                                 "pelo WhatsApp.", **captured}
            except anthropic.RateLimitError:
                del messages[checkpoint:]  # drop the unfinished turn, keep earlier history
                return {"session": sid, "reply": "Muitos pedidos neste momento. Tente de novo em 1 minuto."}
            except (anthropic.APIStatusError, anthropic.APIConnectionError):
                del messages[checkpoint:]
                return {"session": sid, **self.fallback(user_text)}
