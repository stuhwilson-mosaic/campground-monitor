/* Shared by the browser page and the small Node behavioral tests. */
(function () {
    'use strict';

    const DAY = 86400000;
    const STATUS = {
        'available': ['available', 'A'],
        'reserved': ['reserved', 'R'],
        'not available': ['not-available', 'N'],
        'closed': ['closed', 'C'],
        'cutoff': ['cutoff', 'X'],
        'not reservable': ['not-reservable', 'NR'],
        'first come first served': ['first-come', 'FC'],
        'walk up': ['walk-up', 'W'],
        'unknown': ['unknown', '?'],
    };

    function statusInfo(value) {
        const label = typeof value === 'string' && value.trim() ? value : 'Unknown';
        const key = label.toLowerCase();
        const known = Object.prototype.hasOwnProperty.call(STATUS, key) && (key !== 'available' || label === 'Available');
        const match = known ? STATUS[key] : STATUS.unknown;
        return {label, kind: match[0], code: match[1], available: label === 'Available'};
    }

    function dayNumber(value) {
        if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
        const timestamp = Date.parse(value + 'T00:00:00Z');
        if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 10) !== value) return NaN;
        return timestamp / DAY;
    }

    function addDays(value, amount) {
        const day = dayNumber(value);
        return Number.isFinite(day) ? new Date((day + amount) * DAY).toISOString().slice(0, 10) : '';
    }

    function stayNights(checkIn, checkOut) {
        return dayNumber(checkOut) - dayNumber(checkIn);
    }

    function formatDay(value, options) {
        const day = dayNumber(value);
        if (!Number.isFinite(day)) return value;
        // A camping date has no timezone. Only source timestamps use local time.
        return new Intl.DateTimeFormat('en-US', {...options, timeZone: 'UTC'}).format(new Date(day * DAY));
    }

    function validateStay(checkIn, checkOut, today, maxDate) {
        const nights = stayNights(checkIn, checkOut);
        if (!Number.isFinite(nights)) return 'Choose valid check-in and check-out dates.';
        if (checkIn < today) return 'Check-in must be today or later.';
        if (checkOut > maxDate) return 'Choose dates within the next year.';
        if (nights < 1 || nights > 31) return 'Choose a stay of 1 to 31 nights. Check-out is excluded.';
        return null;
    }

    function shiftStay(checkIn, checkOut, days, today, maxDate) {
        const shifted = {check_in: addDays(checkIn, days), check_out: addDays(checkOut, days)};
        return validateStay(shifted.check_in, shifted.check_out, today, maxDate) ? null : shifted;
    }

    function filterSites(sites, filters) {
        return sites.filter(site => {
            if (filters.loop && (site.loop || '') !== filters.loop) return false;
            if (filters.type && (site.type || '') !== filters.type) return false;
            if (filters.availableOnly && site.available_for_stay !== true) return false;
            if (filters.accessibility === 'yes' && site.accessible !== true) return false;
            if (filters.accessibility === 'no' && site.accessible !== false) return false;
            if (filters.accessibility === 'unknown' && site.accessible != null) return false;
            return true;
        });
    }

    function summarizeSites(sites, dates) {
        const daily = Object.fromEntries(dates.map(date => [date, 0]));
        sites.forEach(site => dates.forEach(date => {
            if (statusInfo((site.availability || {})[date]).available) daily[date] += 1;
        }));
        return {total: sites.length, fullStay: sites.filter(site => site.available_for_stay === true).length, daily};
    }

    function createRequestGate() {
        let current;
        return {
            start() {
                if (current) current.abort();
                const controller = new AbortController();
                current = controller;
                return {signal: controller.signal, isCurrent: () => current === controller && !controller.signal.aborted};
            },
        };
    }

    function scrollRowIntoView(container, row, headerHeight) {
        if (!container.clientHeight || !row.offsetHeight) return;
        const rowTop = row.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
        if (rowTop < container.scrollTop + headerHeight || rowTop + row.offsetHeight > container.scrollTop + container.clientHeight) {
            container.scrollTop = Math.max(0, rowTop - headerHeight);
        }
    }

    function showMobileView(app, view, fitMap, scrollSelection) {
        app.dataset.mobileView = view;
        ['calendar', 'map'].forEach(tab => app.querySelector('#show-' + tab).setAttribute('aria-pressed', String(tab === view)));
        window.requestAnimationFrame(view === 'map' ? fitMap : scrollSelection);
    }

    const helpers = {statusInfo, addDays, stayNights, formatDay, validateStay, shiftStay,
        filterSites, summarizeSites, createRequestGate, scrollRowIntoView, showMobileView};
    if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
    if (typeof document === 'undefined') return;

    function init() {
        const app = document.getElementById('availability-app');
        if (!app) return;
        const seed = JSON.parse(document.getElementById('availability-seed').textContent);
        const el = id => document.getElementById(id);
        const state = {view: null, filtered: [], selected: null, map: null, markers: new Map(), rows: new Map()};
        const gate = createRequestGate();
        const checkIn = el('availability-check-in');
        const checkOut = el('availability-check-out');
        let draftCheckIn = checkIn.value;

        function node(tag, className, text) {
            const element = document.createElement(tag);
            if (className) element.className = className;
            if (text != null) element.textContent = text;
            return element;
        }

        function safeExternalUrl(value) {
            try {
                const url = new URL(value);
                return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
            } catch (_) { return null; }
        }

        function coordinates(site) {
            return typeof site.lat === 'number' && typeof site.lon === 'number'
                && Number.isFinite(site.lat) && Number.isFinite(site.lon)
                && Math.abs(site.lat) <= 90 && Math.abs(site.lon) <= 180;
        }

        function showError(message) {
            el('availability-error-text').textContent = message;
            el('availability-error').hidden = false;
        }

        function updateDateControls() {
            checkOut.min = addDays(checkIn.value, 1) || seed.today;
            const limit = addDays(checkIn.value, 31);
            checkOut.max = limit && limit < seed.max_date ? limit : seed.max_date;
            el('previous-week').disabled = !shiftStay(checkIn.value, checkOut.value, -7, seed.today, seed.max_date);
            el('next-week').disabled = !shiftStay(checkIn.value, checkOut.value, 7, seed.today, seed.max_date);
            const nights = stayNights(checkIn.value, checkOut.value);
            el('date-nights').textContent = nights > 0 && nights <= 31 ? `${nights} night${nights === 1 ? '' : 's'} · check-out excluded` : 'Choose 1 to 31 nights';
            const changed = state.view && (checkIn.value !== state.view.check_in || checkOut.value !== state.view.check_out);
            el('date-pending').hidden = !changed;
        }

        function updateLinks(view) {
            const facility = view.facility || (state.view && state.view.facility) || seed.facility;
            const params = new URLSearchParams({campground: seed.facility.id, check_in: view.check_in, check_out: view.check_out});
            el('availability-monitor-link').href = '/monitors/new?' + params;
            const booking = safeExternalUrl(facility.booking_url);
            if (booking) el('campground-booking-link').href = booking;
            const official = safeExternalUrl(facility.map_url);
            if (official) {
                el('official-map-link').href = official;
                el('official-map-link').hidden = false;
            }
        }

        function fillFilter(id, key, label) {
            const select = el(id);
            const selected = select.value;
            select.replaceChildren(new Option(label, ''));
            [...new Set(state.view.sites.map(site => site[key]).filter(Boolean))]
                .sort((a, b) => a.localeCompare(b, undefined, {numeric: true}))
                .forEach(value => select.add(new Option(value, value)));
            select.value = [...select.options].some(option => option.value === selected) ? selected : '';
        }

        function filters() {
            return {loop: el('filter-loop').value, type: el('filter-type').value,
                accessibility: el('filter-accessibility').value, availableOnly: el('filter-available').checked};
        }

        function renderLegend() {
            const statuses = new Map();
            ['Available', 'Reserved', 'Not Available', 'Closed', 'Cutoff', 'Not Reservable',
                'First Come First Served', 'Walk Up', 'Unknown'].forEach(value => statuses.set(value, statusInfo(value)));
            state.view.sites.forEach(site => state.view.dates.forEach(date => {
                const info = statusInfo((site.availability || {})[date]);
                if (info.kind === 'unknown') statuses.set(info.label, info);
            }));
            const legend = el('availability-legend');
            legend.replaceChildren();
            statuses.forEach(info => {
                const item = node('li', 'availability-legend-item');
                const swatch = node('span', 'availability-status status-' + info.kind, info.code);
                swatch.setAttribute('aria-hidden', 'true');
                item.append(swatch, node('span', '', info.label));
                legend.append(item);
            });
        }

        function renderCalendar(summary) {
            const head = el('availability-table-head');
            const body = el('availability-table-body');
            head.replaceChildren();
            body.replaceChildren();
            state.rows.clear();
            const header = node('tr');
            const siteHeader = node('th', 'availability-site-heading', 'Campsite');
            siteHeader.scope = 'col';
            siteHeader.append(node('small', '', 'Select a site for details'));
            header.append(siteHeader);
            state.view.dates.forEach(date => {
                const inStay = date >= state.view.check_in && date < state.view.check_out;
                const cell = node('th', inStay ? 'stay-date' : '');
                cell.scope = 'col';
                cell.title = formatDay(date, {weekday: 'long', month: 'long', day: 'numeric', year: 'numeric'})
                    + (inStay ? ' · selected stay' : ' · outside selected stay');
                cell.append(node('span', 'availability-weekday', formatDay(date, {weekday: 'short'})),
                    node('span', 'availability-day', formatDay(date, {month: 'short', day: 'numeric'})),
                    node('small', 'availability-day-count', summary.daily[date] + ' open'));
                const sr = node('span', 'availability-sr-only', inStay ? ', night in selected stay' : ', outside selected stay');
                cell.append(sr);
                header.append(cell);
            });
            head.append(header);
            state.filtered.forEach(site => {
                const row = node('tr');
                const nameCell = node('th', 'availability-site-heading');
                nameCell.scope = 'row';
                const button = node('button', 'availability-site-button', 'Site ' + (site.name || site.id));
                button.type = 'button';
                button.setAttribute('aria-pressed', 'false');
                button.setAttribute('aria-controls', 'selected-site-details');
                nameCell.append(button);
                const metadata = [site.loop ? 'Loop ' + site.loop : '', site.type || ''].filter(Boolean).join(' · ');
                if (metadata) nameCell.append(node('small', '', metadata));
                if (site.available_for_stay === true) nameCell.append(node('span', 'availability-stay-badge', 'Full stay available'));
                if (!coordinates(site)) nameCell.append(node('small', 'availability-no-location', 'No map coordinates'));
                row.append(nameCell);
                state.view.dates.forEach(date => {
                    const info = statusInfo((site.availability || {})[date]);
                    const cell = node('td', date >= state.view.check_in && date < state.view.check_out ? 'stay-date' : '');
                    cell.title = 'Site ' + (site.name || site.id) + ' · ' + formatDay(date, {month: 'short', day: 'numeric'}) + ': ' + info.label;
                    const status = node('span', 'availability-status status-' + info.kind, info.code);
                    status.setAttribute('aria-hidden', 'true');
                    cell.append(status, node('span', 'availability-sr-only', info.label));
                    row.append(cell);
                });
                row.addEventListener('click', () => selectSite(site.id, false));
                state.rows.set(String(site.id), {row, button});
                body.append(row);
            });
            el('calendar-empty').hidden = state.filtered.length !== 0;
            el('calendar-scroll').hidden = state.filtered.length === 0;
        }

        function markerStyle(site) {
            const selected = String(site.id) === state.selected;
            return {radius: selected ? 10 : 7, weight: selected ? 3 : 2,
                color: selected ? '#8b6308' : site.available_for_stay === true ? '#1f4d3a' : '#5a6e66',
                fillColor: selected ? '#e8b84a' : site.available_for_stay === true ? '#34855f' : '#d2dad4',
                fillOpacity: 1};
        }

        function fitMap() {
            if (!state.map) return;
            const locations = state.filtered.filter(coordinates).map(site => [site.lat, site.lon]);
            state.map.invalidateSize({pan: false});
            if (locations.length) state.map.fitBounds(locations, {padding: [28, 28], maxZoom: 18, animate: false});
            else if (coordinates(seed.facility)) state.map.setView([seed.facility.lat, seed.facility.lon], 15, {animate: false});
            else state.map.setView([39, -98], 4, {animate: false});
        }

        function initMap() {
            if (state.map) return true;
            if (!window.L) {
                el('campground-map').hidden = true;
                el('map-message').textContent = 'The interactive map could not load. Select any campsite in the calendar to view its details.';
                el('map-message').hidden = false;
                el('map-fit').disabled = true;
                return false;
            }
            try {
                state.map = L.map('campground-map', {scrollWheelZoom: false});
                L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                    maxZoom: 19,
                    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
                }).on('tileerror', () => {
                    el('map-message').textContent = 'Map tiles could not load. Campsite markers and the calendar remain available.';
                    el('map-message').hidden = false;
                }).addTo(state.map);
                return true;
            } catch (_) {
                if (state.map) state.map.remove();
                state.map = null;
                el('campground-map').hidden = true;
                el('map-message').textContent = 'The interactive map is unavailable. Use the calendar to select a campsite.';
                el('map-message').hidden = false;
                el('map-fit').disabled = true;
                return false;
            }
        }

        function renderMap() {
            const mapped = state.filtered.filter(coordinates);
            el('map-coverage').textContent = `${mapped.length} of ${state.filtered.length} shown sites have map coordinates. Locations are from the campground catalog.`;
            el('map-empty').hidden = mapped.length !== 0;
            el('map-empty').textContent = state.filtered.length ? 'These sites have no map coordinates. Every site remains in the calendar.' : 'No sites match the current filters.';
            if (!initMap()) return;
            state.markers.forEach(({marker}) => marker.remove());
            state.markers.clear();
            // Leaflet creates marker paths only after the map has an initial view.
            fitMap();
            mapped.forEach(site => {
                const label = 'Site ' + (site.name || site.id) + (site.loop ? ' · Loop ' + site.loop : '')
                    + (site.available_for_stay === true ? ' · full stay available' : ' · full stay not available');
                const marker = L.circleMarker([site.lat, site.lon], markerStyle(site))
                    .addTo(state.map).bindTooltip(node('span', '', label));
                marker.on('click', () => selectSite(site.id, true));
                const path = marker.getElement();
                if (path) {
                    path.setAttribute('tabindex', '0');
                    path.setAttribute('role', 'button');
                    path.setAttribute('aria-label', label);
                    path.setAttribute('aria-pressed', String(String(site.id) === state.selected));
                    path.addEventListener('keydown', event => {
                        if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            selectSite(site.id, true);
                        }
                    });
                }
                state.markers.set(String(site.id), {marker, site});
            });
        }

        function renderSelection() {
            const site = state.filtered.find(item => String(item.id) === state.selected);
            const details = el('selected-site-details');
            details.replaceChildren();
            state.rows.forEach(({row, button}, id) => {
                row.classList.toggle('selected-site-row', id === state.selected);
                button.setAttribute('aria-pressed', String(id === state.selected));
            });
            state.markers.forEach(({marker, site: item}, id) => {
                marker.setStyle(markerStyle(item));
                const path = marker.getElement();
                if (path) path.setAttribute('aria-pressed', String(id === state.selected));
                if (id === state.selected) marker.bringToFront();
                else marker.closeTooltip();
            });
            if (!site) {
                el('selected-site-name').textContent = 'Select a campsite';
                details.append(node('p', '', 'Choose a row or a map marker to see site details and a booking link.'));
                return;
            }
            el('selected-site-name').textContent = 'Site ' + (site.name || site.id);
            const stay = formatDay(state.view.check_in, {month: 'short', day: 'numeric'}) + '–'
                + formatDay(state.view.check_out, {month: 'short', day: 'numeric'});
            details.append(node('p', site.available_for_stay === true ? 'availability-selected-open' : '',
                site.available_for_stay === true ? 'Available for the full stay · ' + stay : 'Not available for the full stay · ' + stay));
            const list = node('dl', 'availability-site-facts');
            [['Loop', site.loop || 'Not listed'], ['Type', site.type || 'Not listed'],
                ['Accessible', site.accessible === true ? 'Yes' : site.accessible === false ? 'No' : 'Not listed'],
                ['Map location', coordinates(site) ? 'Shown on map' : 'Coordinates not provided']]
                .forEach(([label, value]) => list.append(node('dt', '', label), node('dd', '', value)));
            details.append(list);
            const booking = safeExternalUrl(site.booking_url);
            if (booking) {
                const link = node('a', 'btn btn-outline-primary', 'View site on Recreation.gov');
                link.href = booking;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                details.append(link);
            }
            details.append(node('p', 'availability-fine-print', 'Confirm site rules and current availability on Recreation.gov before booking.'));
        }

        function scrollSelectedRow() {
            const selectedRow = state.rows.get(state.selected);
            if (selectedRow) scrollRowIntoView(el('calendar-scroll'), selectedRow.row, el('availability-table-head').offsetHeight);
        }

        function selectSite(id, fromMap) {
            state.selected = String(id);
            renderSelection();
            const mapped = state.markers.get(state.selected);
            if (mapped && state.map) {
                mapped.marker.openTooltip();
                if (!fromMap) state.map.panInside(mapped.marker.getLatLng(), {padding: [28, 28], animate: false});
            }
            if (fromMap) scrollSelectedRow();
        }

        function renderFiltered() {
            state.filtered = filterSites(state.view.sites, filters());
            if (!state.filtered.some(site => String(site.id) === state.selected)) state.selected = null;
            const summary = summarizeSites(state.filtered, state.view.dates);
            el('full-stay-count').textContent = summary.fullStay;
            el('result-count').textContent = `${summary.total} of ${state.view.sites.length} sites shown`;
            renderCalendar(summary);
            renderMap();
            renderSelection();
        }

        function renderView() {
            const view = state.view;
            const nights = stayNights(view.check_in, view.check_out);
            el('stay-summary').textContent = `sites available for all ${nights} night${nights === 1 ? '' : 's'} in the selected stay`;
            el('loaded-stay').textContent = formatDay(view.check_in, {month: 'short', day: 'numeric', year: 'numeric'})
                + ' to ' + formatDay(view.check_out, {month: 'short', day: 'numeric', year: 'numeric'});
            el('grid-range').textContent = formatDay(view.dates[0], {month: 'short', day: 'numeric'}) + '–'
                + formatDay(view.dates[view.dates.length - 1], {month: 'short', day: 'numeric', year: 'numeric'});
            const fetched = new Date(view.fetched_at);
            el('availability-freshness').textContent = Number.isFinite(fetched.getTime())
                ? 'Source checked ' + fetched.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'})
                : 'Source check time unavailable';
            el('availability-notice').hidden = !(view.stale || view.notice);
            el('availability-notice').classList.toggle('is-stale', !!view.stale);
            el('availability-notice').textContent = (view.stale ? 'Showing older availability. ' : '') + (view.notice || '');
            fillFilter('filter-loop', 'loop', 'All loops');
            fillFilter('filter-type', 'type', 'All site types');
            renderLegend();
            updateLinks(view);
            updateDateControls();
            renderFiltered();
        }

        async function loadAvailability() {
            const dates = {check_in: checkIn.value, check_out: checkOut.value};
            const error = validateStay(dates.check_in, dates.check_out, seed.today, seed.max_date);
            if (error) { showError(error); return; }
            const request = gate.start();
            updateLinks(dates);
            el('date-pending').hidden = true;
            el('availability-error').hidden = true;
            el('availability-results').hidden = true;
            el('availability-loading').hidden = false;
            app.setAttribute('aria-busy', 'true');
            try {
                const response = await fetch('/api/campgrounds/' + encodeURIComponent(seed.facility.id) + '/availability?' + new URLSearchParams(dates), {
                    signal: request.signal, headers: {Accept: 'application/json'}, credentials: 'same-origin',
                });
                if (!request.isCurrent()) return;
                if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) {
                    throw new Error('Your session may have expired. Refresh this page to sign in again.');
                }
                const view = await response.json();
                if (!request.isCurrent()) return;
                if (!response.ok) throw new Error(typeof view.detail === 'string' ? view.detail : 'Availability could not be retrieved. Try again.');
                if (!Array.isArray(view.sites) || !Array.isArray(view.dates) || !view.dates.length) {
                    throw new Error('The availability response was incomplete. Try again.');
                }
                state.view = view;
                el('availability-results').hidden = false;
                renderView();
                const url = new URL(window.location.href);
                url.searchParams.set('check_in', view.check_in);
                url.searchParams.set('check_out', view.check_out);
                window.history.replaceState(null, '', url);
            } catch (error) {
                if (!request.isCurrent() || error.name === 'AbortError') return;
                el('availability-results').hidden = true;
                showError(error instanceof TypeError ? 'Availability could not be loaded. Check your connection and try again.' : error.message);
            } finally {
                if (request.isCurrent()) {
                    el('availability-loading').hidden = true;
                    app.setAttribute('aria-busy', 'false');
                }
            }
        }

        el('availability-dates').addEventListener('submit', event => { event.preventDefault(); loadAvailability(); });
        el('availability-retry').addEventListener('click', loadAvailability);
        checkIn.addEventListener('change', () => {
            const nights = stayNights(draftCheckIn, checkOut.value);
            const end = addDays(checkIn.value, nights > 0 && nights <= 31 ? nights : 1);
            checkOut.value = end > seed.max_date ? seed.max_date : end;
            draftCheckIn = checkIn.value;
            updateDateControls();
        });
        checkOut.addEventListener('change', updateDateControls);
        [['previous-week', -7], ['next-week', 7]].forEach(([id, days]) => {
            el(id).addEventListener('click', () => {
                const dates = shiftStay(checkIn.value, checkOut.value, days, seed.today, seed.max_date);
                if (!dates) return;
                checkIn.value = dates.check_in;
                checkOut.value = dates.check_out;
                draftCheckIn = dates.check_in;
                updateDateControls();
                loadAvailability();
            });
        });
        ['filter-loop', 'filter-type', 'filter-accessibility', 'filter-available'].forEach(id => {
            el(id).addEventListener('change', () => { if (state.view) renderFiltered(); });
        });
        el('clear-filters').addEventListener('click', () => {
            ['filter-loop', 'filter-type', 'filter-accessibility'].forEach(id => { el(id).value = ''; });
            el('filter-available').checked = false;
            renderFiltered();
        });
        el('map-fit').addEventListener('click', fitMap);
        ['calendar', 'map'].forEach(view => {
            el('show-' + view).addEventListener('click', () => {
                showMobileView(app, view, fitMap, scrollSelectedRow);
            });
        });
        window.addEventListener('resize', () => { if (state.map) state.map.invalidateSize({pan: false}); });
        checkIn.max = addDays(seed.max_date, -1);
        updateLinks(seed);
        updateDateControls();
        loadAvailability();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
