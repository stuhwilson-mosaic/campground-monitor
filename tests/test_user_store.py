"""Tests for app.user_store — JSON-backed user CRUD with bcrypt verification."""
import json
import os

import pytest

from app.user_store import UserStore


@pytest.fixture
def store(tmp_data_dir):
    """A fresh UserStore on a tmp data dir, no env-var bootstrap."""
    return UserStore(tmp_data_dir, bootstrap_username=None, bootstrap_password=None)


def test_add_and_get_user(store):
    """add() creates a user; get() returns it without the password hash."""
    user = store.add(username="alice", password="hunter2", role="user")
    assert user.username == "alice"
    assert user.role == "user"
    fetched = store.get("alice")
    assert fetched.username == "alice"
    assert fetched.role == "user"
    # The User dataclass exposed to callers must not leak the password hash.
    assert not hasattr(fetched, "password_hash")


def test_get_missing_user_returns_none(store):
    assert store.get("nobody") is None


def test_verify_correct_password(store):
    store.add(username="alice", password="hunter2", role="user")
    user = store.verify("alice", "hunter2")
    assert user is not None
    assert user.username == "alice"


def test_verify_wrong_password(store):
    store.add(username="alice", password="hunter2", role="user")
    assert store.verify("alice", "wrong") is None


def test_verify_unknown_user(store):
    assert store.verify("nobody", "anything") is None


def test_verify_is_case_insensitive_on_username(store):
    store.add(username="Alice", password="hunter2", role="user")
    assert store.verify("alice", "hunter2") is not None
    assert store.verify("ALICE", "hunter2") is not None


def test_list_users_returns_all(store):
    store.add(username="a", password="p1", role="admin")
    store.add(username="b", password="p2", role="user")
    names = sorted(u.username for u in store.list_users())
    assert names == ["a", "b"]


def test_delete_user(store):
    store.add(username="a", password="p1", role="user")
    assert store.delete("a") is True
    assert store.get("a") is None
    assert store.delete("a") is False  # idempotent-ish: already gone returns False


def test_update_password(store):
    store.add(username="a", password="old", role="user")
    assert store.update_password("a", "new") is True
    assert store.verify("a", "old") is None
    assert store.verify("a", "new") is not None


def test_update_role(store):
    store.add(username="a", password="p", role="user")
    assert store.update_role("a", "admin") is True
    assert store.get("a").role == "admin"


def test_update_role_rejects_invalid(store):
    store.add(username="a", password="p", role="user")
    with pytest.raises(ValueError):
        store.update_role("a", "superuser")


def test_update_defaults_persists(store):
    store.add(username="a", password="p", role="user")
    store.update_defaults("a", {"email_to": "a@b.com", "ntfy_topic": "topic1", "poll_interval_seconds": 60})
    fresh = store.get("a")
    assert fresh.defaults["email_to"] == "a@b.com"
    assert fresh.defaults["ntfy_topic"] == "topic1"
    assert fresh.defaults["poll_interval_seconds"] == 60


def test_add_rejects_duplicate_after_normalization(store):
    store.add(username="Alice", password="p", role="user")
    with pytest.raises(ValueError):
        store.add(username="alice", password="p", role="user")


def test_bootstrap_seeds_admin_when_empty(tmp_data_dir):
    """If users.json has no users, the bootstrap creds seed an admin."""
    store = UserStore(
        tmp_data_dir,
        bootstrap_username="stu",
        bootstrap_password="changeme",
    )
    user = store.get("stu")
    assert user is not None
    assert user.role == "admin"
    assert store.verify("stu", "changeme") is not None


def test_bootstrap_is_idempotent(tmp_data_dir):
    """Calling UserStore twice on the same dir doesn't duplicate the admin."""
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw1")
    UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw2")
    # Second call must not overwrite the existing admin's password.
    second = UserStore(
        tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw3"
    )
    assert len(second.list_users()) == 1
    # First-write wins — original password still valid.
    assert second.verify("stu", "pw1") is not None
    assert second.verify("stu", "pw2") is None


def test_bootstrap_skipped_when_credentials_missing(tmp_data_dir):
    store = UserStore(tmp_data_dir, bootstrap_username=None, bootstrap_password=None)
    assert store.list_users() == []


# ── Favorites ─────────────────────────────────────────────────────────────────

def _fav():
    return {
        "label": "Tuolumne + Porcupine",
        "rec_area_id": "2991",
        "rec_area_name": "Yosemite National Park",
        "facilities": [
            {"id": "232448", "name": "Tuolumne Meadows", "type": "Campground"},
            {"id": "10083831", "name": "Porcupine Flat", "type": "Campground"},
        ],
    }


def test_favorites_default_to_empty_for_existing_records(store):
    """Records predating favorites must read as [] rather than KeyError."""
    store.add(username="alice", password="pw", role="user")
    assert store.list_favorites("alice") == []


def test_add_favorite_round_trips(store):
    store.add(username="alice", password="pw", role="user")
    saved = store.add_favorite("alice", _fav())
    assert saved["id"]
    favs = store.list_favorites("alice")
    assert len(favs) == 1
    assert favs[0]["label"] == "Tuolumne + Porcupine"
    assert favs[0]["facilities"][0]["id"] == "232448"


def test_add_favorite_persists_across_instances(store, tmp_data_dir):
    from app.user_store import UserStore
    store.add(username="alice", password="pw", role="user")
    store.add_favorite("alice", _fav())
    fresh = UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    assert len(fresh.list_favorites("alice")) == 1


def test_delete_favorite(store):
    store.add(username="alice", password="pw", role="user")
    fav = store.add_favorite("alice", _fav())
    assert store.delete_favorite("alice", fav["id"]) is True
    assert store.list_favorites("alice") == []


def test_delete_unknown_favorite_returns_false(store):
    store.add(username="alice", password="pw", role="user")
    assert store.delete_favorite("alice", "nope") is False


def test_favorites_are_per_user(store):
    """One user's favorites must never appear in another's list."""
    store.add(username="alice", password="pw", role="user")
    store.add(username="bob", password="pw", role="user")
    store.add_favorite("alice", _fav())
    assert store.list_favorites("bob") == []
    assert len(store.list_favorites("alice")) == 1


def test_get_favorite_scoped_to_owner(store):
    store.add(username="alice", password="pw", role="user")
    store.add(username="bob", password="pw", role="user")
    fav = store.add_favorite("alice", _fav())
    assert store.get_favorite("alice", fav["id"]) is not None
    assert store.get_favorite("bob", fav["id"]) is None


def test_add_favorite_for_unknown_user_returns_none(store):
    assert store.add_favorite("ghost", _fav()) is None
