from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import settings

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startuphow 
    print("Starting up...")
    yield
    # Shutdown
    from app.redis import redis_pool
    await redis_pool.close()
    print("Shut down.")


app = FastAPI(title="Fitness Coach API", version="1.0.0", lifespan=lifespan)

# Static files and templates
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Landing page
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


# Onboarding form page (linked from iMessage chat)
@app.get("/start", response_class=HTMLResponse)
async def start(request: Request) -> HTMLResponse:
    token = request.query_params.get("token", "")
    name = ""
    if token:
        try:
            from app.services.token import verify_onboarding_token
            payload = verify_onboarding_token(token)
            # Quick DB lookup to get the real name
            from app.database import async_session
            from app.models.user import User
            from sqlalchemy import select
            async with async_session() as db:
                result = await db.execute(select(User.name).where(User.phone == payload["phone"]))
                name = result.scalar_one_or_none() or ""
        except Exception:
            pass  # Invalid/expired token — template will fall back to default
    return templates.TemplateResponse(request, "start.html", {"token": token, "name": name})

# Routes
from app.api import health, auth, users, onboarding, training_plans, webhooks, payments, whoop

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(onboarding.router)
app.include_router(training_plans.router)
app.include_router(webhooks.router)
app.include_router(payments.router)
app.include_router(whoop.router)


@app.get("/success", response_class=HTMLResponse)
async def success(request: Request) -> HTMLResponse:
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(request, "success.html", {
        "token": token,
        "sms_number": settings.LINQ_PHONE_NUMBER,
    })


@app.get("/cancel", response_class=HTMLResponse)
async def cancel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "start.html", {"token": "", "name": ""})


@app.get("/plan", response_class=HTMLResponse)
async def plan_view(request: Request) -> HTMLResponse:
    """Token-gated page that displays the user's current training plan in tabular form."""
    token = request.query_params.get("token", "")
    ctx = {
        "plan_data": None,
        "user_name": "",
        "error": None,
        "generated_at": None,
        "plan_id": "",
        "token": token,
    }
    if not token:
        ctx["error"] = "Missing or expired link"
        return templates.TemplateResponse(request, "plan.html", ctx)
    try:
        from app.services.token import verify_onboarding_token
        from app.database import async_session
        from app.models.user import User
        from app.models.training_plan import TrainingPlan
        from sqlalchemy import select
        payload = verify_onboarding_token(token)
        async with async_session() as db:
            r = await db.execute(select(User).where(User.phone == payload["phone"]))
            user = r.scalar_one_or_none()
            if not user:
                ctx["error"] = "User not found"
                return templates.TemplateResponse(request, "plan.html", ctx)
            ctx["user_name"] = user.name or ""
            r2 = await db.execute(
                select(TrainingPlan)
                .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
                .order_by(TrainingPlan.created_at.desc())
            )
            plan = r2.scalars().first()
            if plan:
                pj = plan.plan_json or {}
                # Normalise: support both {"days": [...]} and {"weekly_schedule": [...]}
                days = pj.get("days") or pj.get("weekly_schedule") or []
                # Filter out rest days for the count but keep them in the data
                training_days = [d for d in days if d.get("exercises")]

                # ── Pre-fill PREVIOUS column from progress_entries ──────────
                from app.models.progress_entry import ProgressEntry
                from sqlalchemy import select as sa_select
                from collections import defaultdict

                pe_result = await db.execute(
                    sa_select(ProgressEntry)
                    .where(
                        ProgressEntry.user_id == user.id,
                        ProgressEntry.category == "exercise",
                    )
                    .order_by(ProgressEntry.recorded_at.desc())
                    .limit(200)
                )
                all_entries = pe_result.scalars().all()

                # Build a map: normalised exercise name → most-recent entry
                prev_map: dict = {}
                for entry in all_entries:
                    key = entry.label.strip().lower()
                    if key not in prev_map:
                        val = f"{entry.value}{entry.unit or 'kg'}"
                        if entry.sets and entry.reps:
                            val += f" {entry.sets}×{entry.reps}"
                        prev_map[key] = val

                # Inject into each exercise dict; also fall back to logged_sets
                # stored in the plan itself (plan-page logs land in progress_entries
                # via the PATCH endpoint, but this is a belt-and-braces fallback)
                enriched_days = []
                for day in training_days:
                    enriched_exs = []
                    for ex in day.get("exercises", []):
                        ex_copy = dict(ex)
                        key = ex_copy.get("name", "").strip().lower()
                        prev = prev_map.get(key)
                        if not prev:
                            # Fallback: derive from the exercise's own logged_sets
                            ls = ex_copy.get("logged_sets") or []
                            done = [s for s in ls if s.get("done") and s.get("weight")]
                            if done:
                                prev = f"{done[-1]['weight']}kg"
                                if done[-1].get("reps"):
                                    prev += f" ×{done[-1]['reps']}"
                        ex_copy["previous_performance"] = prev
                        enriched_exs.append(ex_copy)
                    enriched_days.append({**day, "exercises": enriched_exs})

                ctx["plan_data"] = {**pj, "days": enriched_days}
                ctx["generated_at"] = plan.created_at.strftime("%b %d, %Y") if plan.created_at else None
                ctx["plan_id"] = str(plan.id)
    except Exception as e:
        ctx["error"] = str(e)
    return templates.TemplateResponse(request, "plan.html", ctx)


@app.patch("/plan")
async def plan_update(request: Request):
    """Persist user-edited plan. Token-gated, schema-validated, ORM-only.

    409 Conflict is returned if a newer AI-generated plan has superseded the
    one the client was editing — the UI should then show a "fresh plan
    available" banner and reload.
    """
    token = request.query_params.get("token", "")
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    # Parse + validate body strictly
    from app.schemas.plan_edit import PlanUpdateRequest, render_raw_text_from_plan
    from pydantic import ValidationError
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        payload_in = PlanUpdateRequest.model_validate(body)
    except ValidationError as ve:
        raise HTTPException(status_code=422, detail=ve.errors())

    from app.services.token import verify_onboarding_token
    from app.database import async_session
    from app.models.user import User
    from app.models.training_plan import TrainingPlan
    from sqlalchemy import select

    try:
        token_payload = verify_onboarding_token(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

    async with async_session() as db:
        r = await db.execute(select(User).where(User.phone == token_payload["phone"]))
        user = r.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        r2 = await db.execute(
            select(TrainingPlan)
            .where(TrainingPlan.user_id == user.id, TrainingPlan.is_current == True)
            .order_by(TrainingPlan.created_at.desc())
        )
        plan = r2.scalars().first()
        if not plan:
            raise HTTPException(status_code=404, detail="No active plan")

        # Concurrency guard: the user must be editing the currently-active plan
        if str(plan.id) != payload_in.plan_id:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "A newer plan has been generated — please refresh.",
                    "current_plan_id": str(plan.id),
                },
            )

        # Persist via ORM (parameterized → SQL-injection safe)
        plan.plan_json = payload_in.plan_json.model_dump()
        plan.raw_text = render_raw_text_from_plan(payload_in.plan_json)
        plan.updated_by_user = True
        plan.user_edited_at = datetime.utcnow()

        # ── Mirror completed plan-page sets into progress_entries ─────────
        # This ensures PREVIOUS pre-fill and PERFORMANCE_DATA intent both
        # see weights logged on the plan page, not just chat-logged entries.
        from app.models.progress_entry import ProgressEntry as PE
        for day in payload_in.plan_json.days:
            for ex in day.exercises:
                if not ex.logged_sets:
                    continue
                done_sets = [s for s in ex.logged_sets if s.done and s.weight]
                if not done_sets:
                    continue
                # Use the last done-set's weight as the canonical value for this session.
                # We upsert by deleting today's existing entry for the same exercise first
                # to avoid duplicates on multiple "Finish workout" taps in the same day.
                from sqlalchemy import delete as sa_delete, func as sa_func
                await db.execute(
                    sa_delete(PE).where(
                        PE.user_id == user.id,
                        PE.label == ex.name,
                        PE.category == "exercise",
                        sa_func.date(PE.recorded_at) == datetime.utcnow().date(),
                    )
                )
                # Parse weight string → float (strip trailing non-numeric chars)
                import re as _re
                for s in done_sets:
                    w_str = (s.weight or "").strip()
                    m = _re.search(r"[\d.]+", w_str)
                    if not m:
                        continue
                    try:
                        w_val = float(m.group())
                    except ValueError:
                        continue
                    unit = "kg" if "lb" not in w_str.lower() else "lbs"
                    r_val = None
                    if s.reps:
                        reps_m = _re.search(r"\d+", s.reps)
                        r_val = int(reps_m.group()) if reps_m else None
                    db.add(PE(
                        user_id=user.id,
                        category="exercise",
                        label=ex.name,
                        value=w_val,
                        unit=unit,
                        sets=len(done_sets),
                        reps=r_val,
                        notes="logged via plan page",
                    ))
                    # Update fitness profile: PRs + streak + total_workouts_logged
                    try:
                        from app.services.fitness_profile import update_profile_from_workout
                        await update_profile_from_workout(
                            user.id, db,
                            label=ex.name,
                            value=w_val,
                            unit=unit,
                            category="exercise",
                        )
                    except Exception as _pe:
                        print(f"[plan_update profile ERROR] {_pe}")
                    break  # one representative entry per exercise per session

        await db.commit()

        return {"ok": True, "plan_id": str(plan.id)}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "terms.html")


@app.get("/sitemap.xml")
async def sitemap() -> Response:
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://kano.fit/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://kano.fit/terms</loc>
    <changefreq>monthly</changefreq>
    <priority>0.3</priority>
  </url>
  <url>
    <loc>https://kano.fit/privacy</loc>
    <changefreq>monthly</changefreq>
    <priority>0.3</priority>
  </url>
</urlset>"""
    return Response(content=content, media_type="application/xml")


@app.get("/robots.txt")
async def robots() -> PlainTextResponse:
    content = """User-agent: *
Allow: /
Disallow: /plan
Disallow: /api
Disallow: /start
Disallow: /success
Disallow: /cancel

Sitemap: https://kano.fit/sitemap.xml
"""
    return PlainTextResponse(content=content)
