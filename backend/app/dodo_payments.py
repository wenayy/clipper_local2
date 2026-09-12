"""Dodo Payments checkout creation and signed, idempotent webhook fulfillment.

Two payment shapes, one fulfillment path:

  * TOP-UPS are one-time payments taken through a single Pay-What-You-Want
    product with the exact price passed as `amount`. The credits to grant travel
    in the checkout metadata, because the shared product ID cannot say how many
    credits a given pack is worth.

  * SUBSCRIPTIONS are per-tier fixed products. Their credits and plan come from
    the catalog (dodo_catalog.product_by_id), with the checkout metadata as a
    fallback so a payload that omits the product ID still fulfils.

Every grant is written behind a unique per-charge row (DodoGrant) before the
balance is touched, so a webhook redelivery -- or the confirm endpoint racing
the webhook -- can never credit an account twice.
"""

import hashlib
import json
import os

from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.jobs import extend_user_retention
from app.models import (CreditLedger, DodoGrant, DodoSubscription,
                        DodoWebhookEvent, User)
from app.dodo_catalog import DODO_TOPUP_PRODUCT_ID, product_by_id


class DodoConfigurationError(RuntimeError):
    pass


# test_mode and live_mode are separate Dodo environments with separate keys and
# separate product IDs. Explicit and defaulting to live: a forgotten variable
# fails loudly at auth rather than quietly charging in the wrong environment.
DODO_ENVIRONMENT = (os.environ.get("DODO_PAYMENTS_ENVIRONMENT", "live_mode").strip()
                    or "live_mode")

# Where Dodo returns the buyer after paying. Without it the customer is stranded
# on Dodo's page and never comes back to the app.
APP_ORIGIN = os.environ.get("CLIPPER_APP_ORIGIN", "").strip().rstrip("/")


def _client():
    api_key = os.environ.get("DODO_PAYMENTS_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from dodopayments import DodoPayments
    except ImportError as exc:
        raise DodoConfigurationError("dodopayments is not installed") from exc
    # webhook_key is optional for checkout creation but required by unwrap(); pass
    # it through so the one client serves both without a second constructor.
    return DodoPayments(
        bearer_token=api_key,
        environment=DODO_ENVIRONMENT,
        webhook_key=os.environ.get("DODO_PAYMENTS_WEBHOOK_KEY", "").strip() or None,
    )


def _return_url() -> str | None:
    if not APP_ORIGIN:
        print("[clipper] CLIPPER_APP_ORIGIN is not set: the buyer will not be "
              "redirected back after paying.")
        return None
    # `payment=success` is the marker the app already watches for: it shows the
    # confirmation toast and re-polls the balance so a webhook landing a moment
    # later still updates it. Dodo appends its own ids to this URL.
    return f"{APP_ORIGIN}/?payment=success"


def _checkout_url(payload: dict) -> str | None:
    client = _client()
    if not client:
        print("[clipper] DODO_PAYMENTS_API_KEY is not set: cannot open checkout.")
        return None
    try:
        session = client.checkout_sessions.create(**payload)
    except Exception as exc:
        print(f"[clipper] Dodo checkout session failed: {type(exc).__name__}: {exc}")
        return None
    return getattr(session, "checkout_url", None)


def create_subscription_checkout(product, user_id: str, email: str) -> str | None:
    """A subscription checkout bound to this user, or None if unconfigured."""
    if not product or not product.product_id:
        return None
    payload = {
        "product_cart": [{"product_id": product.product_id, "quantity": 1}],
        "customer": {"email": email},
        "metadata": {
            "reference_id": user_id,
            "sku": product.sku,
            "plan_id": product.plan_id or "",
            "kind": "subscription",
            "credits": str(product.grant_credits),
        },
    }
    url = _return_url()
    if url:
        payload["return_url"] = url
    return _checkout_url(payload)


def create_topup_checkout(credits: int, usd: int, user_id: str,
                          email: str) -> str | None:
    """A one-time credit-pack checkout at a dynamic price.

    Every pack reuses the single Pay-What-You-Want product; the exact amount is
    passed in cents, and the credits to grant ride in the metadata because the
    shared product cannot encode them.
    """
    if not DODO_TOPUP_PRODUCT_ID:
        print("[clipper] DODO_TOPUP_PRODUCT_ID is not set: credit packs cannot "
              "check out. Create a one-time Pay-What-You-Want product in Dodo "
              "and set this variable.")
        return None
    payload = {
        "product_cart": [{
            "product_id": DODO_TOPUP_PRODUCT_ID,
            "quantity": 1,
            "amount": int(usd) * 100,      # Dodo takes the lowest denomination
        }],
        "customer": {"email": email},
        "metadata": {
            "reference_id": user_id,
            "sku": f"topup:{credits}",
            "kind": "topup",
            "credits": str(credits),
        },
    }
    url = _return_url()
    if url:
        payload["return_url"] = url
    return _checkout_url(payload)


def validate_webhook(body: bytes, headers) -> dict:
    """Verify the Standard Webhooks signature with Dodo's SDK, return the event."""
    client = _client()
    if not client:
        raise DodoConfigurationError("DODO_PAYMENTS_API_KEY is not configured")
    if not os.environ.get("DODO_PAYMENTS_WEBHOOK_KEY", "").strip():
        raise DodoConfigurationError("DODO_PAYMENTS_WEBHOOK_KEY is not configured")

    event = client.webhooks.unwrap(
        body,
        headers={
            "webhook-id": headers.get("webhook-id", ""),
            "webhook-signature": headers.get("webhook-signature", ""),
            "webhook-timestamp": headers.get("webhook-timestamp", ""),
        },
    )
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if isinstance(event, dict):
        return event
    return json.loads(getattr(event, "json", lambda: "{}")())


def delivery_id(headers, body: bytes) -> str:
    """Standard Webhooks IDs deliveries; hash is a deterministic fallback."""
    return (headers.get("webhook-id") or headers.get("Webhook-Id")
            or hashlib.sha256(body).hexdigest())


def _data(event: dict) -> dict:
    """Dodo wraps the resource in `data`, sometimes with a nested `payload`."""
    data = event.get("data") or {}
    if isinstance(data.get("payload"), dict):
        return data["payload"]
    return data


def _metadata(data: dict) -> dict:
    md = data.get("metadata")
    return md if isinstance(md, dict) else {}


def _reference_id(data: dict) -> str | None:
    md = _metadata(data)
    customer = data.get("customer") or {}
    return (md.get("reference_id")
            or data.get("reference_id")
            or customer.get("external_id")
            or data.get("external_customer_id"))


def _customer_email(data: dict) -> str | None:
    customer = data.get("customer") or {}
    return (customer.get("email") or data.get("customer_email")
            or data.get("email"))


def _subscription_id(data: dict) -> str | None:
    return data.get("subscription_id") or (
        data.get("id") if data.get("payload_type") == "Subscription" else None)


def _resolve_user(session, data: dict) -> User | None:
    user = None
    ref = _reference_id(data)
    if ref:
        user = session.get(User, ref)
    if not user:
        # Weaker fallback: the charged email. A buyer can edit it at checkout, so
        # it is a last resort and logged. Refusing instead would mean a real
        # payment with the credits never delivered -- the worse failure.
        email = _customer_email(data)
        if email:
            user = (session.query(User)
                    .filter(User.email == email.strip().lower()).first())
            if user:
                print(f"[clipper] Dodo: matched payment by email {email!r} -> "
                      f"user {user.id} (no reference_id in metadata)")
    return user


def _grant_once(session, charge_id: str, user: User, credits: int,
                product_id: str, note: str) -> bool:
    """Credit exactly once. Returns False if this charge was already granted."""
    if not charge_id:
        return False
    if session.get(DodoGrant, charge_id):
        return False
    session.add(DodoGrant(charge_id=charge_id, user_id=user.id,
                          product_id=product_id or "", credits=credits))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    user.credits += credits
    session.add(CreditLedger(user_id=user.id, delta=credits,
                             balance_after=user.credits, note=note))
    return True


def _set_subscription(session, user: User, sub_id: str, product_id: str,
                      plan_id: str | None, status: str) -> None:
    if not sub_id:
        return
    row = session.get(DodoSubscription, sub_id)
    if not row:
        row = DodoSubscription(id=sub_id, user_id=user.id)
        session.add(row)
    row.product_id = product_id or (row.product_id or "")
    row.plan_id = plan_id or row.plan_id
    row.status = status

    if status in {"active", "trialing", "renewed"}:
        if plan_id:
            user.plan = plan_id
    elif status in {"cancelled", "canceled", "expired", "failed", "revoked"}:
        other = (session.query(DodoSubscription)
                 .filter(DodoSubscription.user_id == user.id,
                         DodoSubscription.id != sub_id,
                         DodoSubscription.status.in_(("active", "trialing", "renewed")))
                 .first())
        if not other:
            user.plan = "free"


# Events that mean "this subscription just delivered a paid period."
_SUB_GRANT = {"subscription.active", "subscription.renewed"}
# Events that end a subscription's paid entitlement.
_SUB_END = {"subscription.cancelled", "subscription.canceled",
            "subscription.expired", "subscription.failed",
            "subscription.on_hold", "subscription.revoked"}


def handle_event(event: dict, event_id: str) -> dict:
    """Apply one verified event exactly once, including Dodo retries."""
    event_type = (event.get("type") or "").strip()
    data = _data(event)

    with SessionLocal() as session:
        if session.get(DodoWebhookEvent, event_id):
            return {"status": "duplicate"}
        session.add(DodoWebhookEvent(id=event_id, event_type=event_type))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return {"status": "duplicate"}

        # --- One-time payment (credit top-up) ---
        # A subscription's recurring charge can also emit payment.succeeded; skip
        # those here and let the subscription.* branch grant them, so a single
        # renewal is never counted by two events.
        if event_type in {"payment.succeeded", "payment.completed"}:
            if _subscription_id(data):
                session.commit()
                return {"status": "ignored", "reason": "subscription payment"}
            md = _metadata(data)
            credits = int(md.get("credits") or 0)
            user = _resolve_user(session, data)
            payment_id = data.get("payment_id") or data.get("id")
            if not user or credits <= 0 or not payment_id:
                session.commit()
                return {"status": "ignored", "reason": "not a fulfillable top-up"}
            granted = _grant_once(session, payment_id, user, credits,
                                  DODO_TOPUP_PRODUCT_ID, "Dodo credit top-up")
            session.commit()
            return {"status": "applied" if granted else "duplicate",
                    "event_type": event_type}

        # --- Subscription lifecycle ---
        if event_type in _SUB_GRANT or event_type in _SUB_END:
            user = _resolve_user(session, data)
            if not user:
                session.rollback()
                raise DodoConfigurationError(
                    "Dodo subscription event has no resolvable user "
                    f"(type={event_type}, keys={sorted(data.keys())[:12]})")
            md = _metadata(data)
            product = product_by_id(data.get("product_id"))
            sub_id = _subscription_id(data)
            # Prefer the catalog (authoritative), fall back to checkout metadata
            # so an uncatalogued product still fulfils rather than aborting.
            plan_id = product.plan_id if product else (md.get("plan_id") or None)
            product_id = product.product_id if product else (data.get("product_id") or "")

            if event_type in _SUB_END:
                _set_subscription(session, user, sub_id, product_id, plan_id,
                                  event_type.removeprefix("subscription."))
                session.commit()
                return {"status": "applied", "event_type": event_type}

            # active / renewed: set the plan and grant this period's credits.
            _set_subscription(session, user, sub_id, product_id, plan_id,
                              event_type.removeprefix("subscription."))
            credits = (product.grant_credits if product
                       else int(md.get("credits") or 0))
            # Key the grant on the underlying charge so each renewal grants once,
            # but a redelivery of the same renewal does not. Fall back to the
            # delivery id, which is stable across redeliveries of one event.
            charge_id = (data.get("payment_id")
                         or data.get("last_payment_id")
                         or (f"{sub_id}:{data.get('current_period_end') or data.get('next_billing_date') or ''}"
                             if sub_id else None)
                         or event_id)
            if credits > 0:
                _grant_once(session, charge_id, user, credits, product_id,
                            f"Dodo {(plan_id or 'plan').title()} subscription")
                extend_user_retention(session, user.id, hours=72)
            session.commit()
            return {"status": "applied", "event_type": event_type}

        session.commit()
        return {"status": "ignored", "event_type": event_type}
