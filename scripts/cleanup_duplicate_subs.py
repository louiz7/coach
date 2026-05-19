"""Find Stripe customers with multiple live subscriptions and cancel duplicates.

Run inside the api container:
  docker exec -it coach-api-1 python -m scripts.cleanup_duplicate_subs           # dry run
  docker exec -it coach-api-1 python -m scripts.cleanup_duplicate_subs --apply   # actually cancel

Keeps the OLDEST trialing/active sub per customer (so the user keeps their
original trial), cancels the rest. Also issues prorated refunds for any
invoices charged on the duplicates in the last 24h.
"""
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import stripe

from app.config import settings

stripe.api_key = settings.STRIPE_SECRET_KEY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually cancel/refund")
    parser.add_argument("--refund-since-hours", type=int, default=72,
                        help="refund duplicate-sub invoices paid within this window")
    args = parser.parse_args()

    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print("Scanning all Stripe subscriptions...")

    by_customer: dict[str, list] = defaultdict(list)
    total = 0
    for sub in stripe.Subscription.list(status="all", limit=100).auto_paging_iter():
        by_customer[sub.customer].append(sub)
        total += 1
    print(f"Loaded {total} subscriptions across {len(by_customer)} customers")

    live = {"trialing", "active", "past_due"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.refund_since_hours)

    customers_with_dupes = 0
    total_cancelled = 0
    total_refunded_cents = 0

    for customer_id, subs in by_customer.items():
        live_subs = [s for s in subs if s.status in live]
        if len(live_subs) <= 1:
            # Also clean up any orphan incomplete subs for customers with one live one
            if len(live_subs) >= 1:
                for s in subs:
                    if s.status == "incomplete":
                        print(f"  customer {customer_id}: orphan incomplete {s.id}")
                        if args.apply:
                            try:
                                stripe.Subscription.cancel(s.id)
                            except Exception as e:
                                print(f"    cancel failed: {e}")
            continue

        customers_with_dupes += 1
        # Keep the oldest live sub, cancel the rest
        live_subs.sort(key=lambda s: s.created)
        keep = live_subs[0]
        dupes = live_subs[1:]
        cust = stripe.Customer.retrieve(customer_id)
        email = getattr(cust, "email", "?")
        print(f"\nCustomer {customer_id} ({email}): {len(live_subs)} live subs")
        print(f"  KEEP: {keep.id} status={keep.status} created={datetime.fromtimestamp(keep.created)}")

        for dupe in dupes:
            created_dt = datetime.fromtimestamp(dupe.created)
            print(f"  CANCEL: {dupe.id} status={dupe.status} created={created_dt}")
            total_cancelled += 1

            # Refund any invoices paid on this duplicate subscription within window.
            try:
                invoices = stripe.Invoice.list(subscription=dupe.id, status="paid", limit=20)
                for inv in invoices.auto_paging_iter():
                    paid_at = datetime.fromtimestamp(inv.created, tz=timezone.utc)
                    if paid_at < cutoff:
                        continue
                    charge_id = inv.charge
                    if not charge_id:
                        continue
                    amount = inv.amount_paid
                    print(f"    REFUND invoice={inv.id} charge={charge_id} amount={amount/100:.2f} {inv.currency.upper()}")
                    total_refunded_cents += amount
                    if args.apply:
                        try:
                            stripe.Refund.create(charge=charge_id, reason="duplicate")
                        except Exception as e:
                            print(f"      refund failed: {e}")
            except Exception as e:
                print(f"    invoice scan failed: {e}")

            if args.apply:
                try:
                    stripe.Subscription.cancel(dupe.id, prorate=False, invoice_now=False)
                except Exception as e:
                    print(f"    cancel failed: {e}")

        # Cancel any remaining orphan incompletes too
        for s in subs:
            if s.status in ("incomplete", "incomplete_expired"):
                print(f"  CANCEL orphan incomplete: {s.id}")
                if args.apply:
                    try:
                        stripe.Subscription.cancel(s.id)
                    except Exception as e:
                        print(f"    cancel failed: {e}")

    print("\n=== SUMMARY ===")
    print(f"Customers with duplicates: {customers_with_dupes}")
    print(f"Subscriptions to cancel:   {total_cancelled}")
    print(f"Amount to refund:          {total_refunded_cents/100:.2f}")
    print(f"Mode: {'APPLIED' if args.apply else 'DRY RUN — rerun with --apply to actually do it'}")


if __name__ == "__main__":
    main()
