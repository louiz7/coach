import stripe
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.subscription import Subscription
from app.schemas.subscription import SubscriptionStatus, CheckoutResponse, PortalResponse
from app.utils.auth import get_current_user

stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(prefix="/api/v1/subscription", tags=["subscription"])


@router.get("/status", response_model=SubscriptionStatus)
async def get_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return SubscriptionStatus(status="none")
    return SubscriptionStatus(status=sub.status, current_period_end=sub.current_period_end)


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Find or create Stripe customer
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    sub = result.scalar_one_or_none()

    if sub and sub.stripe_customer_id:
        customer_id = sub.stripe_customer_id
    else:
        customer = stripe.Customer.create(
            email=user.email or f"{user.phone}@placeholder.com",
            metadata={"user_id": str(user.id)},
        )
        customer_id = customer.id

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=settings.ALLOWED_ORIGINS.split(",")[0] + "/success",
        cancel_url=settings.ALLOWED_ORIGINS.split(",")[0] + "/cancel",
        metadata={"user_id": str(user.id)},
    )
    return CheckoutResponse(checkout_url=session.url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    sub = result.scalar_one_or_none()
    if not sub or not sub.stripe_customer_id:
        raise HTTPException(404, "No subscription found")

    session = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=settings.ALLOWED_ORIGINS.split(",")[0],
    )
    return PortalResponse(portal_url=session.url)


# Stripe webhook — separate endpoint, no JWT auth
@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(400, "Invalid webhook")

    data = event["data"]["object"]

    if event["type"] == "customer.subscription.created":
        user_id = data.get("metadata", {}).get("user_id")
        if not user_id:
            # Try to find by customer
            result = await db.execute(
                select(Subscription).where(
                    Subscription.stripe_customer_id == data["customer"]
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                user_id = str(existing.user_id)

        if user_id:
            sub = Subscription(
                user_id=user_id,
                stripe_customer_id=data["customer"],
                stripe_subscription_id=data["id"],
                status=data["status"],
                current_period_end=data.get("current_period_end"),
            )
            db.add(sub)
            await db.commit()

    elif event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == data["id"]
            )
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = data.get("status", sub.status)
            if data.get("current_period_end"):
                from datetime import datetime
                sub.current_period_end = datetime.utcfromtimestamp(data["current_period_end"])
            await db.commit()

    elif event["type"] == "invoice.payment_failed":
        sub_id = data.get("subscription")
        if sub_id:
            result = await db.execute(
                select(Subscription).where(
                    Subscription.stripe_subscription_id == sub_id
                )
            )
            sub = result.scalar_one_or_none()
            if sub:
                sub.status = "past_due"
                await db.commit()

    return {"ok": True}
