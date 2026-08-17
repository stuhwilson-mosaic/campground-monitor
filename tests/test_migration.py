"""Tests for the one-time migration UserStore runs on first boot."""
import json
import os

import pytest

from app.user_store import UserStore


def _write_legacy_monitors(data_dir, monitors, defaults=None):
    """Write a pre-migration monitors.json: rows with no `owner`, top-level `defaults`."""
    path = os.path.join(data_dir, "monitors.json")
    payload = {"monitors": monitors}
    if defaults is not None:
        payload["defaults"] = defaults
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def test_migration_backfills_owner_on_legacy_rows(tmp_data_dir):
    _write_legacy_monitors(tmp_data_dir, [
        {"id": "m1", "name": "Yose 1"},
        {"id": "m2", "name": "Yose 2"},
    ])
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    with open(os.path.join(tmp_data_dir, "monitors.json")) as f:
        data = json.load(f)
    assert all(m["owner"] == "stu" for m in data["monitors"])


def test_migration_preserves_existing_owner(tmp_data_dir):
    """Rows that already have an owner are left alone."""
    _write_legacy_monitors(tmp_data_dir, [
        {"id": "m1", "name": "Yose 1", "owner": "alice"},
        {"id": "m2", "name": "Yose 2"},
    ])
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    with open(os.path.join(tmp_data_dir, "monitors.json")) as f:
        data = json.load(f)
    owners = {m["id"]: m["owner"] for m in data["monitors"]}
    assert owners == {"m1": "alice", "m2": "stu"}


def test_migration_lifts_defaults_into_bootstrap_admin(tmp_data_dir):
    _write_legacy_monitors(
        tmp_data_dir,
        [{"id": "m1", "name": "Yose 1"}],
        defaults={"email_to": "a@b.com", "ntfy_topic": "topic", "poll_interval_seconds": 60},
    )
    store = UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    stu = store.get("stu")
    assert stu.defaults["email_to"] == "a@b.com"
    assert stu.defaults["ntfy_topic"] == "topic"
    # And the top-level defaults key is gone from monitors.json:
    with open(os.path.join(tmp_data_dir, "monitors.json")) as f:
        data = json.load(f)
    assert "defaults" not in data


def test_migration_writes_backup_before_mutating(tmp_data_dir):
    _write_legacy_monitors(tmp_data_dir, [{"id": "m1"}], defaults={"email_to": "x"})
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    backup = os.path.join(tmp_data_dir, "monitors.pre-multiuser.json")
    assert os.path.exists(backup)
    with open(backup) as f:
        legacy = json.load(f)
    # Backup retains the pre-migration shape.
    assert "defaults" in legacy
    assert "owner" not in legacy["monitors"][0]


def test_migration_is_idempotent(tmp_data_dir):
    _write_legacy_monitors(tmp_data_dir, [{"id": "m1"}])
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    with open(os.path.join(tmp_data_dir, "monitors.json")) as f:
        data = json.load(f)
    # Owner field still present once, defaults still gone, no duplicate users.
    assert data["monitors"][0]["owner"] == "stu"
    assert "defaults" not in data


def test_migration_skipped_when_no_monitors_file(tmp_data_dir):
    """If monitors.json doesn't exist yet, migration is a no-op (no crash)."""
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    # No exceptions; no monitors.json side effect.
    assert not os.path.exists(os.path.join(tmp_data_dir, "monitors.pre-multiuser.json"))
