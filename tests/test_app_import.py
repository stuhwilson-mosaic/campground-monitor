"""Importing the app must have no side effects on DATA_DIR.

app/main.py historically ended with a module-level `app = create_app()`, which
constructed a UserStore and MonitorManager against whatever config.DATA_DIR
happened to be at import time. With DATA_DIR unset that is the live ./data
directory, and this really did seed data/users.json with the default password
during the May 2026 multi-user rollout.

These run the import in a SUBPROCESS on purpose. An earlier version purged
`app.*` from sys.modules in-process, which broke five tests in
test_monitor_manager.py: that module binds MonitorManager at collection time,
so after a purge its class globals referred to one module object while
`patch("app.monitor_manager.check_permit")` patched a different, freshly
imported one, and the patches silently stopped taking effect. A subprocess is
also the more faithful test, since "a fresh interpreter imports app.main" is
exactly the scenario being guarded.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_app_main_in_subprocess(data_dir, ridb_dir):
    """Import app.main in a clean interpreter. Returns the CompletedProcess."""
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "RIDB_DIR": str(ridb_dir),
        "AUTH_USERNAME": "throwaway",
        "AUTH_PASSWORD": "throwaway-pw",
    }
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_importing_app_main_does_not_write_to_data_dir(tmp_path, sample_ridb_dir):
    """Importing app.main must not create files in DATA_DIR.

    Fails before the fix because the module-level `app = create_app()` builds a
    UserStore and a MonitorManager, writing users.json and monitors.json.
    """
    data_dir = tmp_path / "import-probe-data"
    data_dir.mkdir()

    result = _import_app_main_in_subprocess(data_dir, sample_ridb_dir)
    assert result.returncode == 0, f"import failed:\n{result.stderr}"

    written = sorted(p.name for p in data_dir.iterdir())
    assert written == [], (
        f"importing app.main wrote {written} into DATA_DIR; "
        "the app must only be constructed by an explicit create_app() call"
    )


def test_app_main_defines_no_module_level_app():
    """The `app = create_app()` line must not come back.

    Safe to import directly now: with the factory pattern in place, importing
    app.main has no side effects, so this needs no subprocess and no purge.
    """
    import app.main

    assert not hasattr(app.main, "app"), (
        "app/main.py defines a module-level `app`, which reintroduces the "
        "import-time construction against config.DATA_DIR"
    )


def test_app_main_exposes_create_app_factory():
    """app.main must expose create_app for uvicorn's --factory entrypoint."""
    from app.main import create_app

    assert callable(create_app)


def test_dockerfile_uses_the_factory_entrypoint():
    """The container CMD must match the factory pattern main.py now requires.

    Without --factory, uvicorn would look for a module-level `app` that no
    longer exists and the container would fail to start.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd_line = next(l for l in dockerfile.splitlines() if l.startswith("CMD"))

    assert "app.main:create_app" in cmd_line, cmd_line
    assert "--factory" in cmd_line, cmd_line
