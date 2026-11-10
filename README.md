# ViajaMoz + AlugaMoz

Two independent Mozambican travel apps in one repository, sharing a small common core.

| App | What it is | Port (dev) | Database |
|---|---|---|---|
| **ViajaMoz** (`viajamoz/`) | Bus tickets online, redbus-style: search routes, pick a seat, pay with M-Pesa/e-Mola/mKesh, QR e-ticket | 5001 | `viajamoz.db` |
| **AlugaMoz** (`alugamoz/`) | Hire a whole vehicle with driver, rentbus-style: buses, Coasters, minibuses, 4x4, wedding cars | 5002 | `alugamoz.db` |

Each app has its own brand and colours, database, admin panel and password, session cookie, and AI assistant. They link to each other: ViajaMoz sends big groups to AlugaMoz, and AlugaMoz sends people who only need a few seats to ViajaMoz.

## Layout

```
shared/      code both apps use
  places.py      cities, distances, place matching, MT formatting
  payments.py    M-Pesa (live API when configured), e-Mola, mKesh, bank transfer
  storage.py     SQLite helper + common tables (payments, leads, partners)
  chat.py        Claude tool-use engine (sessions, errors, fallback)
  web.py         Flask app factory: CSRF, admin login, error pages
  templates/     page layout, admin login, error page
  static/        core.css, app.js
viajamoz/    tickets.py (timetables, seat maps, holds, refunds, manifest), assistant.py, app.py, templates/, static/theme.css
alugamoz/    pricing.py (hire quotes), fleet.py (vehicles, bookings), assistant.py, app.py, templates/, static/theme.css
tests/       test_viajamoz.py, test_alugamoz.py, test_shared.py
```

Rule of thumb: if only one app needs it, it lives in that app's folder. `shared/` must never import from `viajamoz/` or `alugamoz/`.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in SECRET_KEY, ADMIN_PASSWORD, ANTHROPIC_API_KEY...
./run.sh                    # ViajaMoz http://localhost:5001 · AlugaMoz http://localhost:5002
```

One app only: `.venv/bin/python -m viajamoz.app` or `.venv/bin/python -m alugamoz.app`.

Production: run each separately behind Nginx with HTTPS, on its own domain:

```bash
gunicorn -w 1 -b 127.0.0.1:8001 viajamoz.app:app
gunicorn -w 1 -b 127.0.0.1:8002 alugamoz.app:app
```

(One worker each until the assistant's in-memory chat history moves to Redis.)

Tests: `.venv/bin/python -m unittest discover -s tests`

## ViajaMoz — bus tickets

- Search with swap button, Today/Tomorrow/Weekend shortcuts and a date strip; 56 routes in Mozambique plus Johannesburg, Nelspruit, Mbabane, Harare and Blantyre.
- Results with time, class and operator filters and sorting. Seat maps: Executivo 2+1, Semi-luxo 2+2, Standard, Minibus.
- Seats held for 10 minutes while paying (24h for bank transfer); a unique index makes double-selling impossible.
- Passenger details (passport required for international trips), promo codes, festive-season pricing.
- QR e-ticket, WhatsApp sharing, schedule-based trip status, self-service cancellation (90% / 50% / 0%).
- Admin: sales, departures, printable passenger manifest, QR/code boarding, bank-transfer confirmation, operator applications.

## AlugaMoz — vehicle hire

- Instant quotes (distance, days, driver nights, border fees, IVA 16%), category recommendation by group size, multi-vehicle groups.
- Partner fleet near the pickup point, 30% deposit, booking tracking, partner fleet sign-up, corporate page.
- Admin: bookings by status, payment status, partner approvals, AI leads.

## Before launch

- ViajaMoz: set `DEMO_FILL=0`. The operators, timetables and fares in `viajamoz/tickets.py` are fictional; replace them with real partners.
- AlugaMoz: check the rates in `alugamoz/pricing.py`; the demo fleet and the testimonials in `alugamoz/templates/index.html` are placeholders.
- Payment merchant credentials (mobile payments are simulated until then). M-Pesa refunds (B2C) are still manual.
- Use a different `SECRET_KEY` and `ADMIN_PASSWORD` for each app.
