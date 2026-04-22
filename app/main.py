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
    # Startup
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

# Routes
from app.api import health, auth, users, onboarding, training_plans, webhooks, payments

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(onboarding.router)
app.include_router(training_plans.router)
app.include_router(webhooks.router)
app.include_router(payments.router)
