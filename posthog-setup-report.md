<wizard-report>
# PostHog post-wizard report

The wizard has completed a deep integration of PostHog analytics into the Kano FastAPI backend. PostHog is initialized in the FastAPI lifespan context manager (startup/shutdown), with credentials loaded from environment variables. Thirteen server-side events were instrumented across six files, covering the full user lifecycle: first contact via iMessage, conversational onboarding, training plan generation, subscription checkout and activation, and churn signals.

| Event | Description | File |
|---|---|---|
| `user_created` | New user created when they first message Kano via iMessage | `app/workers/message_worker.py` |
| `onboarding_goal_captured` | User stated their fitness goal during conversational onboarding | `app/services/onboarding_chat.py` |
| `onboarding_plan_built` | Initial training plan generated and sent during onboarding | `app/services/onboarding_chat.py` |
| `onboarding_completed` | User completed onboarding and received the payment/trial pitch | `app/services/onboarding_chat.py` |
| `onboarding_form_submitted` | User submitted the web onboarding form | `app/api/onboarding.py` |
| `checkout_session_created` | Stripe checkout session created for subscription | `app/api/onboarding.py` |
| `subscription_activated` | User's subscription confirmed via Stripe webhook | `app/api/payments.py` |
| `subscription_cancelled` | User's subscription cancelled via Stripe webhook | `app/api/payments.py` |
| `payment_failed` | Subscription payment failed via Stripe webhook | `app/api/payments.py` |
| `training_plan_generated` | AI training plan generated or modified for a user | `app/services/training_plan.py` |
| `training_plan_viewed` | User opened their training plan page via token-gated URL | `app/main.py` |
| `training_plan_edited` | User saved edits to their training plan via the web UI | `app/main.py` |
| `message_received` | Inbound iMessage processed for an active (post-onboarding) user | `app/workers/message_worker.py` |

## Next steps

We've built some insights and a dashboard for you to keep an eye on user behavior, based on the events we just instrumented:

- [Analytics basics dashboard](/dashboard/679748)
- [Onboarding conversion funnel](/insights/AARamXsz) — tracks drop-off across the 4-step conversational onboarding flow
- [Checkout to subscription funnel](/insights/ZlvpORYw) — measures how many users who start checkout actually subscribe
- [New users over time](/insights/Lkgh4rT5) — daily new users entering Kano via iMessage
- [Training plan engagement](/insights/l4ooAvyf) — plan views vs. edits to gauge active engagement
- [Subscription churn signals](/insights/Ua0YdkYO) — cancellations and payment failures over time

### Agent skill

We've left an agent skill folder in your project. You can use this context for further agent development when using Claude Code. This will help ensure the model provides the most up-to-date approaches for integrating PostHog.

</wizard-report>
