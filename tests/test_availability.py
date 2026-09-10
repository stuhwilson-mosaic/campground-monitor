"""Browser availability must never turn incomplete data into an open stay."""
import csv
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date

import pytest


def service_module():
    from app import availability
    return availability


def month(sites):
    return {"campsites": {
        sid: {"site": sid, "loop": "A", "campsite_type": "TENT ONLY NONELECTRIC",
              "campsite_reserve_type": "Site-Specific", "hide_external": False,
              "availabilities": {day + "T00:00:00Z": status for day, status in days.items()}}
        for sid, days in sites.items()
    }}


def test_cross_month_stay_requires_the_same_site_and_keeps_unknown_status():
    mod = service_module()
    responses = {
        "2026-09-01T00:00:00.000Z": month({"1": {"2026-09-30": "Available"}, "2": {"2026-09-30": "Reserved"}}),
        "2026-10-01T00:00:00.000Z": month({"1": {"2026-10-01": "Reserved"}, "2": {"2026-10-01": "Available"}, "3": {"2026-10-01": "New Status"}}),
    }
    svc = mod.AvailabilityService(fetcher=lambda fid, start: responses[start])
    view = svc.get_view("232448", "2026-09-30", "2026-10-02", {"sites": {}})
    sites = {s['id']: s for s in view['sites']}
    assert not any(s['available_for_stay'] for s in sites.values())
    assert sites['1']['availability']['2026-10-01'] == 'Reserved'
    assert sites['3']['availability']['2026-09-30'] == 'Unknown'
    assert sites['3']['availability']['2026-10-01'] == 'New Status'
    assert view['dates'][0] == '2026-09-30'
    assert len(view['dates']) == 14


def test_checkout_is_excluded_and_missing_coordinates_keep_site_visible():
    mod = service_module()
    raw = month({"1": {"2026-09-10": "Available", "2026-09-11": "Available", "2026-09-12": "Closed"}})
    svc = mod.AvailabilityService(fetcher=lambda *_: raw)
    site = svc.get_view("232448", "2026-09-10", "2026-09-12", {"sites": {}})['sites'][0]
    assert site['available_for_stay'] is True
    assert site['lat'] is None and site['lon'] is None
    assert site['accessible'] is None
    assert site['booking_url'] == 'https://www.recreation.gov/camping/campsites/1'


def test_only_explicit_available_counts_and_external_hidden_inventory_is_omitted():
    mod = service_module()
    raw = month({str(i): {'2026-09-10': status} for i, status in enumerate(
        ['Open', 'Not Reservable', 'Not Available Cutoff', 'Closed', 'Available'], 1)})
    raw['campsites']['5']['hide_external'] = True
    view = mod.AvailabilityService(fetcher=lambda *_: raw).get_view('232448', '2026-09-10', '2026-09-11', {'sites': {}})
    assert {s['id'] for s in view['sites']} == {'1', '2', '3', '4'}
    assert not any(s['available_for_stay'] for s in view['sites'])


def test_cache_reuses_a_month_across_different_date_selections():
    mod = service_module()
    calls = []
    def fetch(fid, start):
        calls.append((fid, start))
        return month({'1': {'2026-09-10': 'Available'}})
    svc = mod.AvailabilityService(fetcher=fetch)
    first = svc.get_view('232448', '2026-09-10', '2026-09-11', {'sites': {}})
    second = svc.get_view('232448', '2026-09-11', '2026-09-12', {'sites': {}})
    assert calls == [('232448', '2026-09-01T00:00:00.000Z')]
    assert first['stale'] is second['stale'] is False
    assert second['sites'][0]['available_for_stay'] is False


def test_expired_data_is_labelled_stale_on_failure_then_expires_entirely():
    mod = service_module()
    now, calls = [0], []
    def fetch(*_):
        calls.append(now[0])
        if len(calls) > 1:
            raise OSError('offline')
        return month({'1': {'2026-09-10': 'Available'}})
    svc = mod.AvailabilityService(fetcher=fetch, clock=lambda: now[0])
    first = svc.get_view('232448', '2026-09-10', '2026-09-11', {'sites': {}})
    now[0] = 91
    stale = svc.get_view('232448', '2026-09-10', '2026-09-11', {'sites': {}})
    assert stale['stale'] is True and stale['notice']
    assert stale['fetched_at'] == first['fetched_at']
    now[0] = 100
    assert svc.get_view('232448', '2026-09-10', '2026-09-11', {'sites': {}})['stale']
    assert calls == [0, 91]
    now[0] = 901
    with pytest.raises(mod.AvailabilityError):
        svc.get_view('232448', '2026-09-10', '2026-09-11', {'sites': {}})


def test_cold_failures_are_backed_off_and_malformed_data_is_never_empty_success():
    mod = service_module()
    calls = []
    def fetch(*_):
        calls.append(1)
        return {'error': 'upstream response changed'}
    svc = mod.AvailabilityService(fetcher=fetch)
    for _ in range(2):
        with pytest.raises(mod.AvailabilityError):
            svc.get_view('232448', '2026-09-10', '2026-09-11', {'sites': {}})
    assert len(calls) == 1


def test_simultaneous_viewers_share_one_inflight_request():
    mod = service_module()
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch(*_):
        calls.append(1)
        started.set()
        assert release.wait(3)
        return month({'1': {'2026-09-10': 'Available'}})
    svc = mod.AvailabilityService(fetcher=fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(svc.get_view, '232448', '2026-09-10', '2026-09-11', {'sites': {}}) for _ in range(3)]
        assert started.wait(3)
        release.set()
        views = [f.result(timeout=3) for f in futures]
    assert len(calls) == 1
    assert all(v['sites'][0]['available_for_stay'] for v in views)


def test_date_validation_bounds_outbound_work():
    mod = service_module()
    today = date(2026, 9, 10)
    for start, end in [('2026-09-09', '2026-09-12'), ('2026-09-10', '2026-09-10'),
                       ('2026-09-10', '2026-10-12'), ('nonsense', '2026-09-12'),
                       ('2027-09-10', '2027-09-11')]:
        with pytest.raises(ValueError):
            mod.validate_dates(start, end, today=today)
    assert mod.validate_dates('2026-09-30', '2026-10-02', today=today) == ('2026-09-30', '2026-10-02')


def test_cache_evicts_old_campgrounds_instead_of_growing_indefinitely():
    mod = service_module()
    calls = []
    def fetch(fid, *_):
        calls.append(fid)
        return month({'1': {'2026-09-10': 'Available'}})
    svc = mod.AvailabilityService(fetcher=fetch, max_entries=2)
    for fid in ['1', '2', '3', '2', '1']:
        svc.get_view(fid, '2026-09-10', '2026-09-11', {'sites': {}})
    assert calls == ['1', '2', '3', '1']


def test_fresh_cached_month_is_not_blocked_by_other_campground_refreshes():
    mod = service_module()
    release = threading.Event()
    started = [threading.Event() for _ in range(4)]
    def fetch(fid, *_):
        if fid != '1':
            started[int(fid) - 2].set()
            assert release.wait(4)
        return month({'1': {'2026-09-10': 'Available'}})
    svc = mod.AvailabilityService(fetcher=fetch)
    svc.get_view('1', '2026-09-10', '2026-09-11', {'sites': {}})
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = [pool.submit(svc.get_view, str(fid), '2026-09-10', '2026-09-11', {'sites': {}}) for fid in range(2, 6)]
        try:
            assert all(event.wait(3) for event in started)
            cached = pool.submit(svc.get_view, '1', '2026-09-10', '2026-09-11', {'sites': {}})
            try:
                assert cached.result(timeout=0.5)['sites'][0]['available_for_stay']
            except TimeoutError:
                pytest.fail('A fresh cached campground waited for unrelated network calls')
        finally:
            release.set()
        for job in jobs:
            job.result(timeout=3)


def test_ridb_metadata_normalizes_coordinates_and_retains_sites_without_locations(sample_ridb_dir):
    from pathlib import Path
    from app.ridb import RIDBCatalog
    root = Path(sample_ridb_dir)
    with (root / 'Campsites_API_v1.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['FacilityID', 'CampsiteID', 'CampsiteName', 'Loop', 'CampsiteType', 'CampsiteAccessible', 'CampsiteLatitude', 'CampsiteLongitude'])
        writer.writerows([['232450','1','A01','A','TENT','true','37.7','-119.5'],
                         ['232450','2','A02','A','TENT','false','0','0'],
                         ['232450','3','A03','A','TENT','','NaN','-119'],
                         ['232447','4','B01','B','TENT','false','37.8','-119.6']])
    catalog = RIDBCatalog(sample_ridb_dir)
    assert hasattr(catalog, 'get_campsite_metadata'), 'Campsite metadata lookup is missing'
    metadata = catalog.get_campsite_metadata('232450')
    assert set(metadata['sites']) == {'1', '2', '3'}
    assert metadata['sites']['1']['lat'] == 37.7
    assert metadata['sites']['1']['accessible'] is True
    assert metadata['sites']['2']['lat'] is None
    assert metadata['sites']['3']['lat'] is None
    assert metadata['sites']['3']['accessible'] is None


def test_browse_search_excludes_permits_and_escapes_no_catalog_filters(sample_ridb_dir):
    from app.ridb import RIDBCatalog
    catalog = RIDBCatalog(sample_ridb_dir)
    assert hasattr(catalog, 'search_campgrounds'), 'Campground browsing is missing'
    results = catalog.search_campgrounds(query='Yosemite', state='CA')
    assert {r['id'] for r in results} == {'232450', '232447'}
    assert catalog.get_campground('999003') is None
    assert catalog.search_campgrounds(query='pines', state='NV') == []


def test_campground_cards_have_a_name_when_the_export_name_is_blank(sample_ridb_dir):
    from app.ridb import RIDBCatalog
    catalog = RIDBCatalog(sample_ridb_dir)
    catalog._facilities['232450']['FacilityName'] = '  '
    assert catalog.get_campground('232450')['name'] == 'Campground 232450'
