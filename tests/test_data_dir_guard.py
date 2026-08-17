"""The test suite must never be able to write to the live data/ directory.

data/monitors.json is rewritten by the running container every 12-18 seconds and
holds live monitor state. A test run that points DATA_DIR at it would race those
writes and could clobber real monitors.
"""
from pathlib import Path

from tests.conftest import LIVE_DATA_DIR, resolves_to_live_data_dir


def test_absolute_path_to_repo_data_is_rejected():
    assert resolves_to_live_data_dir(str(LIVE_DATA_DIR)) is True


def test_relative_dot_data_from_repo_root_is_rejected(monkeypatch):
    """'./data' resolves against CWD, which is the repo root in a normal run."""
    monkeypatch.chdir(LIVE_DATA_DIR.parent)
    assert resolves_to_live_data_dir("./data") is True


def test_unset_data_dir_is_rejected(monkeypatch):
    """Unset means config.DATA_DIR falls back to './data', i.e. the live dir."""
    monkeypatch.chdir(LIVE_DATA_DIR.parent)
    assert resolves_to_live_data_dir(None) is True
    assert resolves_to_live_data_dir("") is True


def test_temp_dir_is_allowed(tmp_path):
    assert resolves_to_live_data_dir(str(tmp_path / "data")) is False


def test_similarly_named_dir_elsewhere_is_allowed(tmp_path):
    """A directory that merely ends in 'data' must not trip the guard."""
    other = tmp_path / "campground-monitor" / "data"
    other.mkdir(parents=True)
    assert resolves_to_live_data_dir(str(other)) is False


def test_guard_survives_a_nonexistent_path(tmp_path):
    """Resolving a path that does not exist yet must not raise."""
    assert resolves_to_live_data_dir(str(tmp_path / "nope" / "data")) is False


def test_live_data_dir_points_at_the_real_repo_data():
    """Sanity: the constant the guard compares against is the actual repo dir."""
    assert LIVE_DATA_DIR.name == "data"
    assert (LIVE_DATA_DIR.parent / "app" / "main.py").exists()
    assert LIVE_DATA_DIR == Path(LIVE_DATA_DIR).resolve()
