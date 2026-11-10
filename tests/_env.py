"""Test environment: temp databases per app, no real AI or payment credentials. Import first."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_tmp = tempfile.mkdtemp()
os.environ["VIAJAMOZ_DB"] = os.path.join(_tmp, "viajamoz.db")
os.environ["ALUGAMOZ_DB"] = os.path.join(_tmp, "alugamoz.db")
os.environ["ADMIN_PASSWORD"] = "segredo"
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FORCE_AI",
          "MPESA_API_KEY", "MPESA_PUBLIC_KEY", "MPESA_SERVICE_PROVIDER_CODE"):
    os.environ.pop(k, None)
