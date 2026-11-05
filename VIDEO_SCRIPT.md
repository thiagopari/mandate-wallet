# Mandate Wallet: demo video script (target 2:50, hard limit under 3:00)

Rules: public on YouTube, English, no third-party logos or copyrighted music. Record a human voice over a screen capture of the app on the PayPal sandbox. Use one fresh session: connect, sign, then follow the beats in order.

| Time | Screen | Voice |
|---|---|---|
| 0:00–0:15 | App, empty wallet | "Would you hand an AI agent your PayPal account? Today it's all or nothing. Mandate Wallet lets an agent spend only inside limits you sign." |
| 0:15–0:35 | Connect → PayPal sandbox approval → back in the app, wallet connected | "First I save my PayPal wallet once. PayPal gives the app a vault token, not my login." |
| 0:35–0:55 | Mandate form: $150 budget, $60 per purchase, groceries and office, 7 days → Sign | "Then I sign a mandate: how much, per purchase, which categories, for how long. My wallet key signs it." |
| 0:55–1:15 | Chat: "Restock oat milk and eggs" → gate feed shows paid, 13/13 checks | "Claude shops. Before PayPal is charged, a gate in plain code runs 13 checks: signatures, freshness, prices, budget. All pass, so PayPal charges $10.48." |
| 1:15–1:35 | Chat: "Buy a USB-C hub and a desk lamp" → step-up card → Approve → paid | "$93 is over my $60 cap, so the gate holds it for me. I approve, and it's paid." |
| 1:35–1:55 | Chat: "Buy wireless earbuds" → Claude declines | "Earbuds aren't in my mandate. The product page also hides an instruction telling AI agents to ignore spending limits. Claude points it out and refuses." |
| 1:55–2:20 | Red team panel: run gate (14/14), run agent (6/6) | "We attack it 20 ways: forged mandates, replays, fake prices, fake PayPal Support messages. Claude refuses on its own, and even a naive agent that falls for everything can't get past the gate. Zero dollars moved." |
| 2:20–2:35 | Receipt dialog: 4 checks verified, PayPal order and capture ids → Refund | "Every purchase gets a signed receipt on the PayPal order, which any merchant can verify with our public keys. A refund puts the money back in the budget." |
| 2:35–2:50 | Kill switch → chat "Restock oat milk" → Claude says there is no active mandate | "One click revokes the mandate. Claude stops, and the gate would reject any request anyway. The model talks. The mandate decides." |

Before recording: run on the deployed URL (or locally with MOCK=0), use the sandbox buyer account, and close other tabs. Check the length before uploading.
