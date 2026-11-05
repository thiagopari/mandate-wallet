"""End-to-end over HTTP with MOCK=1: boots uvicorn on its own port with a temp DB and
DEMO_VAULT_ID=VAULT-TEST, then drives the API through the 11 steps in the task.
Run: .venv/bin/python test_e2e.py"""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request, uuid

PORT = 8771
DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
SESSION = uuid.uuid4().hex
BASE = f"http://127.0.0.1:{PORT}"


def start():
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(PORT)],
        env={**os.environ, "MOCK": "1", "DB_PATH": DB_PATH, "DEMO_VAULT_ID": "VAULT-TEST",
             "KEY_SEED": "test-seed", "BASE_URL": BASE},
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(60):
        try:
            return p, json.loads(urllib.request.urlopen(f"{BASE}/api/config").read())
        except OSError:
            time.sleep(0.2)
    raise SystemExit(p.communicate()[1].decode()[-3000:])


def stop(p):
    p.terminate()
    err = p.communicate()[1].decode()
    if "Traceback" in err:
        print(err[-3000:])


def req(method, path, body=None, session=SESSION):
    headers = {"Content-Type": "application/json"}
    if session:
        headers["X-Session"] = session
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read())


def get(path, session=SESSION):
    return req("GET", path, session=session)


def post(path, body=None, session=SESSION):
    return req("POST", path, body if body is not None else {}, session=session)


def b64url(raw):
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


srv, cfg = start()
try:
    assert cfg["mock"]["paypal"] and cfg["mock"]["ai"], cfg

    # 1. demo wallet connect; default mandate; mandate signature verifies against the keys endpoint.
    st = post("/api/wallet/demo")["state"]
    assert st["wallet"]["connected"] and st["wallet"]["demo"], st
    dm = get("/api/catalog")["default_mandate"]
    st = post("/api/mandate", {"budget": dm["budget"] / 100, "per_purchase_cap": dm["per_purchase_cap"] / 100,
                               "categories": dm["categories"], "merchants": dm["merchants"],
                               "expires_in_days": dm["expires_in_days"]})["state"]
    md = st["mandate"]
    assert md["status"] == "active" and md["budget"] == "150.00" and md["cap"] == "60.00", md

    keys = get("/.well-known/mandate-keys", session=None)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    import base64 as _b64

    def _unb64(s):
        return _b64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def canonical(obj):
        return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

    Ed25519PublicKey.from_public_bytes(_unb64(keys["wallet"])).verify(_unb64(md["sig"]), canonical(md["body"]))

    # 2. chat "Restock oat milk and eggs" -> paid; receipt verify all true; remaining "139.52".
    st = post("/api/chat", {"message": "Restock oat milk and eggs"})["state"]
    d = st["decisions"][0]
    assert d["status"] == "paid" and d["outcome"] == "allow", d
    rid = d["receipt_id"]
    rv = get(f"/api/receipts/{rid}", session=None)["verify"]
    assert all(rv.values()), rv
    assert st["mandate"]["remaining"] == "139.52", st["mandate"]

    # 3. chat USB-C hub + desk lamp -> pending OVER_CAP -> approve -> paid, approved_by human; remaining "46.52".
    st = post("/api/chat", {"message": "Buy a USB-C hub and a desk lamp"})["state"]
    d = st["decisions"][0]
    assert d["status"] == "pending" and d["reason"] == "OVER_CAP", d
    st = post(f"/api/stepup/{d['id']}", {"approve": True})["state"]
    d = next(x for x in st["decisions"] if x["id"] == d["id"])
    assert d["status"] == "paid" and d["approved_by"] == "human", d
    assert st["mandate"]["remaining"] == "46.52", st["mandate"]

    # 4. chat wireless earbuds -> blocked CATEGORY_NOT_ALLOWED; remaining unchanged.
    st = post("/api/chat", {"message": "Buy wireless earbuds"})["state"]
    d = st["decisions"][0]
    assert d["status"] == "blocked" and d["reason"] == "CATEGORY_NOT_ALLOWED", d
    assert st["mandate"]["remaining"] == "46.52", st["mandate"]

    # 5. refund the grocery purchase -> "57.00".
    grocery = next(x for x in st["decisions"] if x["merchant"] == "fenway-grocer" and x["status"] == "paid")
    st = post(f"/api/purchases/{grocery['id']}/refund")["state"]
    assert st["mandate"]["remaining"] == "57.00", st["mandate"]

    # 6. webhook PAYMENT.CAPTURE.REFUNDED (rel up -> hub+lamp capture) -> "150.00"; same id again -> ignored.
    hublamp = next(x for x in st["decisions"] if x["merchant"] == "backbay-office" and x["status"] == "paid")
    cap_id = hublamp["paypal"]["capture_id"]
    eid = "WH-" + uuid.uuid4().hex
    event = {"id": eid, "event_type": "PAYMENT.CAPTURE.REFUNDED",
             "resource": {"links": [{"rel": "up", "href": f"https://api/v2/payments/captures/{cap_id}"}]}}
    post("/api/paypal/webhook", event, session=None)
    st = get("/api/state")["state"]
    assert st["mandate"]["remaining"] == "150.00", st["mandate"]
    again = post("/api/paypal/webhook", event, session=None)
    assert again.get("ignored") is True, again

    # 7. red team gate -> 14/14, 2/2, 0.00, every case pass.
    sc = post("/api/redteam", {"mode": "gate"})["scorecard"]
    assert sc["attacks_blocked"] == "14/14" and sc["controls_ok"] == "2/2" and sc["moved_outside_mandate"] == "0.00", sc
    assert all(c["pass"] for c in sc["cases"]), [c for c in sc["cases"] if not c["pass"]]

    # 8. red team agent -> gate_held 6/6, controls_ok 1/1, 0.00.
    sc = post("/api/redteam", {"mode": "agent"})["scorecard"]
    assert sc["gate_held"] == "6/6" and sc["controls_ok"] == "1/1" and sc["moved_outside_mandate"] == "0.00", sc

    # 9. revoke -> chat "Restock oat milk" -> blocked MANDATE_REVOKED.
    post("/api/mandate/revoke")
    st = post("/api/chat", {"message": "Restock oat milk"})["state"]
    d = st["decisions"][0]
    assert d["status"] == "blocked" and d["reason"] == "MANDATE_REVOKED", d

    # 10. webhook VAULT.PAYMENT-TOKEN.DELETED for VAULT-TEST -> wallet null.
    event = {"id": "WH-" + uuid.uuid4().hex, "event_type": "VAULT.PAYMENT-TOKEN.DELETED",
             "resource": {"id": "VAULT-TEST"}}
    post("/api/paypal/webhook", event, session=None)
    st = get("/api/state")["state"]
    assert st["wallet"] is None, st["wallet"]

    # 11. wallet/connect -> GET its approve_url -> wallet connected, not demo.
    other = uuid.uuid4().hex
    approve = post("/api/wallet/connect", session=other)["approve_url"]
    urllib.request.urlopen(approve)  # MOCK: approve_url is the return url, which creates the token
    st = get("/api/state", session=other)["state"]
    assert st["wallet"] and st["wallet"]["connected"] and not st["wallet"]["demo"], st["wallet"]

    print("e2e ok: 11/11 steps | gate 14/14 2/2 | agent gate_held 6/6 | remaining walk 139.52 -> 46.52 -> 57.00 -> 150.00")
finally:
    stop(srv)
