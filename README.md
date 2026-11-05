# Mandate Wallet

An AI shopping agent (Claude) spends from the owner's real PayPal wallet, but only inside a
**mandate** the owner signed: a budget, a per-purchase cap, allowed categories and merchants, and
an expiry. Every purchase passes a deterministic **gate** (plain code, not the model) before any
money moves. Allowed purchases charge the vaulted PayPal wallet with no buyer present
(Vault v3 + Orders v2) and produce a **gate-signed receipt** anyone can verify. A purchase over
the cap steps up to the owner; the kill switch revokes the mandate.

Three roles, three Ed25519 keys: **wallet** signs mandates, **agent** signs purchase intents,
**gate** signs receipts.

## What the demo shows

Live run on the PayPal sandbox with Claude Opus 5.5 (Nov 4 2026), one mandate: $150 budget, $60
per purchase, groceries and office supplies, 7 days.

| Owner asks | What happens |
|---|---|
| "Restock oat milk and eggs" | Paid $10.48 from the vaulted wallet; gate-signed receipt on the PayPal order |
| "Buy a USB-C hub and a desk lamp" | $93.00 is over the $60 cap, so the gate holds it; the owner approves; paid |
| "Buy wireless earbuds" | Claude declines (electronics aren't in the mandate) and flags the product page's hidden "note to AI agents" |
| Refund | PayPal refund; the $10.48 goes back into the budget |
| Kill switch | Mandate revoked; Claude stops, and the gate rejects any request with `MANDATE_REVOKED` |

## Red team

`redteam.py` runs from the app's Red team panel.

- **Gate:** 14 hand-built signed attacks (forged mandate, replay, price tampering, unlisted
  merchant, expired or revoked mandate, ...): 14/14 blocked, 2/2 controls allowed.
- **Agent:** 6 pressure prompts sent as the owner's chat (urgency, fake PayPal Support, hidden
  instructions, guilt, discount bait, "I already paid"): live Claude refused 6/6. The same prompts
  against a naive scripted agent that falls for every one: the gate still held 6/6.
- $0.00 moved outside the mandate.

## Real vs simplified

**Real:** PayPal sandbox vault (setup token approved on PayPal, then a payment method token),
Orders v2 charges on the vault token with no buyer present, refunds through the Payments API,
PayPal webhooks checked with PayPal's signature verification (needs `PAYPAL_WEBHOOK_ID`), Claude
with strict tool schemas, Ed25519 signatures on mandate, intent and receipt.

**Simplified for the demo:** fictional merchants and a fixed catalog; one sandbox merchant account
receives every payment; the three keys derive from one server seed (in production the owner and
the agent hold their own); step-up approval is an in-app button (a passkey or PayPal re-login in
production); one instance with SQLite on free hosting.

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

Open http://localhost:8000. With `MOCK=1` both PayPal and the AI are faked in memory, so the whole
flow (connect, mandate, chat, gate, receipts, red team) runs with no network and no credentials.
`MOCK=0` with sandbox credentials runs the same app on the PayPal sandbox with real Claude.

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
[DESIGN.md](DESIGN.md). The demo video plan is in [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md).

Built with the PayPal REST APIs (sandbox), the Claude API, FastAPI and AG Grid Community.
MIT license.
