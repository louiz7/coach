import stripe
import posthog
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


@router.post("/cancel")
async def cancel_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    sub = result.scalar_one_or_none()
    if not sub or not sub.stripe_subscription_id:
        raise HTTPException(404, "No active subscription found")

    stripe.Subscription.modify(
        sub.stripe_subscription_id,
        cancel_at_period_end=True,
    )
    sub.status = "canceled"
    await db.commit()
    return {"ok": True}


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

    if event["type"] == "checkout.session.completed":
        # Payment confirmed — mark user as fully onboarded
        # user_id can come from metadata (dynamic sessions) OR client_reference_id (Payment Links)
        user_id = data.get("metadata", {}).get("user_id") or data.get("client_reference_id")
        phone = data.get("metadata", {}).get("phone")
        customer_id = data.get("customer")

        if user_id:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                from app.models.user import OnboardingState

                # Upsert the Subscription row immediately — we have all data here
                sub_id = data.get("subscription")
                result2 = await db.execute(
                    select(Subscription).where(Subscription.user_id == user_id)
                )
                existing_sub = result2.scalar_one_or_none()
                if existing_sub:
                    existing_sub.stripe_customer_id = customer_id
                    if sub_id:
                        existing_sub.stripe_subscription_id = sub_id
                    if existing_sub.status not in ("active", "trialing"):
                        existing_sub.status = "trialing"
                else:
                    new_sub = Subscription(
                        user_id=user_id,
                        stripe_customer_id=customer_id,
                        stripe_subscription_id=sub_id,
                        status="trialing",
                    )
                    db.add(new_sub)

                was_awaiting = user.onboarding_state == OnboardingState.AWAITING_SUBSCRIPTION
                user.onboarding_complete = True
                if was_awaiting:
                    # State will be advanced to PLAN_REVIEW inside _deliver_plan_after_subscription
                    pass
                else:
                    user.onboarding_state = OnboardingState.DONE
                await db.commit()

                posthog.capture("subscription_activated", distinct_id=str(user.id))

                # If user was waiting for subscription to get their plan, deliver it now
                if was_awaiting and user.linq_chat_id:
                    from app.services.onboarding_chat import _deliver_plan_after_subscription
                    from app.database import async_session
                    import asyncio

                    _chat_id = user.linq_chat_id
                    _user_id = str(user.id)

                    async def _deliver_with_fresh_session():
                        async with async_session() as fresh_db:
                            result = await fresh_db.execute(select(User).where(User.id == _user_id))
                            fresh_user = result.scalar_one_or_none()
                            if fresh_user:
                                await _deliver_plan_after_subscription(fresh_user, _chat_id, fresh_db)

                    asyncio.create_task(_deliver_with_fresh_session())

                # Send welcome iMessage (only if not delivering plan — plan delivery has its own message)
                elif user.linq_chat_id:
                    from app.services import linq as linq_svc
                    try:
                        welcome = (
                            f"You're in, {user.name}! 🎉\n\n"
                            "I'm your Kano coach and I'll be texting you right here on iMessage. "
                            "Let's get to work 💪"
                        )
                        await linq_svc.send_message(user.linq_chat_id, welcome)
                    except Exception as e:
                        print(f"[STRIPE WEBHOOK] Failed to send welcome iMessage: {e}")

    elif event["type"] == "customer.subscription.created":
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
            # Upsert (checkout.session.completed may have already created the row)
            result = await db.execute(
                select(Subscription).where(Subscription.user_id == user_id)
            )
            sub = result.scalar_one_or_none()

            from datetime import datetime
            period_end = None
            ts = data.get("current_period_end")
            if ts:
                period_end = datetime.utcfromtimestamp(ts)

            # Only update status if not already at a "better" state
            # (avoid overwriting trialing → incomplete due to event ordering)
            _status_rank = {"incomplete": 0, "incomplete_expired": 0, "trialing": 2, "active": 3, "past_due": 1, "canceled": -1}
            if sub:
                _new_status = data["status"]
                if _status_rank.get(_new_status, 0) >= _status_rank.get(sub.status, 0):
                    sub.status = _new_status
                sub.stripe_customer_id = data["customer"]
                sub.stripe_subscription_id = data["id"]
                if period_end:
                    sub.current_period_end = period_end
            else:
                sub = Subscription(
                    user_id=user_id,
                    stripe_customer_id=data["customer"],
                    stripe_subscription_id=data["id"],
                    status=data["status"],
                    current_period_end=period_end,
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
            prev_status = sub.status
            new_status = data.get("status", sub.status)
            sub.status = new_status
            if data.get("current_period_end"):
                from datetime import datetime
                sub.current_period_end = datetime.utcfromtimestamp(data["current_period_end"])
            await db.commit()
            print(f"[STRIPE WEBHOOK] {event['type']} sub={data['id']} prev={prev_status} new={new_status} user={sub.user_id}", flush=True)
            if event["type"] == "customer.subscription.deleted":
                posthog.capture("subscription_cancelled", distinct_id=str(sub.user_id))

            # Paywall flow: subscription is now trialing/active. Trigger plan
            # generation if user is still awaiting it. The Redis lock inside
            # _generate_plan_post_subscription guards against duplicate runs,
            # so we can call this on every relevant event without worrying about
            # which exact transition fired or in what order.
            if (
                event["type"] == "customer.subscription.updated"
                and new_status in ("trialing", "active")
            ):
                from app.models.user import OnboardingState
                u_result = await db.execute(select(User).where(User.id == sub.user_id))
                u = u_result.scalar_one_or_none()
                if u and u.onboarding_state == OnboardingState.AWAITING_SUBSCRIPTION:
                    print(f"[STRIPE WEBHOOK] triggering plan generation for user {u.id} ({u.name})", flush=True)
                    from app.services.onboarding_chat import _generate_plan_post_subscription
                    from app.database import async_session
                    import asyncio as _asyncio

                    _user_id = str(u.id)

                    async def _gen_with_fresh_session():
                        try:
                            async with async_session() as fresh_db:
                                r = await fresh_db.execute(select(User).where(User.id == _user_id))
                                fresh_u = r.scalar_one_or_none()
                                if fresh_u:
                                    await _generate_plan_post_subscription(fresh_u, fresh_db)
                        except Exception as ex:
                            import traceback
                            print(f"[STRIPE WEBHOOK] plan generation task failed for {_user_id}: {ex}", flush=True)
                            traceback.print_exc()

                    _asyncio.create_task(_gen_with_fresh_session())
                    posthog.capture("subscription_activated", distinct_id=str(u.id))

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
                posthog.capture("payment_failed", distinct_id=str(sub.user_id))

    return {"ok": True}
