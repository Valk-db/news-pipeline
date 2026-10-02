/**
 * Event Map page controller.
 *
 * Plots events from GET /api/globe/events as circle markers on a Leaflet map,
 * re-querying with the visible bbox when the viewport moves.
 *
 * A second mode, replay, swaps the viewport-scoped query for GET /api/map/replay:
 * one chronological window fetched up front, then revealed over time by a
 * bottom scrubber so a story can be watched unfolding across geography.
 *
 * Everything below is plain ES5 so it runs in mobile Safari without a build
 * step, and no data logic lives here: both endpoints own the payload shape.
 */
(function () {
    'use strict';

    var API_ENDPOINT = '/api/globe/events';
    var REPLAY_ENDPOINT = '/api/map/replay';

    /*
     * Keyless OpenStreetMap raster tiles, one host on purpose. iOS Safari pays
     * a DNS plus TLS setup per origin, so rotating {s} triples first-paint cost
     * on a phone for no benefit.
     *
     * The dark look is a filter applied per tile image in map.css. Do not move
     * that filter onto .leaflet-tile-pane: iOS WebKit cannot rasterize one
     * filtered surface that large and paints the whole pane solid black, which
     * reads as a dead basemap. A filter per 256px tile stays inside WebKit's
     * budget and the map draws.
     */
    var TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    var TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

    var MOVE_DEBOUNCE_MS = 400;
    var EVENT_LIMIT = 500;
    var UNKNOWN_COLOR = '#8899aa';
    var UNKNOWN_LABEL = 'Unclassified';

    /* Marker budget for the pulse animation. Every pulsing dot is its own CSS
     * animation, so cap how many run at once. Marks beyond the cap keep the
     * static ring, which still reads as a highlight. */
    var PULSE_MAX = 48;

    /* Freshness thresholds for the marker ring, in hours. */
    var FRESH_HOURS = 12;
    var RECENT_HOURS = 72;

    /* Mirrors the replay cap in curation_ui/main.py. MAP_REPLAY_MAX_LIMIT (1000) is
     * the server hard cap and is what a wide window is most likely to hit;
     * the response carries max_limit and truncated so the note below the
     * scrubber can say so. */
    var REPLAY_LIMIT = 1000;
    var REPLAY_MAX_LIMIT = 1000;
    var REPLAY_STEPS = 1000;
    var REPLAY_TICK_MS = 100;
    var MS_PER_HOUR = 3600000;

    /* Histogram bars behind the scrubber. */
    var HIST_BUCKETS = 56;

    /* Placeholder for a value the API did not send. Spelled out rather than
     * dashed, so no visible copy on this page carries an em dash. */
    var UNKNOWN_VALUE = 'n/a';

    var REPLAY_WINDOWS = [
        { value: '24h', label: 'Last 24h', hours: 24 },
        { value: '7d', label: 'Last 7d', hours: 24 * 7 },
        { value: '30d', label: 'Last 30d', hours: 24 * 30 },
        { value: 'all', label: 'All time', hours: null }
    ];

    /**
     * Event type vocabulary mirrored from src/schema/models.py EventType.
     * Hexes are unchanged from the globe page on purpose, so one event reads
     * the same on both surfaces.
     */
    var EVENT_TYPES = [
        { value: 'conflict', label: 'Conflict', color: '#ff4757' },
        { value: 'protest', label: 'Protest', color: '#ffb800' },
        { value: 'election', label: 'Election', color: '#00d4aa' },
        { value: 'disaster', label: 'Disaster', color: '#ff6b35' },
        { value: 'accident', label: 'Accident', color: '#8899aa' },
        { value: 'political', label: 'Political', color: '#9b59ff' },
        { value: 'economic', label: 'Economic', color: '#00d4aa' },
        { value: 'health', label: 'Health', color: '#e91e63' },
        { value: 'environmental', label: 'Environmental', color: '#2ecc71' },
        { value: 'crime', label: 'Crime', color: '#c0392b' },
        { value: 'sports', label: 'Sports', color: '#f1c40f' },
        { value: 'cultural', label: 'Cultural', color: '#8e44ad' },
        { value: 'scientific', label: 'Scientific', color: '#1abc9c' },
        { value: 'other', label: 'Other', color: '#7f8c8d' }
    ];

    var COLOR_BY_TYPE = {};
    var LABEL_BY_TYPE = {};
    EVENT_TYPES.forEach(function (entry) {
        COLOR_BY_TYPE[entry.value] = entry.color;
        LABEL_BY_TYPE[entry.value] = entry.label;
    });

    var el = {
        map: document.getElementById('map'),
        body: document.getElementById('map-body'),
        typeSelect: document.getElementById('event-type-select'),
        searchInput: document.getElementById('map-search'),
        tierChips: document.querySelectorAll('.map-tier-chip'),
        windowSelect: document.getElementById('map-window'),
        sortSelect: document.getElementById('map-sort'),
        modeToggle: document.getElementById('map-mode-toggle'),
        modeButtons: document.querySelectorAll('.map-mode-btn'),
        status: document.getElementById('map-status'),
        statusText: document.getElementById('map-status-text'),
        statusMeta: document.getElementById('map-status-meta'),
        loading: document.getElementById('map-loading'),
        empty: document.getElementById('map-empty'),
        emptyTitle: document.getElementById('map-empty-title'),
        emptyBody: document.getElementById('map-empty-body'),
        emptyPrimary: document.getElementById('map-empty-primary'),
        banner: document.getElementById('map-banner'),
        bannerMessage: document.getElementById('map-banner-message'),
        bannerRetry: document.getElementById('map-banner-retry'),
        bannerClose: document.getElementById('map-banner-close'),
        legend: document.getElementById('map-legend'),
        legendHead: document.getElementById('map-legend-head'),
        legendList: document.getElementById('map-legend-list'),
        legendTotal: document.getElementById('map-legend-total'),
        replay: document.getElementById('map-replay'),
        replayRestart: document.getElementById('map-replay-restart'),
        replayPlay: document.getElementById('map-replay-play'),
        replayClock: document.getElementById('map-replay-clock'),
        replayProgress: document.getElementById('map-replay-progress'),
        replayTrack: document.getElementById('map-replay-track'),
        replayHist: document.getElementById('map-replay-hist'),
        replayWindow: document.getElementById('map-replay-window'),
        replaySpeed: document.getElementById('map-replay-speed'),
        replayScrub: document.getElementById('map-replay-scrub'),
        replayStart: document.getElementById('map-replay-start'),
        replayEnd: document.getElementById('map-replay-end'),
        replayNote: document.getElementById('map-replay-note')
    };

    var map = null;
    var markerLayer = null;
    var debounceTimer = null;
    var replayTimer = null;
    var inflightController = null;
    var replayController = null;
    var requestSeq = 0;
    var replaySeq = 0;
    var state = {
        eventType: '',
        markerCount: 0,
        typeCounts: {},
        loaded: false,
        mode: 'live'
    };

    /* Replay-only state. timeline is ascending by start time; entries hold
     * markers that are created up front but only added to the map as the play
     * head passes them. */
    var replay = {
        loading: false,
        timeline: [],
        revealed: 0,
        rangeStart: 0,
        rangeEnd: 0,
        playing: false,
        playHead: 0,
        playOrigin: 0,
        playStamp: 0,
        speed: 1,
        window: '7d',
        truncated: false,
        maxLimit: REPLAY_MAX_LIMIT
    };

    /* ------------------------------------------------------------------ */
    /* Helpers                                                             */
    /* ------------------------------------------------------------------ */

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value).replace(
            /[&<>"']/g,
            function (ch) {
                return {
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#39;'
                }[ch];
            }
        );
    }

    function typeColor(type) {
        return COLOR_BY_TYPE[type] || UNKNOWN_COLOR;
    }

    function typeLabel(type) {
        if (!type) {
            return UNKNOWN_LABEL;
        }
        return LABEL_BY_TYPE[type] || type;
    }

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    function formatCoord(value) {
        return Number(value).toFixed(4);
    }

    function formatTime(isoString) {
        if (!isoString) {
            return UNKNOWN_VALUE;
        }
        var parsed = new Date(isoString);
        if (isNaN(parsed.getTime())) {
            return String(isoString);
        }
        try {
            return parsed.toLocaleString(undefined, {
                year: 'numeric',
                month: 'short',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit'
            });
        } catch (err) {
            return parsed.toISOString().replace('T', ' ').slice(0, 16);
        }
    }

    function formatNumber(value) {
        if (value === null || value === undefined || value === '') {
            return UNKNOWN_VALUE;
        }
        var num = Number(value);
        if (isNaN(num)) {
            return String(value);
        }
        return num.toLocaleString();
    }

    function toFiniteNumber(value, fallback) {
        var num = Number(value);
        return isFinite(num) ? num : fallback;
    }

    function isUuid(value) {
        return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(value || ''));
    }

    /** Replay clock: same precision as before, but never an em dash. */
    function formatClock(ms) {
        if (!ms) {
            return '--:--';
        }
        return formatTime(new Date(ms).toISOString());
    }

    function formatBound(ms) {
        if (!ms) {
            return '--';
        }
        return formatTime(new Date(ms).toISOString());
    }

    /** "just now", "18 min ago", "3 d ago" for a coarse freshness read. */
    function relativeAge(isoString, now) {
        if (!isoString) {
            return null;
        }
        var ms = new Date(isoString).getTime();
        if (isNaN(ms)) {
            return null;
        }
        var delta = Math.max(0, now - ms);
        var minutes = Math.floor(delta / 60000);
        if (minutes < 2) {
            return 'just now';
        }
        if (minutes < 60) {
            return minutes + ' min ago';
        }
        var hours = Math.floor(minutes / 60);
        if (hours < 24) {
            return hours + (hours === 1 ? ' hour ago' : ' hours ago');
        }
        var days = Math.floor(hours / 24);
        return days + (days === 1 ? ' day ago' : ' days ago');
    }

    /** Hours since start_time, or null when the event cannot be aged. */
    function hoursSince(isoString, now) {
        if (!isoString) {
            return null;
        }
        var ms = new Date(isoString).getTime();
        if (isNaN(ms)) {
            return null;
        }
        return (now - ms) / MS_PER_HOUR;
    }

    /**
     * A handful of entity names from the event's {"PERSON": [], "ORG": [],
     * "GPE": []} bag, as the popup's who-is-involved line.
     */
    function entityNames(props, limit) {
        var max = limit || 3;
        var entities = props.entities;
        if (!entities || typeof entities !== 'object') {
            return [];
        }
        var names = [];
        ['ORG', 'PERSON', 'GPE'].some(function (key) {
            var values = entities[key];
            if (!Array.isArray(values)) {
                return false;
            }
            values.forEach(function (value) {
                if (names.length < max && value && names.indexOf(String(value)) === -1) {
                    names.push(String(value));
                }
            });
            return names.length >= max;
        });
        return names;
    }

    /** Where the rest of the story lives. Only when the id is really a uuid. */
    function storyHref(props) {
        if (isUuid(props.story_id)) {
            return '/story/' + encodeURIComponent(props.story_id) + '/edit';
        }
        return '/';
    }

    function storyHrefLabel(props) {
        return isUuid(props.story_id) ? 'Open the story' : 'Open Curation';
    }

    function confidenceLabel(value) {
        if (value >= 0.75) {
            return 'Strong';
        }
        if (value >= 0.45) {
            return 'Partial';
        }
        return 'Weak';
    }

    function confidenceTone(value) {
        if (value >= 0.75) {
            return 'is-strong';
        }
        if (value >= 0.45) {
            return 'is-partial';
        }
        return 'is-weak';
    }

    /**
     * Pull a representative [lat, lon] out of any GeoJSON geometry so a
     * feature can always be drawn as a circle marker.
     */
    function representativeLatLng(geometry) {
        if (!geometry || !geometry.coordinates) {
            return null;
        }
        var coords = geometry.coordinates;
        var type = geometry.type;

        if (type === 'GeometryCollection') {
            var geometries = geometry.geometries || [];
            for (var i = 0; i < geometries.length; i += 1) {
                var nested = representativeLatLng(geometries[i]);
                if (nested) {
                    return nested;
                }
            }
            return null;
        }

        if (type === 'Point') {
            return latLngFrom(coords);
        }
        if (type === 'MultiPoint') {
            return coords.length ? latLngFrom(coords[0]) : null;
        }
        if (type === 'LineString') {
            if (!coords.length) {
                return null;
            }
            return latLngFrom(coords[Math.floor(coords.length / 2)]);
        }
        if (type === 'MultiLineString') {
            for (var j = 0; j < coords.length; j += 1) {
                var line = representativeLatLng({ type: 'LineString', coordinates: coords[j] });
                if (line) {
                    return line;
                }
            }
            return null;
        }
        if (type === 'Polygon') {
            return polygonCentroid(coords);
        }
        if (type === 'MultiPolygon') {
            for (var k = 0; k < coords.length; k += 1) {
                var polygon = polygonCentroid(coords[k]);
                if (polygon) {
                    return polygon;
                }
            }
            return null;
        }
        if (Array.isArray(coords[0]) && coords[0].length) {
            return representativeLatLng({ type: 'Polygon', coordinates: coords });
        }
        return null;
    }

    function latLngFrom(coords) {
        if (!Array.isArray(coords) || coords.length < 2) {
            return null;
        }
        var lon = toFiniteNumber(coords[0], null);
        var lat = toFiniteNumber(coords[1], null);
        if (lon === null || lat === null) {
            return null;
        }
        if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
            return null;
        }
        return [lat, lon];
    }

    function polygonCentroid(rings) {
        if (!Array.isArray(rings) || !rings.length || !Array.isArray(rings[0])) {
            return null;
        }
        var ring = rings[0];
        if (ring.length === 1) {
            return latLngFrom(ring[0]);
        }
        var sumLat = 0;
        var sumLon = 0;
        var count = 0;
        for (var i = 0; i < ring.length; i += 1) {
            var point = ring[i];
            if (!Array.isArray(point) || point.length < 2) {
                continue;
            }
            var lon = toFiniteNumber(point[0], null);
            var lat = toFiniteNumber(point[1], null);
            if (lon === null || lat === null) {
                continue;
            }
            sumLon += lon;
            sumLat += lat;
            count += 1;
        }
        if (!count) {
            return null;
        }
        return [sumLat / count, sumLon / count];
    }

    /* ------------------------------------------------------------------ */
    /* Bounding box                                                        */
    /* ------------------------------------------------------------------ */

    /**
     * Visible bounds as one or two API bbox strings. Two are needed when the
     * viewport straddles the antimeridian, which a single min/max pair cannot
     * express.
     */
    function visibleBboxes() {
        if (!map) {
            return [];
        }
        var bounds = map.getBounds();
        var south = clamp(bounds.getSouth(), -90, 90);
        var north = clamp(bounds.getNorth(), -90, 90);
        var west = clamp(bounds.getWest(), -180, 180);
        var east = clamp(bounds.getEast(), -180, 180);

        if (east < west) {
            return [
                [west, south, 180, north],
                [-180, south, east, north]
            ];
        }
        return [[west, south, east, north]];
    }

    function buildRequestUrl(bbox) {
        var params = new URLSearchParams();
        if (bbox) {
            params.set('bbox', formatCoord(bbox[0]) + ',' + formatCoord(bbox[1]) + ',' +
                formatCoord(bbox[2]) + ',' + formatCoord(bbox[3]));
        }
        if (state.eventType) {
            params.set('event_type', state.eventType);
        }
        var tiers = activeTiers();
        if (tiers.length > 0 && tiers.length < 4) {
            params.set('tiers', tiers.join(','));
        }
        params.set('limit', String(EVENT_LIMIT));
        return API_ENDPOINT + '?' + params.toString();
    }

    function fetchBbox(bbox, signal) {
        var url = buildRequestUrl(bbox);
        var options = { headers: { Accept: 'application/json' }, credentials: 'same-origin' };
        if (signal) {
            options.signal = signal;
        }
        return fetch(url, options)
            .then(function (response) {
                return response
                    .json()
                    .catch(function () {
                        throw new Error('Request failed (HTTP ' + response.status + ')');
                    })
                    .then(function (payload) {
                        if (payload && payload.error) {
                            throw new Error(String(payload.error));
                        }
                        if (!response.ok) {
                            throw new Error('Request failed (HTTP ' + response.status + ')');
                        }
                        if (!payload || payload.type !== 'FeatureCollection' ||
                            !Array.isArray(payload.features)) {
                            throw new Error('Unexpected response from ' + API_ENDPOINT);
                        }
                        return payload.features;
                    });
            });
    }

    /* ------------------------------------------------------------------ */
    /* Status pill, banner, page states                                    */
    /* ------------------------------------------------------------------ */

    /**
     * One line of outcome plus a quiet second line of context: which mode, and
     * how wide the question is. Both are textContent, never markup.
     */
    function setStatus(text, mode, meta) {
        if (el.statusText) {
            el.statusText.textContent = text;
        }
        if (el.statusMeta) {
            el.statusMeta.textContent = meta || '';
        }
        if (!el.status) {
            return;
        }
        el.status.classList.remove('is-busy', 'is-error');
        if (mode) {
            el.status.classList.add(mode);
        }
    }

    function scopeLabel() {
        return state.eventType ? typeLabel(state.eventType).toLowerCase() : 'all types';
    }

    function liveMeta() {
        return 'live view / ' + scopeLabel();
    }

    function replayMeta() {
        var label = REPLAY_WINDOWS.filter(function (entry) {
            return entry.value === replay.window;
        })[0];
        return 'replay window / ' + (label ? label.label.toLowerCase() : replay.window);
    }

    function showBanner(message) {
        if (!el.banner || !el.bannerMessage) {
            return;
        }
        el.bannerMessage.textContent = message;
        el.banner.hidden = false;
    }

    function hideBanner() {
        if (el.banner) {
            el.banner.hidden = true;
        }
    }

    function showLoading(active) {
        if (!el.loading) {
            return;
        }
        el.loading.hidden = !active;
        el.loading.setAttribute('aria-hidden', active ? 'false' : 'true');
    }

    /**
     * The zero-result state. With a type filter active it says so and offers
     * the way out; without one it points at the region, which is the usual
     * reason a view comes back empty.
     */
    function showEmpty() {
        if (!el.empty) {
            return;
        }
        var filtered = !!state.eventType;
        if (el.emptyTitle) {
            el.emptyTitle.textContent = filtered
                ? 'No ' + typeLabel(state.eventType).toLowerCase() + ' events here'
                : 'Nothing plotted here';
        }
        if (el.emptyBody) {
            el.emptyBody.textContent = filtered
                ? 'Every confirmed ' + typeLabel(state.eventType).toLowerCase() +
                    ' event lands on this map as it happens. None have been confirmed ' +
                    'inside the view you are looking at.'
                : 'Every confirmed event lands on this map as it happens, at the place ' +
                    'it happened. No events fall inside the view you are looking at.';
        }
        if (el.emptyPrimary) {
            el.emptyPrimary.textContent = filtered ? 'Show all types' : 'Look at the world';
        }
        el.empty.hidden = false;
    }

    function hideEmpty() {
        if (el.empty) {
            el.empty.hidden = true;
        }
    }

    function onEmptyPrimary() {
        if (state.eventType) {
            setEventType('');
            return;
        }
        if (map && typeof map.setView === 'function') {
            map.setView([20, 0], 2);
        }
        state.loaded = false;
        fetchEvents();
    }

    /* ------------------------------------------------------------------ */
    /* Filters and legend                                                  */
    /* ------------------------------------------------------------------ */

    function setEventType(value) {
        state.eventType = value || '';
        if (el.typeSelect && el.typeSelect.value !== state.eventType) {
            el.typeSelect.value = state.eventType;
        }
        syncLegendSelection();
        hideBanner();
        if (state.mode === 'replay') {
            loadReplay();
        } else {
            scheduleFetch();
        }
    }

    function buildTypeFilter() {
        if (!el.typeSelect) {
            return;
        }
        EVENT_TYPES.forEach(function (entry) {
            var option = document.createElement('option');
            option.value = entry.value;
            option.textContent = entry.label;
            el.typeSelect.appendChild(option);
        });
        el.typeSelect.addEventListener('change', function () {
            setEventType(el.typeSelect.value || '');
        });
        wireDiscoveryControls();
    }

    /**
     * Discovery controls: tier chips, topic search, date window, sort.
     *
     * The stories list is server-rendered, so a control change navigates to
     * /map with the new query string and the server re-renders everything
     * consistently. Marker fetches also carry the active tiers so the map
     * markers respect the same filter. Search is debounced and submits on
     * Enter to avoid a reload per keystroke.
     */
    function activeTiers() {
        var tiers = [];
        if (!el.tierChips) {
            return tiers;
        }
        Array.prototype.forEach.call(el.tierChips, function (chip) {
            if (chip.classList.contains('is-on')) {
                tiers.push(chip.getAttribute('data-tier'));
            }
        });
        return tiers;
    }

    function discoveryQuery(overrides) {
        var params = new URLSearchParams(window.location.search);
        var tiers = (overrides && overrides.tiers !== undefined) ? overrides.tiers : activeTiers();
        params.delete('tiers');
        if (tiers.length > 0 && tiers.length < 4) {
            params.set('tiers', tiers.join(','));
        }
        var q = (overrides && overrides.q !== undefined) ? overrides.q : (el.searchInput ? el.searchInput.value.trim() : '');
        params.delete('q');
        if (q) {
            params.set('q', q);
        }
        var hours = (overrides && overrides.hours !== undefined) ? overrides.hours : (el.windowSelect ? el.windowSelect.value : '24');
        params.delete('hours');
        params.set('hours', String(hours));
        var sort = (overrides && overrides.sort !== undefined) ? overrides.sort : (el.sortSelect ? el.sortSelect.value : 'top');
        params.delete('sort');
        params.set('sort', sort);
        return params.toString();
    }

    function navigateWithDiscovery(overrides) {
        window.location.href = '/map?' + discoveryQuery(overrides);
    }

    function wireDiscoveryControls() {
        if (el.tierChips) {
            Array.prototype.forEach.call(el.tierChips, function (chip) {
                chip.addEventListener('click', function () {
                    var nowOn = !chip.classList.contains('is-on');
                    chip.classList.toggle('is-on', nowOn);
                    chip.setAttribute('aria-pressed', nowOn ? 'true' : 'false');
                    navigateWithDiscovery({ tiers: activeTiers() });
                });
            });
        }
        if (el.windowSelect) {
            el.windowSelect.addEventListener('change', function () {
                navigateWithDiscovery({ hours: el.windowSelect.value });
            });
        }
        if (el.sortSelect) {
            el.sortSelect.addEventListener('change', function () {
                navigateWithDiscovery({ sort: el.sortSelect.value });
            });
        }
        if (el.searchInput) {
            var debounce = null;
            el.searchInput.addEventListener('input', function () {
                if (debounce) {
                    window.clearTimeout(debounce);
                }
                debounce = window.setTimeout(function () {
                    if (el.searchInput.value.trim() !== (new URLSearchParams(window.location.search).get('q') || '')) {
                        navigateWithDiscovery({ q: el.searchInput.value.trim() });
                    }
                }, 900);
            });
            el.searchInput.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    if (debounce) {
                        window.clearTimeout(debounce);
                    }
                    navigateWithDiscovery({ q: el.searchInput.value.trim() });
                }
            });
        }
    }

    function buildLegend() {
        if (!el.legendList) {
            return;
        }
        EVENT_TYPES.forEach(function (entry) {
            var item = document.createElement('li');
            item.className = 'map-legend-item is-empty';
            item.dataset.eventType = entry.value;

            var hit = document.createElement('button');
            hit.type = 'button';
            hit.className = 'map-legend-hit';
            hit.setAttribute('data-event-type', entry.value);
            hit.setAttribute('aria-pressed', 'false');

            var swatch = document.createElement('span');
            swatch.className = 'map-legend-swatch';
            swatch.style.setProperty('--swatch', entry.color);

            var label = document.createElement('span');
            label.className = 'map-legend-label';
            label.textContent = entry.label;

            var count = document.createElement('span');
            count.className = 'map-legend-count';
            count.textContent = '0';

            hit.appendChild(swatch);
            hit.appendChild(label);
            hit.appendChild(count);
            item.appendChild(hit);
            el.legendList.appendChild(item);
        });

        if (el.legendList.addEventListener) {
            el.legendList.addEventListener('click', function (evt) {
                var target = evt.target;
                var row = null;
                while (target && target !== el.legendList) {
                    if (target.classList && target.classList.contains('map-legend-hit')) {
                        row = target;
                        break;
                    }
                    target = target.parentNode;
                }
                if (!row) {
                    return;
                }
                var type = row.getAttribute('data-event-type') || '';
                setEventType(type === state.eventType ? '' : type);
            });
        }

        if (el.legendHead) {
            el.legendHead.addEventListener('click', function () {
                var collapsed = el.legend.classList.toggle('is-collapsed');
                el.legendHead.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            });
        }
    }

    function syncLegendSelection() {
        if (!el.legendList) {
            return;
        }
        var items = el.legendList.querySelectorAll('.map-legend-item');
        Array.prototype.forEach.call(items, function (item) {
            var active = item.dataset.eventType === state.eventType;
            item.classList.toggle('is-active', active);
            var hit = item.querySelector('.map-legend-hit');
            if (hit) {
                hit.setAttribute('aria-pressed', active ? 'true' : 'false');
            }
        });
    }

    function updateLegend() {
        if (!el.legendList) {
            return;
        }
        var items = el.legendList.querySelectorAll('.map-legend-item');
        Array.prototype.forEach.call(items, function (item) {
            var type = item.dataset.eventType;
            var count = state.typeCounts[type] || 0;
            var node = item.querySelector('.map-legend-count');
            if (node) {
                node.textContent = formatNumber(count);
            }
            item.classList.toggle('is-empty', count === 0);
            item.classList.toggle('is-active', type === state.eventType);
        });
        if (el.legendTotal) {
            el.legendTotal.textContent = formatNumber(state.markerCount);
        }
    }

    /* ------------------------------------------------------------------ */
    /* Rendering                                                           */
    /* ------------------------------------------------------------------ */

    function popupHtml(props, now) {
        var type = props.event_type || '';
        // 0.5 is the same default the marker sizing and the API use, so the
        // meter never disagrees with the dot the user tapped.
        var confidence = clamp(toFiniteNumber(props.confidence, 0.5), 0, 1);
        var percent = Math.round(confidence * 100);
        var age = relativeAge(props.start_time, now);
        var sourceCount = toFiniteNumber(props.source_count, null);
        var tier1Count = toFiniteNumber(props.tier1_source_count, null);
        var sources = sourceCount === null ? 'none yet' : formatNumber(sourceCount);
        var tier1 = tier1Count === null ? UNKNOWN_VALUE : formatNumber(tier1Count);
        var entities = entityNames(props, 3);

        var html = '<div class="map-popup" style="--m-popup-accent:' + typeColor(type) + '">';
        html += '<div class="map-popup-eyebrow">';
        html += '<span class="map-popup-chip">';
        html += '<span class="map-popup-dot" style="background:' + typeColor(type) + '"></span>';
        html += escapeHtml(typeLabel(type));
        html += '</span>';
        if (age) {
            html += '<span class="map-popup-age">' + escapeHtml(age) + '</span>';
        }
        html += '</div>';
        html += '<div class="map-popup-title">' +
            escapeHtml(props.location_name || 'Unknown location');
        html += '</div>';

        if (entities.length) {
            html += '<div class="map-popup-entities">';
            entities.forEach(function (name) {
                html += '<span class="map-popup-entity">' + escapeHtml(name) + '</span>';
            });
            html += '</div>';
        }

        html += '<div class="map-popup-meter ' + confidenceTone(confidence) + '">';
        html += '<div class="map-popup-meter-head">';
        html += '<span class="map-popup-meter-label">confidence</span>';
        html += '<span class="map-popup-meter-value">' + confidenceLabel(confidence) + '</span>';
        html += '</div>';
        html += '<div class="map-popup-bar"><span style="width:' + percent + '%"></span></div>';
        html += '<div class="map-popup-meter-foot">';
        html += '<span>' + percent + '%</span>';
        html += '<span>' + sources + '</span>';
        if (tier1Count !== null) {
            html += '<span>' + tier1 + (tier1Count === 1 ? ' tier 1 source' : ' tier 1') + '</span>';
        }
        html += '</div>';
        html += '</div>';

        html += '<dl class="map-popup-rows">';
        html += '<div class="map-popup-row"><dt>Type</dt><dd>' +
            escapeHtml(typeLabel(type)) + '</dd></div>';
        html += '<div class="map-popup-row"><dt>Confidence</dt><dd>' + percent + '%</dd></div>';
        html += '<div class="map-popup-row"><dt>Sources</dt><dd>' + sources + '</dd></div>';
        html += '<div class="map-popup-row"><dt>Started</dt><dd>' +
            escapeHtml(formatTime(props.start_time)) + '</dd></div>';
        html += '</dl>';

        html += '<a class="map-popup-cta" href="' + escapeHtml(storyHref(props)) + '">' +
            escapeHtml(storyHrefLabel(props)) +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            '<path d="M5 12h13M12 5l7 7-7 7"></path></svg></a>';
        html += '</div>';
        return html;
    }

    /**
     * How much of the map body's bottom edge the replay deck is sitting on.
     * The map container runs the full height under the deck, so Leaflet's own
     * auto-pan would leave a popup tucked behind it without this allowance.
     */
    function deckInset() {
        if (!el.replay || el.replay.hidden) {
            return 0;
        }
        return el.replay.offsetHeight || 0;
    }

    /**
     * How one event reads on the map: radius from confidence, plus a marker
     * class for freshness and the pulse ring. The pulse budget is spent in
     * event order, so the highlight lands on what is new rather than on
     * whichever feature the API happened to return first.
     */
    function markerStyle(props, now, budget) {
        var confidence = clamp(toFiniteNumber(props.confidence, 0.5), 0, 1);
        var hours = hoursSince(props.start_time, now);
        var fresh = hours !== null && hours >= 0 && hours <= FRESH_HOURS;
        var recent = hours !== null && hours >= 0 && hours <= RECENT_HOURS;
        var pulse = !!budget && fresh && confidence >= 0.6 && budget.left > 0;

        if (pulse) {
            budget.left -= 1;
        }

        return {
            radius: 4 + Math.round(confidence * 4),
            className: 'map-marker',
            // The pulse draws as a bright stroke on the marker's own ring, so no
            // second shape per event and no extra SVG nodes to composite.
            color: pulse ? typeColor(props.event_type || '') : '#05080c',
            weight: pulse ? 2.4 : 1,
            opacity: 0.9,
            fillColor: typeColor(props.event_type || ''),
            fillOpacity: fresh ? 0.95 : 0.85,
            recent: recent,
            pulse: pulse
        };
    }

    /**
     * Freshness classes live on the rendered path rather than in the Leaflet
     * options, because the path only exists once the marker is on the map.
     */
    function decorateMarker(marker, style) {
        var node = marker.getElement();
        if (!node) {
            return;
        }
        if (style.recent) {
            node.classList.add('is-recent-event');
        }
        if (style.pulse) {
            node.classList.add('is-pulse');
        }
    }

    function createEventMarker(latlng, props, now, budget) {
        var style = markerStyle(props, now, budget);
        var marker = L.circleMarker(latlng, style);
        marker.bindPopup(popupHtml(props, now), {
            maxWidth: 300,
            minWidth: 240,
            autoPan: true,
            autoPanPadding: [16, 16 + deckInset()]
        });
        return { marker: marker, style: style };
    }

    function renderFeatures(features, now) {
        markerLayer.clearLayers();

        var counts = {};
        var skipped = 0;
        var drawn = 0;
        var seen = {};
        var budget = { left: PULSE_MAX };

        features.forEach(function (feature) {
            if (!feature || !feature.properties) {
                skipped += 1;
                return;
            }
            var latlng = representativeLatLng(feature.geometry);
            if (!latlng) {
                skipped += 1;
                return;
            }

            var props = feature.properties;
            var type = props.event_type || '';
            counts[type] = (counts[type] || 0) + 1;

            var key = latlng[0].toFixed(3) + ',' + latlng[1].toFixed(3) + '|' + type;
            if (seen[key]) {
                return;
            }
            seen[key] = true;
            drawn += 1;

            var built = createEventMarker(latlng, props, now, budget);
            built.marker.addTo(markerLayer);
            decorateMarker(built.marker, built.style);
        });

        state.markerCount = drawn;
        state.typeCounts = counts;
        updateLegend();

        var typesPresent = Object.keys(counts).filter(function (type) {
            return counts[type] > 0;
        }).length;

        var text = formatNumber(state.markerCount) + ' events';
        if (typesPresent) {
            text += ' / ' + typesPresent + ' types';
        }
        if (skipped) {
            text += ' / ' + skipped + ' unplotted';
        }
        return text;
    }

    /* ------------------------------------------------------------------ */
    /* Data flow                                                           */
    /* ------------------------------------------------------------------ */

    function fetchEvents() {
        var seq = ++requestSeq;
        // First load queries worldwide (no bbox); every load after that is
        // scoped to the visible bounds.
        var bboxes = state.loaded ? visibleBboxes() : [];

        if (inflightController) {
            inflightController.abort();
        }
        inflightController = typeof AbortController !== 'undefined' ? new AbortController() : null;
        state.loaded = true;

        hideEmpty();
        showLoading(true);
        setStatus('loading\u2026', 'is-busy', 'live view / querying');

        var requests;
        var signal = inflightController ? inflightController.signal : null;
        if (bboxes.length === 0) {
            requests = [fetchBbox(null, signal)];
        } else {
            requests = bboxes.map(function (bbox) {
                return fetchBbox(bbox, signal);
            });
        }

        Promise.all(requests)
            .then(function (results) {
                if (seq !== requestSeq) {
                    return;
                }
                var features = [];
                results.forEach(function (batch) {
                    features = features.concat(batch);
                });
                hideBanner();
                showLoading(false);
                setStatus(renderFeatures(features, Date.now()), null, liveMeta());
                if (!state.markerCount) {
                    showEmpty();
                }
            })
            .catch(function (err) {
                if (seq !== requestSeq || (err && err.name === 'AbortError')) {
                    return;
                }
                markerLayer.clearLayers();
                state.markerCount = 0;
                state.typeCounts = {};
                updateLegend();
                showLoading(false);
                setStatus('unavailable', 'is-error', 'live view / failed');
                showBanner(err && err.message ? err.message : 'Failed to load events');
            });
    }

    function scheduleFetch() {
        // Replay holds a fixed window in the client, so panning must not refetch.
        if (state.mode !== 'live') {
            return;
        }
        if (debounceTimer) {
            clearTimeout(debounceTimer);
        }
        debounceTimer = setTimeout(function () {
            debounceTimer = null;
            fetchEvents();
        }, MOVE_DEBOUNCE_MS);
    }

    /* ------------------------------------------------------------------ */
    /* Replay mode                                                         */
    /* ------------------------------------------------------------------ */

    /**
     * Milliseconds since epoch for an API timestamp, or null when unusable.
     * Events without a usable start_time cannot be placed on the scrubber.
     */
    function parseTimestamp(value) {
        if (!value) {
            return null;
        }
        var ms = new Date(value).getTime();
        return isNaN(ms) ? null : ms;
    }

    /**
     * Ascending-by-time marker list for one replay window. Markers are built
     * but not attached, so the play head controls what is on screen. The
     * lat/lon+type dedupe mirrors live mode: a second event at the same spot
     * and type would land under the first marker anyway.
     */
    function buildTimeline(features, now) {
        var entries = [];
        var seen = {};
        var budget = { left: PULSE_MAX };

        features.forEach(function (feature) {
            if (!feature || !feature.properties) {
                return;
            }
            var latlng = representativeLatLng(feature.geometry);
            if (!latlng) {
                return;
            }
            var props = feature.properties;
            var time = parseTimestamp(props.start_time);
            if (time === null) {
                return;
            }
            var key = latlng[0].toFixed(3) + ',' + latlng[1].toFixed(3) + '|' + (props.event_type || '');
            if (seen[key]) {
                return;
            }
            seen[key] = true;
            var built = createEventMarker(latlng, props, now, budget);
            entries.push({
                time: time,
                type: props.event_type || '',
                marker: built.marker,
                style: built.style
            });
        });

        entries.sort(function (a, b) {
            return a.time - b.time;
        });
        return entries;
    }

    function windowHours() {
        for (var i = 0; i < REPLAY_WINDOWS.length; i += 1) {
            if (REPLAY_WINDOWS[i].value === replay.window) {
                return REPLAY_WINDOWS[i].hours;
            }
        }
        return REPLAY_WINDOWS[1].hours;
    }

    function windowLabel() {
        var match = REPLAY_WINDOWS.filter(function (entry) {
            return entry.value === replay.window;
        })[0];
        return match ? match.label.toLowerCase() : replay.window;
    }

    function buildReplayRequestUrl() {
        var params = new URLSearchParams();
        var hours = windowHours();
        if (hours !== null) {
            params.set('start', new Date(Date.now() - hours * MS_PER_HOUR).toISOString());
        }
        params.set('end', new Date(Date.now()).toISOString());
        if (state.eventType) {
            params.set('event_type', state.eventType);
        }
        var replayTiers = activeTiers();
        if (replayTiers.length > 0 && replayTiers.length < 4) {
            params.set('tiers', replayTiers.join(','));
        }
        params.set('limit', String(REPLAY_LIMIT));
        return REPLAY_ENDPOINT + '?' + params.toString();
    }

    /** Number of timeline entries at or before a given time (binary search). */
    function indexAtTime(time) {
        var timeline = replay.timeline;
        var low = 0;
        var high = timeline.length;
        while (low < high) {
            var mid = (low + high) >> 1;
            if (timeline[mid].time <= time) {
                low = mid + 1;
            } else {
                high = mid;
            }
        }
        return low;
    }

    function setReplayEnabled(enabled) {
        if (el.replayScrub) {
            el.replayScrub.disabled = !enabled;
        }
        if (el.replayPlay) {
            el.replayPlay.disabled = !enabled;
        }
        if (el.replayRestart) {
            el.replayRestart.disabled = !enabled;
        }
    }

    function setReplayNote(message) {
        if (!el.replayNote) {
            return;
        }
        el.replayNote.textContent = message || '';
        el.replayNote.hidden = !message;
    }

    function setReplayPlaying(playing) {
        if (!el.replayPlay) {
            return;
        }
        el.replayPlay.classList.toggle('is-playing', playing);
        el.replayPlay.setAttribute('aria-pressed', playing ? 'true' : 'false');
        el.replayPlay.setAttribute('aria-label', playing ? 'Pause replay' : 'Play replay');
    }

    function syncScrub(headTime) {
        var span = replay.rangeEnd - replay.rangeStart;
        // A one-event window has no span to interpolate, so fall back to
        // whether anything has been revealed.
        var ratio = span > 0
            ? clamp((headTime - replay.rangeStart) / span, 0, 1)
            : (replay.revealed > 0 ? 1 : 0);
        var percent = Math.round(ratio * 100);
        var step = Math.round(ratio * REPLAY_STEPS);
        if (el.replayScrub) {
            el.replayScrub.value = String(step);
            el.replayScrub.style.setProperty('--m-scrub-fill', percent + '%');
        }
        if (el.replayTrack) {
            el.replayTrack.style.setProperty('--m-scrub-fill', percent + '%');
        }
    }

    /**
     * Move the play head: attach markers the head has passed and detach the
     * ones it no longer covers, so scrubbing backwards rewinds the map.
     */
    function applyCursor(index, headTime) {
        var total = replay.timeline.length;
        var target = clamp(Math.round(index), 0, total);

        if (target < replay.revealed) {
            while (replay.revealed > target) {
                replay.revealed -= 1;
                var back = replay.timeline[replay.revealed];
                markerLayer.removeLayer(back.marker);
                state.typeCounts[back.type] = Math.max(0, (state.typeCounts[back.type] || 0) - 1);
                state.markerCount = Math.max(0, state.markerCount - 1);
            }
        } else {
            while (replay.revealed < target) {
                var entry = replay.timeline[replay.revealed];
                entry.marker.addTo(markerLayer);
                var node = entry.marker.getElement();
                if (node) {
                    node.classList.add('is-fresh');
                }
                decorateMarker(entry.marker, entry.style);
                state.typeCounts[entry.type] = (state.typeCounts[entry.type] || 0) + 1;
                state.markerCount += 1;
                replay.revealed += 1;
            }
        }

        replay.playHead = headTime;

        if (el.replayClock) {
            el.replayClock.textContent = formatClock(headTime);
        }
        if (el.replayProgress) {
            el.replayProgress.textContent = formatNumber(replay.revealed) + ' / ' +
                formatNumber(total) + ' events';
        }
        syncScrub(headTime);
        updateLegend();
    }

    function tick() {
        var elapsed = Date.now() - replay.playStamp;
        var head = replay.playOrigin + elapsed * replay.speed;
        if (head >= replay.rangeEnd) {
            applyCursor(replay.timeline.length, replay.rangeEnd);
            pausePlayback();
            return;
        }
        applyCursor(indexAtTime(head), head);
    }

    function startPlayback() {
        if (state.mode !== 'replay' || replay.loading || !replay.timeline.length) {
            return;
        }
        // Pressing play at the end restarts from the beginning.
        if (replay.revealed >= replay.timeline.length) {
            applyCursor(0, replay.rangeStart);
        }
        replay.playOrigin = replay.playHead;
        replay.playStamp = Date.now();
        replay.playing = true;
        setReplayPlaying(true);
        if (replayTimer) {
            clearInterval(replayTimer);
        }
        replayTimer = setInterval(tick, REPLAY_TICK_MS);
        tick();
    }

    function pausePlayback() {
        if (replayTimer) {
            clearInterval(replayTimer);
            replayTimer = null;
        }
        if (!replay.playing) {
            return;
        }
        replay.playing = false;
        setReplayPlaying(false);
    }

    function togglePlayback() {
        if (replay.playing) {
            pausePlayback();
        } else {
            startPlayback();
        }
    }

    function onScrubInput() {
        if (state.mode !== 'replay' || !replay.timeline.length || !el.replayScrub) {
            return;
        }
        pausePlayback();
        var span = replay.rangeEnd - replay.rangeStart;
        var step = clamp(toFiniteNumber(el.replayScrub.value, 0), 0, REPLAY_STEPS);
        // A single-event window has no span; any drag to the right reveals it.
        var head = span > 0
            ? replay.rangeStart + span * (step / REPLAY_STEPS)
            : (step > 0 ? replay.rangeEnd : replay.rangeStart);
        applyCursor(indexAtTime(head), head);
    }

    function onScrubJump() {
        if (state.mode !== 'replay' || !replay.timeline.length) {
            return;
        }
        pausePlayback();
        applyCursor(0, replay.rangeStart);
    }

    function clearHistogram() {
        if (!el.replayHist) {
            return;
        }
        while (el.replayHist.firstChild) {
            el.replayHist.removeChild(el.replayHist.firstChild);
        }
    }

    /** Event density behind the track, so the scrubber shows where the story is. */
    function buildHistogram() {
        if (!el.replayHist) {
            return;
        }
        clearHistogram();
        var timeline = replay.timeline;
        if (!timeline.length) {
            return;
        }
        var span = replay.rangeEnd - replay.rangeStart;
        var buckets = [];
        var i;
        for (i = 0; i < HIST_BUCKETS; i += 1) {
            buckets.push(0);
        }
        timeline.forEach(function (entry) {
            var ratio = span > 0 ? clamp((entry.time - replay.rangeStart) / span, 0, 1) : 0;
            buckets[Math.min(HIST_BUCKETS - 1, Math.floor(ratio * HIST_BUCKETS))] += 1;
        });
        var peak = buckets.reduce(function (max, value) {
            return Math.max(max, value);
        }, 0) || 1;
        buckets.forEach(function (value, index) {
            var bar = document.createElement('i');
            bar.className = 'map-replay-bar';
            // 12% keeps an empty bucket visible as a baseline tick.
            var height = Math.max(12, Math.round((value / peak) * 100));
            bar.style.height = height + '%';
            if (!value) {
                bar.classList.add('is-empty');
            }
            bar.setAttribute('data-bucket', String(index));
            el.replayHist.appendChild(bar);
        });
    }

    function resetReplayView() {
        markerLayer.clearLayers();
        replay.timeline = [];
        replay.revealed = 0;
        replay.rangeStart = 0;
        replay.rangeEnd = 0;
        replay.truncated = false;
        replay.playHead = 0;
        replay.playOrigin = 0;
        replay.playStamp = 0;
        state.markerCount = 0;
        state.typeCounts = {};
        updateLegend();
        setReplayEnabled(false);
        setReplayPlaying(false);
        setReplayNote('');
        clearHistogram();
        if (el.replayClock) {
            el.replayClock.textContent = '--:--';
        }
        if (el.replayProgress) {
            el.replayProgress.textContent = '0 / 0 events';
        }
        if (el.replayStart) {
            el.replayStart.textContent = '--';
        }
        if (el.replayEnd) {
            el.replayEnd.textContent = '--';
        }
        if (el.replayScrub) {
            el.replayScrub.value = '0';
        }
        syncScrub(0);
    }

    function loadReplay() {
        var seq = ++replaySeq;
        pausePlayback();
        resetReplayView();
        replay.loading = true;

        if (replayController) {
            replayController.abort();
        }
        replayController = typeof AbortController !== 'undefined' ? new AbortController() : null;

        hideEmpty();
        showLoading(true);
        setStatus('loading replay\u2026', 'is-busy', replayMeta());
        setReplayNote('Loading replay window\u2026');

        var options = { headers: { Accept: 'application/json' }, credentials: 'same-origin' };
        if (replayController) {
            options.signal = replayController.signal;
        }

        fetch(buildReplayRequestUrl(), options)
            .then(function (response) {
                return response
                    .json()
                    .catch(function () {
                        throw new Error('Request failed (HTTP ' + response.status + ')');
                    })
                    .then(function (payload) {
                        if (payload && (payload.error || payload.detail)) {
                            throw new Error(String(payload.error || payload.detail));
                        }
                        if (!response.ok) {
                            throw new Error('Request failed (HTTP ' + response.status + ')');
                        }
                        if (!payload || payload.type !== 'FeatureCollection' ||
                            !Array.isArray(payload.features)) {
                            throw new Error('Unexpected response from ' + REPLAY_ENDPOINT);
                        }
                        return payload;
                    });
            })
            .then(function (payload) {
                if (seq !== replaySeq || state.mode !== 'replay') {
                    return;
                }
                replay.loading = false;
                replay.truncated = !!payload.truncated;
                replay.maxLimit = payload.max_limit || REPLAY_MAX_LIMIT;
                replay.timeline = buildTimeline(payload.features, Date.now());
                hideBanner();
                showLoading(false);

                if (!replay.timeline.length) {
                    setReplayNote(state.eventType
                        ? 'No events in this window for ' +
                            typeLabel(state.eventType).toLowerCase() +
                            '. Try a wider window, or show all types.'
                        : 'No events in this window. Try a wider window.');
                    setStatus('0 events', null, replayMeta());
                    return;
                }

                replay.rangeStart = replay.timeline[0].time;
                replay.rangeEnd = replay.timeline[replay.timeline.length - 1].time;
                if (el.replayStart) {
                    el.replayStart.textContent = formatBound(replay.rangeStart);
                }
                if (el.replayEnd) {
                    el.replayEnd.textContent = formatBound(replay.rangeEnd);
                }
                buildHistogram();
                setReplayEnabled(true);
                applyCursor(0, replay.rangeStart);
                // Status stays static during playback; the scrubber shows progress.
                setStatus(formatNumber(replay.timeline.length) + ' events' +
                    (replay.truncated ? ' (capped)' : ''), null, replayMeta());
                setReplayNote(replay.truncated
                    ? 'Window truncated at the ' + formatNumber(replay.maxLimit) +
                        ' event API cap. Narrow the window or filter by type to see the rest.'
                    : '');
            })
            .catch(function (err) {
                if (seq !== replaySeq || (err && err.name === 'AbortError')) {
                    return;
                }
                replay.loading = false;
                resetReplayView();
                showLoading(false);
                setStatus('unavailable', 'is-error', 'replay window / failed');
                showBanner(err && err.message ? err.message : 'Failed to load replay');
            });
    }

    /** Legend and zoom controls sit above the scrubber rather than under it. */
    function measureReplayPanel() {
        if (!el.body) {
            return;
        }
        var height = el.replay && !el.replay.hidden ? el.replay.offsetHeight : 0;
        el.body.style.setProperty('--m-replay-offset', (height ? height + 12 : 0) + 'px');
    }

    function buildModeToggle() {
        Array.prototype.forEach.call(el.modeButtons, function (button) {
            button.addEventListener('click', function () {
                var mode = button.getAttribute('data-mode');
                if (mode && mode !== state.mode) {
                    setMode(mode);
                }
            });
        });
    }

    function buildReplayControls() {
        if (!el.replay) {
            return;
        }
        if (el.replayPlay) {
            el.replayPlay.addEventListener('click', togglePlayback);
        }
        if (el.replayRestart) {
            el.replayRestart.addEventListener('click', onScrubJump);
        }
        if (el.replayScrub) {
            el.replayScrub.addEventListener('input', onScrubInput);
        }
        if (el.replaySpeed) {
            replay.speed = clamp(toFiniteNumber(el.replaySpeed.value, 1), 0.25, 64);
            el.replaySpeed.addEventListener('change', function () {
                replay.speed = clamp(toFiniteNumber(el.replaySpeed.value, 1), 0.25, 64);
                // Rebase so a speed change takes effect on the next tick with no jump.
                if (replay.playing) {
                    replay.playOrigin = replay.playHead;
                    replay.playStamp = Date.now();
                }
            });
        }
        if (el.replayWindow) {
            replay.window = el.replayWindow.value || replay.window;
            el.replayWindow.addEventListener('change', function () {
                replay.window = el.replayWindow.value || '7d';
                if (state.mode === 'replay') {
                    loadReplay();
                }
            });
        }
        el.replay.addEventListener('keydown', function (evt) {
            if ((evt.key === ' ' || evt.key === 'Spacebar') && evt.target === el.replayScrub) {
                evt.preventDefault();
                togglePlayback();
            }
        });
        setReplayEnabled(false);
    }

    function setMode(mode) {
        if (state.mode === mode) {
            return;
        }
        if (inflightController) {
            inflightController.abort();
            inflightController = null;
        }
        if (replayController) {
            replayController.abort();
            replayController = null;
        }
        replaySeq += 1;
        pausePlayback();
        state.mode = mode;

        Array.prototype.forEach.call(el.modeButtons, function (button) {
            var active = button.getAttribute('data-mode') === mode;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });

        if (el.modeToggle) {
            el.modeToggle.setAttribute('data-mode', mode);
        }
        if (el.body) {
            el.body.classList.toggle('is-replay', mode === 'replay');
        }
        if (el.replay) {
            el.replay.hidden = mode !== 'replay';
        }
        measureReplayPanel();

        if (mode === 'replay') {
            hideEmpty();
            resetReplayView();
            loadReplay();
        } else {
            resetReplayView();
            hideEmpty();
            // Live mode's first query is worldwide; re-enter it that way.
            state.loaded = false;
            fetchEvents();
        }
    }

    /* ------------------------------------------------------------------ */
    /* Init                                                                */
    /* ------------------------------------------------------------------ */

    function init() {
        if (!el.map || typeof L === 'undefined') {
            setStatus('unavailable', 'is-error', 'map library failed');
            showBanner('Map library failed to load');
            return;
        }

        /*
         * Note what is deliberately absent: no crossOrigin on the tile layer
         * (nothing reads tile pixels back, and asking for a CORS image only
         * adds a failure mode on iOS) and no canvas renderer (the replay reveal
         * animation needs a real path element per marker to pulse).
         */
        map = L.map(el.map, {
            zoomControl: false,
            worldCopyJump: true,
            minZoom: 2,
            maxZoom: 18,
            attributionControl: true
        }).setView([20, 0], 2);

        L.tileLayer(TILE_URL, {
            attribution: TILE_ATTRIBUTION,
            maxZoom: 19
        }).addTo(map);

        L.control.zoom({ position: 'bottomright' }).addTo(map);
        markerLayer = L.layerGroup().addTo(map);

        buildTypeFilter();
        buildLegend();
        buildModeToggle();
        buildReplayControls();
        measureReplayPanel();
        syncLegendSelection();

        if (el.bannerClose) {
            el.bannerClose.addEventListener('click', hideBanner);
        }
        if (el.bannerRetry) {
            el.bannerRetry.addEventListener('click', function () {
                hideBanner();
                if (state.mode === 'replay') {
                    loadReplay();
                } else {
                    scheduleFetch();
                }
            });
        }
        if (el.emptyPrimary) {
            el.emptyPrimary.addEventListener('click', onEmptyPrimary);
        }

        if (el.legend && window.matchMedia && window.matchMedia('(max-width: 900px)').matches) {
            el.legend.classList.add('is-collapsed');
            if (el.legendHead) {
                el.legendHead.setAttribute('aria-expanded', 'false');
            }
        }

        map.on('moveend zoomend', scheduleFetch);
        window.addEventListener('orientationchange', function () {
            setTimeout(function () {
                map.invalidateSize();
                measureReplayPanel();
            }, 250);
        });
        window.addEventListener('resize', measureReplayPanel);

        fetchEvents();

        // iOS Safari settles its viewport after fonts and the safe area land, so
        // one extra size pass keeps the first tile row from being cropped.
        setTimeout(function () {
            if (!map) {
                return;
            }
            map.invalidateSize();
            measureReplayPanel();
        }, 250);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();