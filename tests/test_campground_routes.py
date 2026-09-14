"""Browse routes are authenticated, bounded reads; wizard handoff creates nothing."""
import json
import re
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def browse_client(tmp_path, sample_ridb_dir, monkeypatch):
    from app import config
    from app.main import create_app
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(config, 'RIDB_DIR', sample_ridb_dir)
    monkeypatch.setattr(config, 'AUTH_USERNAME', 'browser')
    monkeypatch.setattr(config, 'AUTH_PASSWORD', 'testpass')
    return TestClient(create_app(), follow_redirects=False)


def login(client):
    client.post('/login', data={'username': 'browser', 'password': 'testpass'})


def trip():
    today = date.today()
    return {'check_in': today.isoformat(), 'check_out': (today + timedelta(days=2)).isoformat()}


def test_browsing_requires_auth_before_requesting_data(browse_client):
    for path in ['/campgrounds', '/campgrounds/232450']:
        response = browse_client.get(path)
        assert response.status_code == 303
        assert response.headers['location'] == '/login'
    response = browse_client.get('/api/campgrounds/232450/availability', params=trip())
    assert response.status_code == 401


def test_campground_search_shows_enabled_campgrounds_and_links(browse_client):
    login(browse_client)
    response = browse_client.get('/campgrounds', params={'q': 'Yosemite', 'state': 'CA'})
    assert response.status_code == 200
    assert 'UPPER PINES' in response.text and 'LOWER PINES' in response.text
    assert '/campgrounds/232450' in response.text
    assert 'HALF DOME' not in response.text


@pytest.mark.parametrize('fid', ['999003', '999001', '999002', '111111', 'not-a-number'])
def test_invalid_or_ineligible_facilities_never_reach_the_upstream(browse_client, fid, monkeypatch):
    login(browse_client)
    def unexpected(*args, **kwargs):
        pytest.fail('Invalid facility reached the upstream')
    if hasattr(browse_client.app.state, 'availability'):
        monkeypatch.setattr(browse_client.app.state.availability, '_fetcher', unexpected)
    response = browse_client.get(f'/api/campgrounds/{fid}/availability', params=trip())
    assert response.status_code == 404


@pytest.mark.parametrize('change', [
    {'check_in': 'not-a-date'}, {'check_out': '2020-01-01'},
    {'check_out': (date.today() + timedelta(days=32)).isoformat()},
])
def test_invalid_dates_never_reach_the_upstream(browse_client, change, monkeypatch):
    login(browse_client)
    def unexpected(*args, **kwargs):
        pytest.fail('Invalid dates reached the upstream')
    if hasattr(browse_client.app.state, 'availability'):
        monkeypatch.setattr(browse_client.app.state.availability, '_fetcher', unexpected)
    response = browse_client.get('/api/campgrounds/232450/availability', params=trip() | change)
    assert response.status_code == 422


def test_availability_api_returns_statuses_and_does_not_persist_a_monitor(browse_client, monkeypatch):
    login(browse_client)
    assert hasattr(browse_client.app.state, 'availability'), 'Browser availability service is not wired'
    first = date.today()
    raw = {'campsites': {'10': {'site': 'A10', 'loop': 'A', 'campsite_type': 'TENT', 'availabilities': {
        (first + timedelta(days=i)).isoformat() + 'T00:00:00Z': 'Available' for i in range(2)
    }}}}
    monkeypatch.setattr(browse_client.app.state.availability, '_fetcher', lambda *_: raw)
    response = browse_client.get('/api/campgrounds/232450/availability', params=trip())
    assert response.status_code == 200
    data = response.json()
    assert data['facility']['name'] == 'UPPER PINES'
    assert data['sites'][0]['available_for_stay'] is True
    assert data['sites'][0]['lat'] is None
    assert data['sites'][0]['availability'][first.isoformat()] == 'Available'
    assert data['fetched_at'] and data['stale'] is False
    assert response.headers['cache-control'] == 'no-store'
    assert browse_client.app.state.manager.list_monitors() == []


def test_upstream_failure_is_service_unavailable_not_zero_open_sites(browse_client, monkeypatch):
    login(browse_client)
    assert hasattr(browse_client.app.state, 'availability'), 'Browser availability service is not wired'
    def offline(*_):
        raise OSError('private diagnostic')
    monkeypatch.setattr(browse_client.app.state.availability, '_fetcher', offline)
    response = browse_client.get('/api/campgrounds/232450/availability', params=trip())
    assert response.status_code == 503
    assert 'private diagnostic' not in response.text
    assert 'sites' not in response.json()


def test_monitor_handoff_uses_catalog_and_dates_without_creating_a_monitor(browse_client):
    login(browse_client)
    response = browse_client.get('/monitors/new', params={'campground': '232450', **trip(), 'name': 'Untrusted name'})
    assert response.status_code == 200
    match = re.search(r'<script id="wizard-seed"[^>]*>(.*?)</script>', response.text, re.S)
    seed = json.loads(match.group(1))
    assert seed['facility_ids'] == ['232450']
    assert seed['facility_names'] == {'232450': 'UPPER PINES'}
    assert seed['check_in'] == trip()['check_in']
    assert seed['check_out'] == trip()['check_out']
    assert '/monitors/new/step3' in response.text
    assert browse_client.app.state.manager.list_monitors() == []


def test_detail_shell_escapes_catalog_names_and_embedded_seed(browse_client):
    login(browse_client)
    browse_client.app.state.catalog._facilities['232450']['FacilityName'] = '</script><script>alert(1)</script>'
    response = browse_client.get('/campgrounds/232450')
    assert response.status_code == 200
    assert '<script>alert(1)</script>' not in response.text


def test_wizard_rejects_ineligible_campground_handoff(browse_client):
    login(browse_client)
    response = browse_client.get('/monitors/new', params={'campground': '999003', **trip()})
    assert response.status_code == 404


def test_browser_waiters_leave_worker_capacity_for_monitor_checks(browse_client, monkeypatch):
    """Coalesced viewers wait on the loop, not every thread in the shared executor."""
    import httpx
    login(browse_client)
    started, release = threading.Event(), threading.Event()
    def fetch(*_):
        started.set()
        assert release.wait(5)
        return {'campsites': {}}
    monkeypatch.setattr(browse_client.app.state.availability, '_fetcher', fetch)

    async def exercise():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=8) as executor:
            loop.set_default_executor(executor)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browse_client.app),
                                         base_url='http://testserver', cookies=browse_client.cookies) as client:
                viewers = [asyncio.create_task(client.get('/api/campgrounds/232450/availability', params=trip())) for _ in range(16)]
                try:
                    assert await asyncio.to_thread(started.wait, 3)
                    # The heartbeat queues after the viewers' work on the same pool.
                    heartbeat = await asyncio.wait_for(asyncio.to_thread(lambda: 'monitor can run'), timeout=0.5)
                    assert heartbeat == 'monitor can run'
                finally:
                    release.set()
                    responses = await asyncio.gather(*viewers)
                assert all(response.status_code == 200 for response in responses)
    asyncio.run(exercise())


def test_long_monitor_dates_do_not_make_its_browse_link_unusable(browse_client):
    login(browse_client)
    start = date.today()
    browse_client.app.state.manager.add_monitor({
        'id': 'long-stay', 'owner': 'browser', 'name': 'Long trip', 'status': 'stopped',
        'facilities': [{'id': '232450', 'name': 'UPPER PINES', 'type': 'Campground'}],
        'check_in': start.isoformat(), 'check_out': (start + timedelta(days=39)).isoformat(),
    })
    dashboard = browse_client.get('/dashboard')
    match = re.search(r'href="(/campgrounds/232450[^"]*)"', dashboard.text)
    assert match
    assert browse_client.get(match.group(1)).status_code == 200
