"""Products sold by KlipCut through Dodo Payments.

Two kinds of thing are sold, and Dodo handles them differently:

  SUBSCRIPTIONS (Creator, Pro)  -- one Dodo product per tier, at a fixed price.
      Dodo, like every processor, only supports "pay what you want" on one-time
      products, so a recurring plan cannot take an arbitrary amount at checkout.
      Each tier is therefore its own immutable Dodo product ID, pasted below.

  TOP-UPS (credit packs)        -- ONE Dodo product, priced dynamically.
      The single Pay-What-You-Want product `DODO_TOPUP_PRODUCT_ID` is reused for
      every pack; the exact price is passed as `amount` when the checkout is
      created (see dodo_payments.create_topup_checkout). No per-pack product to
      maintain -- add a pack in billing.TOPUPS and it just works.

Product IDs identify public products, so this file is safe to commit. Secrets
(the API key, the webhook key) live in the environment, never here.
"""

import os
from dataclasses import dataclass


# The one Pay-What-You-Want one-time product every credit pack checks out
# through. Create it in the Dodo dashboard with "Pay What You Want" enabled and
# a min/max that covers your smallest and largest pack, then set this env var.
DODO_TOPUP_PRODUCT_ID = os.environ.get("DODO_TOPUP_PRODUCT_ID", "").strip()


@dataclass(frozen=True)
class DodoProduct:
    sku: str
    product_id: str
    kind: str                       # "subscription" | "topup"
    credits: int
    plan_id: str | None = None
    interval: str | None = None

    @property
    def grant_credits(self) -> int:
        """Credits delivered by one successful charge.

        Yearly plans charge once and are granted all twelve monthly allowances;
        monthly plans and top-ups grant their face value.
        """
        return self.credits * 12 if self.interval == "yearly" else self.credits


def _subscription(plan: str, credits: int, interval: str,
                  product_id: str = "") -> DodoProduct:
    return DodoProduct(
        sku=f"{plan}:{credits}:{interval}", product_id=product_id,
        kind="subscription", credits=credits, plan_id=plan, interval=interval,
    )


# ---------------------------------------------------------------------------
# PASTE DODO SUBSCRIPTION PRODUCT IDS HERE (one per tier, prices from billing.py)
# ---------------------------------------------------------------------------
# A tier with a blank product_id reports "not available yet" at checkout rather
# than charging the wrong price. Top-ups need NO entry here -- they are dynamic.
SUBSCRIPTIONS = (
    #                            price to set on the Dodo product
    _subscription("creator", 500, "monthly",  product_id="pdt_0NnTTeprrcG4xP8suv8IR"),   # $12 / mo (500 credits, LIVE)
    _subscription("creator", 500, "yearly",   product_id=""),   # $120 / yr
    _subscription("creator", 1000, "monthly", product_id="pdt_0NnTRYnbamySwNiQjYawb"),   # $19 / mo  (1000 credits, LIVE)
    _subscription("creator", 1000, "yearly",  product_id="pdt_0NnTREsU0FXvNK5Fbe1Cx"),   # $180 / yr (1000 credits, LIVE)
    _subscription("creator", 1500, "monthly", product_id=""),   # $27 / mo
    _subscription("creator", 1500, "yearly",  product_id=""),   # $264 / yr
    _subscription("creator", 2000, "monthly", product_id=""),   # $34 / mo
    _subscription("creator", 2000, "yearly",  product_id=""),   # $324 / yr
    _subscription("pro", 1000, "monthly",     product_id=""),   # $49 / mo
    _subscription("pro", 1000, "yearly",      product_id=""),   # $468 / yr
)


def subscription_product(plan_id: str, credits: int,
                         interval: str) -> DodoProduct | None:
    sku = f"{plan_id}:{credits}:{interval}"
    return next((p for p in SUBSCRIPTIONS if p.sku == sku), None)


def product_by_id(product_id: str) -> DodoProduct | None:
    """Reverse a subscription Dodo product back to its tier.

    Top-ups intentionally have no entry: they share one product ID and their
    credit amount travels in the checkout metadata, not in this catalog.
    """
    if not product_id:
        return None
    return next((p for p in SUBSCRIPTIONS
                 if p.product_id and p.product_id == product_id), None)
