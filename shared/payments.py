"""Payment providers for Mozambique.

- mpesa:   Vodacom M-Pesa Open API (C2B single stage, USSD push to the customer).
           Live mode when MPESA_API_KEY + MPESA_PUBLIC_KEY + MPESA_SERVICE_PROVIDER_CODE
           are set; otherwise simulated so the full flow can be demoed.
- emola:   Movitel e-Mola  (simulated until merchant credentials are available).
- mkesh:   Tmcel mKesh     (simulated until merchant credentials are available).
- transferencia / numerario: manual, confirmed by the operations team in /admin.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

METHODS = {
    "mpesa": {"name": "M-Pesa", "operator": "Vodacom", "prefixes": ("84", "85")},
    "emola": {"name": "e-Mola", "operator": "Movitel", "prefixes": ("86", "87")},
    "mkesh": {"name": "mKesh", "operator": "Tmcel", "prefixes": ("82", "83")},
    "transferencia": {"name": "Transferência bancária", "operator": "BCI · Millennium bim · Standard Bank · Absa", "prefixes": ()},
    "numerario": {"name": "Numerário no embarque", "operator": "Só para clientes empresariais aprovados", "prefixes": ()},
}

PHONE_RE = re.compile(r"^(?:\+?258)?(8[2-7]\d{7})$")


def normalize_phone(raw: str) -> str | None:
    """Return '+2588XXXXXXXX' for a valid Mozambican mobile number, else None."""
    digits = re.sub(r"[\s\-().]", "", raw or "")
    m = PHONE_RE.match(digits)
    return f"+258{m.group(1)}" if m else None


def operator_for(phone: str) -> str | None:
    p = normalize_phone(phone)
    if not p:
        return None
    pref = p[4:6]
    for key, meta in METHODS.items():
        if pref in meta["prefixes"]:
            return key
    return None


@dataclass
class PaymentResult:
    ok: bool
    status: str        # 'pago' | 'pendente' | 'falhou'
    provider_ref: str
    message: str
    simulated: bool = False


def charge(method: str, msisdn: str, amount: int, booking_ref: str) -> PaymentResult:
    if method not in METHODS:
        return PaymentResult(False, "falhou", "", "Método de pagamento inválido.")
    if method in ("transferencia", "numerario"):
        return PaymentResult(True, "pendente", "", _manual_instructions(method, booking_ref, amount))

    phone = normalize_phone(msisdn)
    if not phone:
        return PaymentResult(False, "falhou", "", "Número de telemóvel inválido. Use 8X XXX XXXX.")
    if phone[4:6] not in METHODS[method]["prefixes"]:
        return PaymentResult(False, "falhou", "",
                             f"O número {phone} não é {METHODS[method]['operator']}. "
                             f"Escolha o método certo para o seu número.")

    if method == "mpesa" and _mpesa_configured():
        return _mpesa_c2b(phone, amount, booking_ref)

    # Simulated push: approved immediately so the demo flow is complete.
    return PaymentResult(True, "pago", f"SIM-{booking_ref}",
                         f"Pedido {METHODS[method]['name']} enviado para {phone}. "
                         f"(modo demonstração — pagamento simulado)", simulated=True)


def _manual_instructions(method: str, ref: str, amount: int) -> str:
    if method == "transferencia":
        bank = os.environ.get("BANK_DETAILS", "BCI · NIB 0008 0000 0000 0000 0000 0 (definir BANK_DETAILS)")
        return (f"Transfira {amount:,} MT para {bank}. Use a referência {ref} e envie o "
                f"comprovativo por WhatsApp.").replace(",", " ")
    return "Pagamento em numerário no embarque (sujeito a aprovação da equipa)."


# ---------------------------------------------------------------- M-Pesa ----

def _mpesa_configured() -> bool:
    return all(os.environ.get(k) for k in ("MPESA_API_KEY", "MPESA_PUBLIC_KEY", "MPESA_SERVICE_PROVIDER_CODE"))


def _mpesa_bearer() -> str:
    """M-Pesa Open API token = RSA(PKCS#1 v1.5) encryption of the API key with the public key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    pem = ("-----BEGIN PUBLIC KEY-----\n" + os.environ["MPESA_PUBLIC_KEY"].strip()
           + "\n-----END PUBLIC KEY-----")
    key = serialization.load_pem_public_key(pem.encode())
    token = key.encrypt(os.environ["MPESA_API_KEY"].encode(), padding.PKCS1v15())
    return base64.b64encode(token).decode()


def _mpesa_c2b(phone: str, amount: int, ref: str) -> PaymentResult:
    base = os.environ.get("MPESA_BASE_URL", "https://api.sandbox.vm.co.mz:18352")
    body = {
        "input_TransactionReference": ref.replace("-", ""),
        "input_CustomerMSISDN": phone.lstrip("+"),
        "input_Amount": str(amount),
        "input_ThirdPartyReference": ref.replace("-", "")[-8:],
        "input_ServiceProviderCode": os.environ["MPESA_SERVICE_PROVIDER_CODE"],
    }
    req = urllib.request.Request(
        f"{base}/ipg/v1x/c2bPayment/singleStage/", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_mpesa_bearer()}",
                 "Origin": os.environ.get("MPESA_ORIGIN", "developer.mpesa.vm.co.mz")})
    try:
        # The customer confirms with their PIN on the phone; the call blocks until then.
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            return PaymentResult(False, "falhou", "", f"M-Pesa respondeu {e.code}.")
    except (urllib.error.URLError, TimeoutError) as e:
        return PaymentResult(False, "pendente", "", f"Sem resposta do M-Pesa ({e}). Verificaremos o pagamento.")

    code = data.get("output_ResponseCode", "")
    desc = data.get("output_ResponseDesc", "")
    if code == "INS-0":
        return PaymentResult(True, "pago", data.get("output_TransactionID", ""), "Pagamento M-Pesa confirmado.")
    return PaymentResult(False, "falhou", data.get("output_TransactionID", ""), f"M-Pesa: {desc or code}")
