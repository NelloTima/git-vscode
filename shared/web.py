"""Flask plumbing shared by both apps: app factory, CSRF, admin login, layout and errors."""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import date
from functools import wraps

from flask import Blueprint, Flask, abort, flash, redirect, render_template, request, session, url_for

from shared import chat, payments, places

shared_bp = Blueprint("shared", __name__, template_folder="templates",
                      static_folder="static", static_url_path="/shared-static")


def parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper


def create_app(import_name: str, *, brand: dict, cookie: str, today) -> Flask:
    """
    brand: name, tagline, whatsapp, phone, email, sister_name, sister_url, sister_pitch.
    today: callable returning the local date (Africa/Maputo).
    Each app gets its own session cookie so both can run on one host.
    """
    app = Flask(import_name)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_NAME=cookie, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS")))
    app.register_blueprint(shared_bp)
    admin_password = os.environ.get("ADMIN_PASSWORD", "")

    @app.context_processor
    def _globals():
        return {"brand": brand, "mzn": places.mzn, "pay_methods": payments.METHODS, "csrf_token": csrf_token,
                "today": today().isoformat(), "ai_on": chat.ai_enabled()}

    @app.before_request
    def _check_csrf():
        if request.method == "POST" and not request.path.startswith("/api/"):
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Sessão expirada. Volte atrás e tente de novo.")

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            if admin_password and hmac.compare_digest(request.form.get("senha", ""), admin_password):
                session["admin"] = True
                return redirect(url_for("admin"))
            flash("Senha errada." if admin_password else "Defina ADMIN_PASSWORD no servidor.", "erro")
        return render_template("admin_login.html")

    @app.post("/admin/sair")
    def admin_logout():
        session.pop("admin", None)
        return redirect(url_for("home"))

    @app.errorhandler(404)
    def _404(_):
        return render_template("error.html", code=404, msg="Página não encontrada."), 404

    @app.errorhandler(400)
    def _400(e):
        return render_template("error.html", code=400, msg=getattr(e, "description", "Pedido inválido.")), 400

    @app.errorhandler(403)
    def _403(_):
        return render_template("error.html", code=403, msg="Sem acesso a esta página."), 403

    return app
