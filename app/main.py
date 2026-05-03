from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "terms.html")
