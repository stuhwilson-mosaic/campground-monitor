"""FastAPI application factory for the campground monitor web UI."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config
from app.auth import get_current_user
from app.monitor_manager import MonitorManager
from app.ridb import RIDBCatalog
from app.user_store import UserStore
from app.routes import auth_routes, dashboard, wizard, api, logs, admin


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""

    # UserStore MUST be constructed before MonitorManager so the migration
    # (Task 8) backfills `owner` on monitors before any other code reads them.
    user_store = UserStore(
        config.DATA_DIR,
        bootstrap_username=config.AUTH_USERNAME,
        bootstrap_password=config.AUTH_PASSWORD,
    )
    manager = MonitorManager(config.DATA_DIR)
    catalog = RIDBCatalog(config.RIDB_DIR)
    templates = Jinja2Templates(directory="app/templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.resume_all()
        yield
        await manager.stop_all()

    app = FastAPI(title="Campground Monitor", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    app.state.user_store = user_store
    app.state.manager = manager
    app.state.catalog = catalog
    app.state.templates = templates

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(wizard.router)
    app.include_router(api.router)
    app.include_router(logs.router)
    app.include_router(admin.router)

    @app.get("/")
    async def index(request: Request):
        user = get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return RedirectResponse(url="/dashboard", status_code=303)

    return app


app = create_app()
