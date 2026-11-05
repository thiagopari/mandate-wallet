# Mandate Wallet

An AI shopping agent (Claude) spends from the owner's real PayPal wallet, but only inside a
**mandate** the owner signed: a budget, a per-purchase cap, allowed categories and merchants, and
an expiry. Every purchase passes a deterministic **gate** (plain code, not the model) before any
money moves. Allowed purchases charge the vaulted PayPal wallet with no buyer present
(Vault v3 + Orders v2) and produce a **gate-signed receipt** anyone can verify. A purchase over
the cap steps up to the owner; the kill switch revokes the mandate.

Three roles, three Ed25519 keys: **wallet** signs mandates, **agent** signs purchase intents,
**gate** signs receipts.

## How the gate works

`mandate.evaluate()` is a pure function. For each intent it runs checks in a fixed order and stops
at the first failure: mandate signature, not revoked, not expired, agent bound, agent signature,
fresh nonce, fresh intent, known items, price, merchant allowed, category allowed, budget, and the
per-purchase cap. The outcome is `allow`, `block`, or `step_up` (over cap). The model never sets a
price or a signature: the runtime prices every order from `catalog.json` and signs the intent.

## Run locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
MOCK=1 uvicorn app:app --port 8000     # no PayPal or Anthropic keys needed
python test_e2e.py                      # end-to-end over HTTP, all mocked
```

Open http://localhost:8000 for a placeholder page that shows `/api/state`. With `MOCK=1` both
PayPal and the AI are faked in-memory, so the whole flow (connect, mandate, chat, gate, receipts,
red team) runs with no network and no credentials.

## Environment

| var | what |
|---|---|
| `MOCK` | `auto` (default), `1`, or `0`. `auto` mocks PayPal without PayPal creds and the AI without `ANTHROPIC_API_KEY`; `0` with missing creds exits. |
| `KEY_SEED` | derives the three Ed25519 keys. Required unless PayPal is mocked (then `dev-seed`). |
| `MODEL` | Claude model, default `claude-opus-5-5`. |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | PayPal sandbox credentials (live mode only). |
| `PAYPAL_WEBHOOK_ID` | verifies incoming webhooks; unset rejects every webhook in live mode. |
| `ANTHROPIC_API_KEY` | Claude API key (live mode only). |
| `BASE_URL` | public base for PayPal return/cancel URLs, default `http://localhost:8000`. |
| `DB_PATH` | SQLite path, default `mandate.db`. |
| `DEMO_VAULT_ID` | vault id used by the one-click demo wallet. |
| `DAILY_MODEL_CALLS` | daily cap on model calls, default `200`. |

## More

The full contract (signed objects, gate checks, API, webhook, red team) is in
[DESIGN.md](DESIGN.md).
