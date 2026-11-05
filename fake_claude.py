"""Scripted stand-in for anthropic.Anthropic() when MOCK=1: same beta.messages.create shape,
so app.agent_turn runs its real tool loop against the gate.

Deliberately naive, so MOCK shows the gate doing the work (not the model being clever):
turn 1 calls get_mandate then one purchase per merchant for the keywords in the last owner
message; turn 2 (after the tool results) summarises what came back. No keyword -> short help.
"""
import json, uuid
from types import SimpleNamespace as NS

# keyword -> (merchant, sku). One purchase call per distinct merchant matched.
KEYWORDS = [
    ("oat milk", "fenway-grocer", "oat-milk"),
    ("eggs", "fenway-grocer", "eggs-dozen"),
    ("usb", "backbay-office", "usb-c-hub"),
    ("lamp", "backbay-office", "desk-lamp"),
    ("earbuds", "gadget-barn", "wireless-earbuds"),
    ("protection", "gadget-barn", "priority-protection"),
    ("gift card", "gift-card-hub", "store-gift-card-100"),
    ("spin", "neon-jackpot", "spin-credits-95"),
    ("train", "logan-travel", "bos-nyc-train"),
]


def text(t):
    return NS(content=[NS(type="text", text="[mock AI] " + t)], stop_reason="end_turn")


def call(*calls, say=""):
    return NS(content=([NS(type="text", text="[mock AI] " + say)] if say else [])
              + [NS(type="tool_use", id="toolu_" + uuid.uuid4().hex[:8], name=n, input=a) for n, a in calls],
              stop_reason="tool_use")


def _purchases(said):
    """Group matched keywords by merchant into one purchase call each, in first-seen order."""
    low = said.lower()
    order, by_merchant = [], {}
    for kw, merchant, sku in KEYWORDS:
        if kw in low:
            if merchant not in by_merchant:
                by_merchant[merchant] = []
                order.append(merchant)
            by_merchant[merchant].append({"sku": sku, "qty": 1})
    return [(m, by_merchant[m]) for m in order]


class FakeClaude:
    def __init__(self):
        self.messages = self
        self.beta = self

    def create(self, messages, **_):
        last = messages[-1]["content"]
        results = [b for b in last if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            calls = [b for b in messages[-2]["content"] if b["type"] == "tool_use"]
            out = [(c["name"], json.loads(r["content"])) for c, r in zip(calls, results)]
            lines = []
            for name, res in out:
                if name == "purchase":
                    lines.append(f"{res.get('outcome', '?')} ({res.get('reason', '')})"
                                 + (f" ${res.get('amount')}" if res.get("amount") is not None else ""))
            return text("Here's what happened: " + "; ".join(lines) + "." if lines
                        else "I checked the mandate; tell me what to buy.")
        said = " ".join(b["text"] for b in last if isinstance(b, dict) and b.get("type") == "text")
        buys = _purchases(said)
        if not buys:
            return text("Tell me what to buy from the catalog and I'll check it against the mandate.")
        calls = [("get_mandate", {})] + [
            ("purchase", {"merchant": m, "items": items, "reason": "owner asked"}) for m, items in buys]
        return call(*calls)
