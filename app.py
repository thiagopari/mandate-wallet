"""Mandate Wallet: FastAPI app. Holds the wallet role (signs mandates) and the gate role (runs
evaluate(), charges PayPal, signs receipts). SQLite stores wallets, mandates, decisions, nonces,
webhook events and chat. The agent (agent.py) is given mandate() and submit() callbacks and never
touches PayPal or the DB; evaluate() (mandate.py) is the pure gate both the app and the red team use.
"""
import datetime as dt, json, os, sqlite3, threading, time, uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

import mandate as m
import paypal
import redteam
from agent import Agent, MODEL, DAILY_CAP

MOCK_AI = os.environ.get("MOCK", "auto") == "1" or (
    os.environ.get("MOCK", "auto") == "auto" and not os.environ.get("ANTHROPIC_API_KEY"))
if os.environ.get("MOCK") == "0" and not os.environ.get("ANTHROPIC_API_KEY") and not MOCK_AI:
    raise SystemExit("MOCK=0 needs ANTHROPIC_API_KEY")

if MOCK_AI:
    from fake_claude import FakeClaude
    client = FakeClaude()
else:
    import anthropic
    client = anthropic.Anthropic()

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
DEMO_VAULT_ID = os.environ.get("DEMO_VAULT_ID", "DEMO-VAULT")
DEMO_VAULT_SOURCE = os.environ.get("DEMO_VAULT_SOURCE", "paypal")  # "card": sandbox test card vaulted server-side
HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = json.load(open(os.path.join(HERE, "catalog.json")))

DB = sqlite3.connect(os.environ.get("DB_PATH", "mandate.db"), check_same_thread=False, isolation_level=None)
DB.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS wallets(session TEXT PRIMARY KEY, vault_id TEXT, customer_id TEXT,
    payer_email TEXT, pending_setup_id TEXT, demo INTEGER, connected_at REAL);
CREATE TABLE IF NOT EXISTS mandates(id TEXT PRIMARY KEY, session TEXT, body TEXT, sig TEXT, revoked_at REAL);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, session TEXT, mandate_id TEXT, intent TEXT,
    intent_sig TEXT, outcome TEXT, reason TEXT, checks TEXT, amount INTEGER, merchant TEXT, status TEXT,
    approved_by TEXT, paypal TEXT, receipt TEXT, receipt_sig TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS webhook_events(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS chats(session TEXT PRIMARY KEY, messages TEXT, history TEXT);
""")
DB_LOCK = threading.Lock()  # ponytail: one connection behind one global lock; fine for a single instance
DAILY = {"day": None, "n": 0}


def db(sql, *args):
    with DB_LOCK:
        return DB.execute(sql, args).fetchall()


def count_call(peek=False):
    today = dt.date.today()
    if DAILY["day"] != today:
        DAILY.update(day=today, n=0)
    if DAILY["n"] >= DAILY_CAP:
        return False
    if not peek:
        DAILY["n"] += 1
    return True


# --- wallets --------------------------------------------------------------
def get_wallet(session):
    r = db("SELECT session, vault_id, customer_id, payer_email, pending_setup_id, demo, connected_at "
           "FROM wallets WHERE session = ?", session)
    if not r:
        return None
    k = ("session", "vault_id", "customer_id", "payer_email", "pending_setup_id", "demo", "connected_at")
    return dict(zip(k, r[0]))


def mask_email(email):
    if not email or "@" not in email:
        return email or ""
    name, _, domain = email.partition("@")
    return (name[0] if name else "") + "***@" + domain


# --- mandates -------------------------------------------------------------
def latest_mandate(session):
    r = db("SELECT id, body, sig, revoked_at FROM mandates WHERE session = ? ORDER BY rowid DESC LIMIT 1", session)
    if not r:
        return None
    mid, body, sig, revoked_at = r[0]
    return {"id": mid, "body": json.loads(body), "sig": sig, "revoked_at": revoked_at}


def mandate_status(md, now):
    if md["revoked_at"] is not None:
        return "revoked"
    if now >= md["body"]["expires_at"]:
        return "expired"
    return "active"


def spent_for(mandate_id):
    r = db("SELECT COALESCE(SUM(amount), 0) FROM decisions WHERE mandate_id = ? AND status = 'paid'", mandate_id)
    return r[0][0] or 0


def active_mandate_body(session):
    """The agent's get_mandate(): the current mandate body only if active, else None."""
    md = latest_mandate(session)
    if not md or mandate_status(md, time.time()) != "active":
        return None
    body = dict(md["body"])
    remaining = md["body"]["budget"] - spent_for(md["id"])
    body["remaining"] = f"{remaining / 100:.2f}"
    return body


def latest_mandate_body(session):
    """The latest mandate body even if revoked/expired, so a purchase still reaches the gate."""
    md = latest_mandate(session)
    return md["body"] if md else None


# --- gate (submit) --------------------------------------------------------
def gate_submit(session, intent, sig):
    """The gate. Uses the session's latest mandate (even if revoked). Stores the nonce and a
    decision row for every evaluation; charges PayPal on allow. Returns the agent-facing decision."""
    md = latest_mandate(session)
    if not md:
        raise HTTPException(400, "no mandate")  # surfaces as a tool error, no decision row
    now = time.time()
    seen = {r[0] for r in db("SELECT nonce FROM nonces")}
    ctx = {"now": now, "revoked": md["revoked_at"] is not None, "spent": spent_for(md["id"]),
           "seen_nonce": seen, "catalog": CATALOG}
    decision = m.evaluate(md["body"], md["sig"], intent, sig, ctx)
    outcome, reason = decision["outcome"], decision["reason"]

    db("INSERT OR IGNORE INTO nonces VALUES (?)", intent.get("nonce"))
    decision_id = "dec_" + uuid.uuid4().hex[:12]

    status, paypal_info, receipt, receipt_sig, amount = None, None, None, None, intent.get("amount", 0)
    if outcome == "block":
        status = "blocked"
    elif outcome == "step_up":
        status = "pending"
    elif outcome == "allow":
        status, paypal_info, receipt, receipt_sig = _charge_and_receipt(
            session, md, intent, decision_id, "mandate")

    db("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       decision_id, session, md["id"], json.dumps(intent), sig, outcome, reason,
       json.dumps(decision["checks"]), amount, intent.get("merchant"), status, None,
       json.dumps(paypal_info) if paypal_info else None,
       json.dumps(receipt) if receipt else None, receipt_sig, now)

    return {"decision_id": decision_id, "outcome": outcome, "reason": reason, "status": status, "amount": amount}


def _charge_and_receipt(session, md, intent, decision_id, approved_by):
    """Charge the vaulted wallet and build a gate-signed receipt. On PayPal error, status failed."""
    wallet = get_wallet(session)
    receipt_id = "rcpt_" + uuid.uuid4().hex[:12]
    desc = f"Mandate Wallet: {intent['merchant']} ({decision_id})"
    try:
        pp = paypal.charge(wallet["vault_id"], intent["amount"], custom_id=receipt_id,
                           description=desc, request_id=decision_id,
                           source=DEMO_VAULT_SOURCE if wallet["demo"] else "paypal")
    except paypal.PayPalError as e:
        print(f"charge failed for {decision_id}: {e}", flush=True)  # PayPal's reason, for the Render log
        return "failed", None, None, None
    receipt = {
        "id": receipt_id, "decision_id": decision_id, "mandate_id": md["id"],
        "mandate_hash": m.digest(md["body"]), "intent_hash": m.digest(intent),
        "merchant": intent["merchant"], "items": intent["items"], "amount": intent["amount"],
        "currency": intent["currency"],
        "paypal": {"order_id": pp["order_id"], "capture_id": pp["capture_id"], "status": pp["status"]},
        "approved_by": approved_by, "issued_at": int(time.time()),
    }
    return "paid", receipt["paypal"], receipt, m.sign("gate", receipt)


# --- state ----------------------------------------------------------------
def dollars(cents):
    return f"{cents / 100:.2f}"


def state(session):
    now = time.time()
    wallet = get_wallet(session)
    wallet_view = None
    if wallet and wallet["vault_id"]:
        wallet_view = {"connected": True, "demo": bool(wallet["demo"]),
                       "payer_email": mask_email(wallet["payer_email"]),
                       "connected_at": wallet["connected_at"]}

    md = latest_mandate(session)
    mandate_view = None
    if md:
        st = mandate_status(md, now)
        spent = spent_for(md["id"])
        budget = md["body"]["budget"]
        mandate_view = {
            "id": md["id"], "body": md["body"], "sig": md["sig"], "status": st,
            "budget": dollars(budget), "cap": dollars(md["body"]["per_purchase_cap"]),
            "spent": dollars(spent), "remaining": dollars(budget - spent)}

    decisions = []
    for row in db("SELECT id, intent, outcome, reason, checks, amount, merchant, status, approved_by, "
                  "paypal, receipt, created_at FROM decisions WHERE session = ? ORDER BY rowid DESC", session):
        (did, intent_j, outcome, reason, checks_j, amount, merchant, st, approved_by, pp_j,
         receipt_j, created_at) = row
        intent = json.loads(intent_j)
        mer = CATALOG["merchants"].get(merchant, {})
        items = [{"sku": i["sku"], "qty": i["qty"],
                  "name": mer.get("items", {}).get(i["sku"], {}).get("name", i["sku"]),
                  "price": dollars(mer.get("items", {}).get(i["sku"], {}).get("price", 0))}
                 for i in intent.get("items", [])]
        receipt = json.loads(receipt_j) if receipt_j else None
        decisions.append({
            "id": did, "created_at": created_at, "merchant": merchant,
            "merchant_name": mer.get("name", merchant), "category": mer.get("category"),
            "items": items, "amount": dollars(amount), "outcome": outcome, "reason": reason,
            "checks": json.loads(checks_j), "status": st,
            "receipt_id": receipt["id"] if receipt else None, "approved_by": approved_by,
            "paypal": json.loads(pp_j) if pp_j else None})

    chat = load_chat(session)["history"]
    rt = load_redteam(session)
    return {"mock": {"paypal": paypal.MOCK, "ai": MOCK_AI}, "model": MODEL, "wallet": wallet_view,
            "mandate": mandate_view, "decisions": decisions, "chat": chat, "redteam": rt}


# --- chat storage ---------------------------------------------------------
def load_chat(session):
    r = db("SELECT messages, history FROM chats WHERE session = ?", session)
    if not r:
        return {"messages": [], "history": []}
    return {"messages": json.loads(r[0][0]), "history": json.loads(r[0][1])}


def save_chat(session, messages, history):
    db("INSERT OR REPLACE INTO chats VALUES (?, ?, ?)", session, json.dumps(messages), json.dumps(history))


_REDTEAM = {}  # session -> last scorecard (ephemeral: the red team never writes decisions)


def load_redteam(session):
    return _REDTEAM.get(session)


def save_redteam(session, scorecard):
    _REDTEAM[session] = scorecard


# --- FastAPI --------------------------------------------------------------
app = FastAPI()
SESSION_LOCKS = {}


def lock_for(session):
    return SESSION_LOCKS.setdefault(session, threading.Lock())


def session_id(request):
    sid = request.headers.get("x-session", "")
    if not sid:
        raise HTTPException(400, "X-Session header required")
    return sid


def with_state(session, extra=None):
    body = {"state": state(session)}
    if extra:
        body.update(extra)
    return body


@app.get("/api/config")
def config():
    return {"ok": True, "mock": {"paypal": paypal.MOCK, "ai": MOCK_AI}, "model": MODEL}


@app.get("/api/state")
def get_state(request: Request):
    return {"state": state(session_id(request))}


@app.get("/api/catalog")
def catalog():
    return CATALOG


@app.get("/.well-known/mandate-keys")
def keys():
    return {"alg": "Ed25519", "wallet": m.pub("wallet"), "agent": m.pub("agent"), "gate": m.pub("gate")}


# --- wallet ---------------------------------------------------------------
@app.post("/api/wallet/connect")
def wallet_connect(request: Request):
    session = session_id(request)
    with lock_for(session):
        setup_id, approve_url = paypal.create_setup_token(
            f"{BASE_URL}/wallet/return?session={session}", f"{BASE_URL}/?cancel=1")
        w = get_wallet(session) or {}
        db("INSERT OR REPLACE INTO wallets VALUES (?,?,?,?,?,?,?)",
           session, w.get("vault_id"), w.get("customer_id"), w.get("payer_email"),
           setup_id, w.get("demo", 0) or 0, w.get("connected_at"))
        return with_state(session, {"approve_url": approve_url})


@app.get("/wallet/return")
def wallet_return(session: str):
    with lock_for(session):
        w = get_wallet(session)
        if w and w["pending_setup_id"]:
            tok = paypal.create_payment_token(w["pending_setup_id"])
            db("INSERT OR REPLACE INTO wallets VALUES (?,?,?,?,?,?,?)",
               session, tok["vault_id"], tok["customer_id"], tok["payer_email"], None, 0, time.time())
    return RedirectResponse("/", status_code=303)


@app.post("/api/wallet/demo")
def wallet_demo(request: Request):
    session = session_id(request)
    with lock_for(session):
        db("INSERT OR REPLACE INTO wallets VALUES (?,?,?,?,?,?,?)",
           session, DEMO_VAULT_ID, "DEMO-CUSTOMER", "demo@example.com", None, 1, time.time())
        return with_state(session, {"demo": True})


@app.post("/api/wallet/disconnect")
def wallet_disconnect(request: Request):
    session = session_id(request)
    with lock_for(session):
        _revoke(session)
        w = get_wallet(session)
        if w and w["vault_id"] and not w["demo"] and w["vault_id"] != DEMO_VAULT_ID:  # never delete the shared demo vault
            try:
                paypal.delete_token(w["vault_id"])
            except paypal.PayPalError:
                pass
        db("DELETE FROM wallets WHERE session = ?", session)
        return with_state(session)


# --- mandate --------------------------------------------------------------
@app.post("/api/mandate")
def create_mandate(body: dict, request: Request):
    session = session_id(request)
    with lock_for(session):
        w = get_wallet(session)
        if not w or not w["vault_id"]:
            raise HTTPException(400, "connect a wallet first")
        _revoke(session)  # revoke the previous mandate before issuing a new one
        now = int(time.time())
        mandate_body = {
            "id": "mdt_" + uuid.uuid4().hex[:12],
            "session": session,
            "agent_pub": m.pub("agent"),
            "budget": int(round(float(body["budget"]) * 100)),
            "per_purchase_cap": int(round(float(body["per_purchase_cap"]) * 100)),
            "categories": list(body.get("categories", [])),
            "merchants": list(body.get("merchants", [])),
            "currency": "USD",
            "issued_at": now,
            "expires_at": now + int(body.get("expires_in_days", 7)) * 86400,
        }
        sig = m.sign("wallet", mandate_body)
        db("INSERT INTO mandates VALUES (?,?,?,?,?)",
           mandate_body["id"], session, json.dumps(mandate_body), sig, None)
        return with_state(session)


@app.post("/api/mandate/revoke")
def revoke_mandate(request: Request):
    session = session_id(request)
    with lock_for(session):
        _revoke(session)
        return with_state(session)


def _revoke(session):
    """Kill switch: revoke the latest active mandate and decline its pending step-ups."""
    md = latest_mandate(session)
    if md and md["revoked_at"] is None:
        db("UPDATE mandates SET revoked_at = ? WHERE id = ?", time.time(), md["id"])
        db("UPDATE decisions SET status = 'declined' WHERE mandate_id = ? AND status = 'pending'", md["id"])


# --- chat -----------------------------------------------------------------
@app.post("/api/chat")
def chat(body: dict, request: Request):
    session = session_id(request)
    with lock_for(session):
        msg = str(body.get("message", "")).strip()
        if not msg:
            raise HTTPException(400, "empty message")
        store = load_chat(session)
        messages, history = store["messages"], store["history"]
        history.append({"who": "owner", "text": msg, "tools": []})
        agent = Agent(client, CATALOG, lambda: active_mandate_body(session),
                      lambda intent, sig: gate_submit(session, intent, sig), count_call,
                      latest_cb=lambda: latest_mandate_body(session))
        reply, tools, messages = agent.turn(messages, msg)
        history.append({"who": "agent", "text": reply, "tools": tools})
        save_chat(session, messages, history)
        return with_state(session, {"reply": reply})


# --- step-up --------------------------------------------------------------
@app.post("/api/stepup/{decision_id}")
def stepup(decision_id: str, body: dict, request: Request):
    session = session_id(request)
    with lock_for(session):
        r = db("SELECT intent, status, mandate_id FROM decisions WHERE id = ? AND session = ?",
               decision_id, session)
        if not r:
            raise HTTPException(404, "no such decision")
        intent_j, status, mandate_id = r[0]
        if status != "pending":
            return with_state(session)
        if not body.get("approve"):
            db("UPDATE decisions SET status = 'declined' WHERE id = ?", decision_id)
            return with_state(session)
        intent = json.loads(intent_j)
        md = latest_mandate(session)
        now = time.time()
        # re-check: not revoked, not expired, still within budget
        if md["revoked_at"] is not None or now >= md["body"]["expires_at"] \
                or spent_for(md["id"]) + intent["amount"] > md["body"]["budget"]:
            db("UPDATE decisions SET status = 'declined' WHERE id = ?", decision_id)
            return with_state(session)
        new_status, pp, receipt, receipt_sig = _charge_and_receipt(session, md, intent, decision_id, "human")
        db("UPDATE decisions SET status = ?, approved_by = ?, paypal = ?, receipt = ?, receipt_sig = ? WHERE id = ?",
           new_status, "human", json.dumps(pp) if pp else None,
           json.dumps(receipt) if receipt else None, receipt_sig, decision_id)
        return with_state(session)


# --- refund ---------------------------------------------------------------
@app.post("/api/purchases/{decision_id}/refund")
def refund_purchase(decision_id: str, request: Request):
    session = session_id(request)
    with lock_for(session):
        r = db("SELECT status, paypal FROM decisions WHERE id = ? AND session = ?", decision_id, session)
        if not r:
            raise HTTPException(404, "no such decision")
        status, pp_j = r[0]
        if status != "paid":  # idempotent: only a paid purchase refunds
            return with_state(session)
        pp = json.loads(pp_j) if pp_j else {}
        try:
            if pp.get("capture_id"):
                paypal.refund(pp["capture_id"], "refund-" + decision_id)
        except paypal.PayPalError:
            pass
        db("UPDATE decisions SET status = 'refunded' WHERE id = ?", decision_id)
        return with_state(session)


# --- red team -------------------------------------------------------------
@app.post("/api/redteam")
def run_redteam(body: dict, request: Request):
    session = session_id(request)
    mode = body.get("mode", "gate")
    scorecard = redteam.run(mode, CATALOG, client, count_call)
    save_redteam(session, scorecard)
    return with_state(session, {"scorecard": scorecard})


# --- webhook --------------------------------------------------------------
@app.post("/api/paypal/webhook")
async def webhook(request: Request):
    raw = await request.body()
    event = json.loads(raw or b"{}")
    if not paypal.verify_webhook(request.headers, event):
        raise HTTPException(400, "webhook signature not verified")
    eid = str(event.get("id", ""))
    if eid and db("SELECT 1 FROM webhook_events WHERE id = ?", eid):
        return {"ignored": True}
    kind = event.get("event_type", "")
    res = event.get("resource") or {}
    handled = False

    if kind == "PAYMENT.CAPTURE.COMPLETED":
        rid = res.get("custom_id")
        if rid and db("SELECT 1 FROM decisions WHERE receipt IS NOT NULL AND json_extract(receipt, '$.id') = ?", rid):
            handled = True  # already paid; nothing to do but acknowledge
    elif kind in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
        cap_id = _up_capture(res) or res.get("custom_id")
        handled = _refund_by_capture(session=None, capture_id=cap_id)
    elif kind == "VAULT.PAYMENT-TOKEN.DELETED":
        vault_id = res.get("id")
        handled = _kill_vault(vault_id)

    if eid:
        db("INSERT OR IGNORE INTO webhook_events VALUES (?)", eid)
    if not handled:
        return {"ignored": True}
    return {"ok": True}


def _up_capture(res):
    """The capture id from the 'up' link (last path segment), used by refund/reversal events."""
    for l in res.get("links", []):
        if l.get("rel") == "up":
            return l.get("href", "").rstrip("/").rsplit("/", 1)[-1]
    return None


def _refund_by_capture(session, capture_id):
    if not capture_id:
        return False
    rows = db("SELECT id, session FROM decisions WHERE status = 'paid' "
              "AND json_extract(paypal, '$.capture_id') = ?", capture_id)
    if not rows:
        return False
    for did, _sess in rows:
        db("UPDATE decisions SET status = 'refunded' WHERE id = ?", did)
    return True


def _kill_vault(vault_id):
    if not vault_id:
        return False
    rows = db("SELECT session FROM wallets WHERE vault_id = ?", vault_id)
    if not rows:
        return False
    for (sess,) in rows:
        _revoke(sess)
        db("DELETE FROM wallets WHERE session = ?", sess)
    return True


# --- receipts -------------------------------------------------------------
@app.get("/api/receipts/{receipt_id}")
def get_receipt(receipt_id: str):
    r = db("SELECT intent, intent_sig, receipt, receipt_sig, mandate_id FROM decisions "
           "WHERE receipt IS NOT NULL AND json_extract(receipt, '$.id') = ?", receipt_id)
    if not r:
        raise HTTPException(404, "no such receipt")
    intent_j, intent_sig, receipt_j, receipt_sig, mandate_id = r[0]
    intent = json.loads(intent_j)
    receipt = json.loads(receipt_j)
    mr = db("SELECT body, sig FROM mandates WHERE id = ?", mandate_id)
    mandate_body, mandate_sig = (json.loads(mr[0][0]), mr[0][1]) if mr else (None, None)
    keys_view = {"wallet": m.pub("wallet"), "agent": m.pub("agent"), "gate": m.pub("gate")}
    verify = {
        "receipt_sig_ok": m.verify(keys_view["gate"], receipt, receipt_sig),
        "mandate_sig_ok": m.verify(keys_view["wallet"], mandate_body, mandate_sig) if mandate_body else False,
        "intent_sig_ok": m.verify(mandate_body["agent_pub"], intent, intent_sig) if mandate_body else False,
        "hashes_match": bool(mandate_body) and receipt["mandate_hash"] == m.digest(mandate_body)
        and receipt["intent_hash"] == m.digest(intent),
    }
    return {"receipt": receipt, "receipt_sig": receipt_sig, "mandate": mandate_body,
            "mandate_sig": mandate_sig, "intent": intent, "intent_sig": intent_sig,
            "keys": keys_view, "verify": verify}


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
