from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
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
    return templates.TemplateResponse(request, "success.html", {"token": token})


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
                ctx["plan_data"] = plan.plan_json
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
        await db.commit()

        return {"ok": True, "plan_id": str(plan.id)}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "terms.html")
