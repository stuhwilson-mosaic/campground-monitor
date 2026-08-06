"""FastAPI application factory for the campground monitor web UI."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config
from app.auth import get_current_user
from app.monitor_manager import MonitorManager
from app.ridb import RIDBCatalog
from app.telemetry import TelemetryStore
from app.user_store import UserStore

log = logging.getLogger(__name__)
from app.routes import auth_routes, dashboard, wizard, api, logs, admin


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""

    # Without this the app has no logging handler at all, so Python's
    # last-resort handler emits WARNING and above only, and every log.info in
    # the codebase goes nowhere. Root stays at WARNING deliberately: raising it
    # globally would emit a line per facility per poll cycle, which at a 30s
    # interval is a lot of volume against an unrotated container log. Only the
    # retention reporter is opted up to INFO.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(name)s: %(message)s",
    )
    logging.getLogger(__name__).setLevel(logging.INFO)

    # UserStore MUST be constructed before MonitorManager so the migration
    # (Task 8) backfills `owner` on monitors before any other code reads them.
    user_store = UserStore(
        config.DATA_DIR,
        bootstrap_username=config.AUTH_USERNAME,
        bootstrap_password=config.AUTH_PASSWORD,
    )
    telemetry = TelemetryStore(config.DATA_DIR)
    manager = MonitorManager(config.DATA_DIR, telemetry=telemetry)
    catalog = RIDBCatalog(config.RIDB_DIR)
    templates = Jinja2Templates(directory="app/templates")

    async def _prune_telemetry_forever():
        """Drop check rows past the retention window, at boot then daily.

        The deleted count is logged every run on purpose: a prune task that
        dies silently is otherwise discovered as a full disk.
        """
        while True:
            try:
                removed = await asyncio.to_thread(telemetry.prune_checks)
                log.info("telemetry retention: pruned %d check rows", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telemetry retention pass failed")
            await asyncio.sleep(24 * 60 * 60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pruner = asyncio.create_task(_prune_telemetry_forever(), name="telemetry-prune")
        # Best-effort and backgrounded: it makes outbound calls, and startup
        # must not wait on recreation.gov being reachable.
        backfill = asyncio.create_task(
            manager.backfill_division_names(), name="division-name-backfill"
        )
        await manager.resume_all()
        yield
        for task in (pruner, backfill):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("background task failed during shutdown")
        await manager.stop_all()
        telemetry.close()

    app = FastAPI(title="Campground Monitor", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    app.state.user_store = user_store
    app.state.manager = manager
    app.state.telemetry = telemetry
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


# NOTE: deliberately no module-level `app = create_app()`.
#
# Constructing the app at import time builds a UserStore and MonitorManager
# against whatever config.DATA_DIR happens to be at that moment. With DATA_DIR
# unset that is the live ./data directory, so merely importing this module from
# a test could seed data/users.json with the default bootstrap password. That
# is not hypothetical: it happened during the May 2026 multi-user rollout.
#
# The entrypoint uses uvicorn's factory mode instead:
#     uvicorn app.main:create_app --factory
# See tests/test_app_import.py, which fails if the side effect comes back.
