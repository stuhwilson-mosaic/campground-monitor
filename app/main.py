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
from app.routes import auth_routes, dashboard, wizard, api, logs


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""

    manager = MonitorManager(config.DATA_DIR)
    catalog = RIDBCatalog(config.RIDB_DIR)
    templates = Jinja2Templates(directory="app/templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.resume_all()
        yield
        await manager.stop_all()

    app = FastAPI(title="Campground Monitor", lifespan=lifespan)

    # Mount static files
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # Attach shared state
    app.state.manager = manager
    app.state.catalog = catalog
    app.state.templates = templates

    # Include routers
    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(wizard.router)
    app.include_router(api.router)
    app.include_router(logs.router)

    @app.get("/")
    async def index(request: Request):
        """Root: redirect to /login if not authenticated, else to /dashboard."""
        user = get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        # Dashboard route added in Task 7; forward there for now
        return RedirectResponse(url="/dashboard", status_code=303)

    return app


# Module-level app for uvicorn
app = create_app()
