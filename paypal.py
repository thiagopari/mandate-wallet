"""PayPal Vault v3 + Orders v2 over plain REST (stdlib urllib only).

The owner vaults a PayPal wallet once (setup token -> payment token). After that the agent's
allowed purchases charge that vaulted wallet with no buyer present. MOCK=1 swaps the HTTP layer
for an in-memory fake with the same function names, so the whole app runs with no network.
"""
import base64, json, os, time, uuid, urllib.error, urllib.request

BASE = os.environ.get("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
_MOCK_ENV = os.environ.get("MOCK", "auto")
MOCK = _MOCK_ENV == "1" or (_MOCK_ENV == "auto" and not (CLIENT_ID and SECRET))
if _MOCK_ENV == "0" and not (CLIENT_ID and SECRET):
    raise SystemExit("MOCK=0 needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET")
_token = {"value": "", "exp": 0}
_fake_tokens = {}  # setup_id / vault_id bookkeeping for the mock


class PayPalError(Exception):
    pass


def _call(method, path, body=None, headers=None, form=None):
    if MOCK:
        return _fake(method, path, body)
    h = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if form is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        data = form.encode()
    else:
        h["Authorization"] = "Bearer " + token()
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise PayPalError(f"{method} {path} -> {e.code}: {e.read()[:500].decode(errors='replace')}")


def token():
    if time.time() < _token["exp"]:
        return _token["value"]
    auth = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    t = _call("POST", "/v1/oauth2/token", form="grant_type=client_credentials",
              headers={"Authorization": "Basic " + auth})
    _token.update(value=t["access_token"], exp=time.time() + t["expires_in"] - 60)
    return _token["value"]


def _rid():
    return "req-" + uuid.uuid4().hex


def money(cents):
    return f"{cents / 100:.2f}"


def create_setup_token(return_url, cancel_url):
    """Start vaulting the owner's PayPal wallet. Returns (setup_id, approve_url)."""
    r = _call("POST", "/v3/vault/setup-tokens", {
        "payment_source": {"paypal": {
            "usage_pattern": "IMMEDIATE",
            "usage_type": "MERCHANT",
            "customer_type": "CONSUMER",
            "experience_context": {
                "brand_name": "Mandate Wallet",
                "shipping_preference": "NO_SHIPPING",
                "return_url": return_url,
                "cancel_url": cancel_url,
            }}}}, headers={"PayPal-Request-Id": _rid()})
    approve = next((l["href"] for l in r.get("links", []) if l.get("rel") == "approve"), "")
    return r["id"], approve


def create_payment_token(setup_id):
    """Exchange the approved setup token for a durable vault (payment) token.
    Returns {vault_id, customer_id, payer_email}."""
    r = _call("POST", "/v3/vault/payment-tokens",
              {"payment_source": {"token": {"id": setup_id, "type": "SETUP_TOKEN"}}},
              headers={"PayPal-Request-Id": _rid()})
    src = r.get("payment_source", {}).get("paypal", {})
    return {"vault_id": r["id"], "customer_id": r.get("customer", {}).get("id", ""),
            "payer_email": src.get("email_address", "")}


def charge(vault_id, amount, custom_id, description, request_id, source="paypal"):
    """Charge the vaulted wallet with no buyer present (Orders v2, auto-captured).
    source is the vault token's type: "paypal" (a connected wallet) or "card" (the demo's test card).
    custom_id and invoice_id are the receipt id; request_id is the decision id (idempotency).
    Returns {order_id, capture_id, status}; anything but a completed capture raises."""
    o = _call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "payment_source": {source: {"vault_id": vault_id}},
        "purchase_units": [{
            "amount": {"currency_code": "USD", "value": money(amount)},
            "custom_id": custom_id,
            "invoice_id": custom_id,
            "description": description[:127],
        }]}, headers={"PayPal-Request-Id": request_id})
    cap = (o.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [{}]
    cap = cap[0]
    if o.get("status") != "COMPLETED" or cap.get("status") != "COMPLETED":
        raise PayPalError(f"charge not completed: order {o.get('status')} capture {cap.get('status')}")
    return {"order_id": o["id"], "capture_id": cap["id"], "status": cap["status"]}


def refund(capture_id, request_id):
    return _call("POST", f"/v2/payments/captures/{capture_id}/refund", {},
                 headers={"PayPal-Request-Id": request_id})


def delete_token(vault_id):
    return _call("DELETE", f"/v3/vault/payment-tokens/{vault_id}")


def verify_webhook(headers, event):
    if MOCK:
        return True
    if not WEBHOOK_ID:
        return False
    r = _call("POST", "/v1/notifications/verify-webhook-signature", {
        "auth_algo": headers.get("paypal-auth-algo"), "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": WEBHOOK_ID, "webhook_event": event})
    return r.get("verification_status") == "SUCCESS"


def _fake(method, path, body):
    """In-memory stand-in for the sandbox endpoints used above."""
    if path == "/v1/oauth2/token":
        return {"access_token": "MOCK-TOKEN", "expires_in": 32400}
    if method == "POST" and path == "/v3/vault/setup-tokens":
        sid = "SETUP-MOCK-" + uuid.uuid4().hex[:10].upper()
        ctx = body["payment_source"]["paypal"]["experience_context"]
        _fake_tokens[sid] = "setup"
        # ponytail: mock approves instantly, so approve_url is the return_url
        return {"id": sid, "status": "PAYER_ACTION_REQUIRED",
                "links": [{"rel": "approve", "href": ctx["return_url"]}]}
    if method == "POST" and path == "/v3/vault/payment-tokens":
        vid = "VAULT-MOCK-" + uuid.uuid4().hex[:10].upper()
        return {"id": vid, "customer": {"id": "CUST-MOCK-" + uuid.uuid4().hex[:6].upper()},
                "payment_source": {"paypal": {"email_address": "sb-buyer@example.com"}}}
    if method == "POST" and path == "/v2/checkout/orders":
        oid = "MOCKORDER" + uuid.uuid4().hex[:10].upper()
        amt = body["purchase_units"][0]["amount"]
        return {"id": oid, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
            {"id": "MOCKCAP" + oid[9:], "status": "COMPLETED", "amount": amt}]}}]}
    if method == "POST" and path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
        return {"id": "MOCKREF" + uuid.uuid4().hex[:8].upper(), "status": "COMPLETED"}
    if method == "DELETE" and path.startswith("/v3/vault/payment-tokens/"):
        return {}
    raise PayPalError(f"{method} {path} -> 404 (not faked)")
