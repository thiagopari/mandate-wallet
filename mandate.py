"""Mandate Wallet crypto and the pure gate.

Three roles, three Ed25519 keys derived from KEY_SEED: wallet signs mandates, agent signs
purchase intents, gate signs receipts. No I/O here: evaluate() is a pure function the gate
(app.py) calls to decide allow / block / step_up, so the same decision runs in app and redteam.
"""
import base64, hashlib, json, os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

# KEY_SEED is required unless PayPal is mocked (then a dev default, so the demo runs with no setup).
_MOCK = os.environ.get("MOCK", "auto")
_paypal_mocked = _MOCK == "1" or (_MOCK == "auto" and not (os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET")))
KEY_SEED = os.environ.get("KEY_SEED") or ("dev-seed" if _paypal_mocked else "")
if not KEY_SEED:
    raise SystemExit("KEY_SEED is required when PayPal is not mocked")


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def key(role):
    seed = hashlib.sha256((KEY_SEED + ":" + role).encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def pub(role):
    raw = key(role).public_key().public_bytes_raw()
    return b64(raw)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sign(role, obj):
    return b64(key(role).sign(canonical(obj)))


def verify(pub_b64, obj, sig):
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(pub_b64)).verify(_unb64(sig), canonical(obj))
        return True
    except (InvalidSignature, ValueError, TypeError, Exception):
        return False


def digest(obj):
    return "sha256:" + hashlib.sha256(canonical(obj)).hexdigest()


def _total(intent, catalog):
    """Catalog total for an intent's items; unknown skus price as 0 (so a price check can catch them)."""
    m = catalog["merchants"].get(intent["merchant"], {})
    items = m.get("items", {})
    return sum(items.get(i["sku"], {}).get("price", 0) * i["qty"] for i in intent["items"])


def _item_category(merchant_id, catalog):
    return catalog["merchants"].get(merchant_id, {}).get("category")


def evaluate(mandate, mandate_sig, intent, intent_sig, ctx):
    """Pure gate. ctx = {now, revoked, spent, seen_nonce, catalog}. Runs the checks in order,
    recording {name, ok} for each, and stops at the first failure.
    Returns {outcome: allow|block|step_up, reason: OK|<code>, checks}."""
    checks = []

    def fail(name, code, outcome="block"):
        checks.append({"name": name, "ok": False})
        return {"outcome": outcome, "reason": code, "checks": checks}

    def ok(name):
        checks.append({"name": name, "ok": True})

    catalog = ctx["catalog"]

    # 1 mandate signature (wallet)
    if not verify(pub("wallet"), mandate, mandate_sig):
        return fail("mandate_signature", "BAD_MANDATE_SIGNATURE")
    ok("mandate_signature")

    # 2 not revoked
    if ctx["revoked"]:
        return fail("not_revoked", "MANDATE_REVOKED")
    ok("not_revoked")

    # 3 not expired
    if not (ctx["now"] < mandate["expires_at"]):
        return fail("not_expired", "MANDATE_EXPIRED")
    ok("not_expired")

    # 4 agent bound: intent's agent_pub matches the mandate's, and ids line up
    if intent.get("agent_pub") != mandate.get("agent_pub") or intent.get("mandate_id") != mandate.get("id"):
        return fail("agent_bound", "AGENT_NOT_BOUND")
    ok("agent_bound")

    # 5 agent signature (mandate's agent_pub)
    if not verify(mandate["agent_pub"], intent, intent_sig):
        return fail("agent_signature", "BAD_AGENT_SIGNATURE")
    ok("agent_signature")

    # 6 fresh nonce
    if intent.get("nonce") in ctx["seen_nonce"]:
        return fail("fresh_nonce", "REPLAY")
    ok("fresh_nonce")

    # 7 fresh intent
    if abs(ctx["now"] - intent.get("ts", 0)) > 300:
        return fail("fresh_intent", "STALE_INTENT")
    ok("fresh_intent")

    # 8 known items: merchant and every sku exist, qty int 1-10, items non-empty
    merchant = catalog["merchants"].get(intent.get("merchant"))
    items = intent.get("items") or []
    good_items = bool(merchant) and len(items) > 0 and all(
        isinstance(i.get("qty"), int) and 1 <= i["qty"] <= 10 and i.get("sku") in merchant["items"]
        for i in items)
    if not good_items:
        return fail("known_items", "UNKNOWN_ITEM")
    ok("known_items")

    # 9 price: amount == catalog total, currency matches
    if intent.get("amount") != _total(intent, catalog) or intent.get("currency") != mandate.get("currency"):
        return fail("price", "PRICE_MISMATCH")
    ok("price")

    # 10 merchant allowed (empty list = any merchant in an allowed category)
    allowed_merchants = mandate.get("merchants") or []
    if allowed_merchants and intent["merchant"] not in allowed_merchants:
        return fail("merchant_allowed", "MERCHANT_NOT_ALLOWED")
    ok("merchant_allowed")

    # 11 category allowed
    if _item_category(intent["merchant"], catalog) not in mandate.get("categories", []):
        return fail("category_allowed", "CATEGORY_NOT_ALLOWED")
    ok("category_allowed")

    # 12 budget
    if ctx["spent"] + intent["amount"] > mandate["budget"]:
        return fail("budget", "OVER_BUDGET")
    ok("budget")

    # 13 per-purchase cap -> step_up (within budget but over the cap goes to the owner)
    if intent["amount"] > mandate["per_purchase_cap"]:
        return fail("per_purchase_cap", "OVER_CAP", outcome="step_up")
    ok("per_purchase_cap")

    return {"outcome": "allow", "reason": "OK", "checks": checks}
