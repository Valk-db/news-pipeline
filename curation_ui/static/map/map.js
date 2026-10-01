/**
 * Flat map page controller.
 *
 * Plots events from GET /api/globe/events as circle markers on a Leaflet map,
 * re-querying with the visible bbox when the viewport moves.
 *
 * A second mode, replay, swaps the viewport-scoped query for GET /api/map/replay:
 * one chronological window fetched up front, then revealed over time by a
 * bottom scrubber so a story can be watched unfolding across geography.
 */
(function () {
    'use strict';

    var API_ENDPOINT = '/api/globe/events';
    var REPLAY_ENDPOINT = '/api/map/replay';
    var TILE_URL = 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
    var TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';
    var TILE_SUBDOMAINS = 'abcd';
    var MOVE_DEBOUNCE_MS = 400;
    var EVENT_LIMIT = 500;
    var FALLBACK_COLOR = '#7f8c8d';
    var UNKNOWN_COLOR = '#8899aa';

    /* Mirrors the replay cap in curation_ui/main.py. MAP_REPLAY_MAX_LIMIT (1000) is
     * the server hard cap and is what a wide window is most likely to hit;
     * the response carries max_limit and truncated so the note below the
     * scrubber can say so. */
    var REPLAY_LIMIT = 1000;
    var REPLAY_MAX_LIMIT = 1000;
    var REPLAY_STEPS = 1000;
    var REPLAY_TICK_MS = 100;
    var MS_PER_HOUR = 3600000;

    var REPLAY_WINDOWS = [
        { value: '24h', label: 'Last 24h', hours: 24 },
        { value: '7d', label: 'Last 7d', hours: 24 * 7 },
        { value: '30d', label: 'Last 30d', hours: 24 * 30 },
        { value: 'all', label: 'All time', hours: null }
    ];

    /**
     * Event type vocabulary mirrored from src/schema/models.py EventType,
     * with the same colors as the globe page.
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
        modeButtons: document.querySelectorAll('.map-mode-btn'),
        status: document.getElementById('map-status'),
        statusText: document.getElementById('map-status-text'),
        banner: document.getElementById('map-banner'),
        bannerMessage: document.getElementById('map-banner-message'),
        bannerClose: document.getElementById('map-banner-close'),
        legend: document.getElementById('map-legend'),
        legendHead: document.getElementById('map-legend-head'),
        legendList: document.getElementById('map-legend-list'),
        replay: document.getElementById('map-replay'),
        replayPlay: document.getElementById('map-replay-play'),
        replayClock: document.getElementById('map-replay-clock'),
        replayProgress: document.getElementById('map-replay-progress'),
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
    var state = { eventType: '', markerCount: 0, typeCounts: {}, loaded: false, mode: 'live' };

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
            return 'Unclassified';
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
            return '—';
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
            return '—';
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
    /* Status / banner / legend                                            */
    /* ------------------------------------------------------------------ */

    function setStatus(text, mode) {
        if (el.statusText) {
            el.statusText.textContent = text;
        }
        if (!el.status) {
            return;
        }
        el.status.classList.remove('is-busy', 'is-error');
        if (mode) {
            el.status.classList.add(mode);
        }
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
            state.eventType = el.typeSelect.value || '';
            hideBanner();
            if (state.mode === 'replay') {
                loadReplay();
            } else {
                scheduleFetch();
            }
        });
    }

    function buildLegend() {
        if (!el.legendList) {
            return;
        }
        EVENT_TYPES.forEach(function (entry) {
            var item = document.createElement('li');
            item.className = 'map-legend-item is-empty';
            item.dataset.eventType = entry.value;

            var swatch = document.createElement('span');
            swatch.className = 'map-legend-swatch';
            swatch.style.background = entry.color;

            var label = document.createElement('span');
            label.className = 'map-legend-label';
            label.textContent = entry.label;

            var count = document.createElement('span');
            count.className = 'map-legend-count';
            count.textContent = '0';

            item.appendChild(swatch);
            item.appendChild(label);
            item.appendChild(count);
            el.legendList.appendChild(item);
        });

        if (el.legendHead) {
            el.legendHead.addEventListener('click', function () {
                el.legend.classList.toggle('is-collapsed');
            });
        }
    }

    function updateLegend() {
        if (!el.legendList) {
            return;
        }
        var items = el.legendList.querySelectorAll('.map-legend-item');
        Array.prototype.forEach.call(items, function (item) {
            var type = item.dataset.eventType;
            var count = state.typeCounts[type] || 0;
            item.querySelector('.map-legend-count').textContent = formatNumber(count);
            item.classList.toggle('is-empty', count === 0);
        });
    }

    /* ------------------------------------------------------------------ */
    /* Rendering                                                           */
    /* ------------------------------------------------------------------ */

    function popupHtml(props) {
        var type = props.event_type || '';
        var confidence = toFiniteNumber(props.confidence, 0);
        var percent = Math.round(clamp(confidence, 0, 1) * 100);
        var sources = formatNumber(props.source_count);

        var rows = [
            ['Event type', escapeHtml(typeLabel(type))],
            ['Confidence', percent + '%'],
            ['Sources', sources],
            ['Start time', escapeHtml(formatTime(props.start_time))]
        ];

        if (props.tier1_source_count !== null && props.tier1_source_count !== undefined) {
            rows.push(['Tier-1 sources', formatNumber(props.tier1_source_count)]);
        }

        var html = '<div class="map-popup">';
        html += '<div class="map-popup-title">' +
            escapeHtml(props.location_name || 'Unknown location');
        html += '<span class="map-popup-type">' +
            '<span class="map-popup-dot" style="background:' + typeColor(type) + '"></span>' +
            escapeHtml(typeLabel(type)) + '</span></div>';
        html += '<dl class="map-popup-rows">';
        rows.forEach(function (row) {
            html += '<div class="map-popup-row"><dt>' + row[0] + '</dt>';
            if (row[0] === 'Confidence') {
                html += '<dd><span class="map-popup-bar"><span style="width:' + percent +
                    '%"></span></span>' + row[1] + '</dd>';
            } else {
                html += '<dd>' + row[1] + '</dd>';
            }
            html += '</div>';
        });
        html += '</dl></div>';
        return html;
    }

    function renderFeatures(features) {
        markerLayer.clearLayers();

        var counts = {};
        var skipped = 0;
        var drawn = 0;
        var seen = {};

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

            createEventMarker(latlng, props).addTo(markerLayer);
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

        setStatus('loading…', 'is-busy');

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
                setStatus(renderFeatures(features));
            })
            .catch(function (err) {
                if (seq !== requestSeq || (err && err.name === 'AbortError')) {
                    return;
                }
                markerLayer.clearLayers();
                state.markerCount = 0;
                state.typeCounts = {};
                updateLegend();
                setStatus('unavailable', 'is-error');
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

    function markerOptions(props) {
        var confidence = clamp(toFiniteNumber(props.confidence, 0.5), 0, 1);
        return {
            radius: 4 + Math.round(confidence * 4),
            className: 'map-marker',
            color: '#05080c',
            weight: 1,
            opacity: 0.9,
            fillColor: typeColor(props.event_type || ''),
            fillOpacity: 0.85
        };
    }

    /** Same marker + popup as live mode, so both modes look identical. */
    function createEventMarker(latlng, props) {
        var marker = L.circleMarker(latlng, markerOptions(props));
        marker.bindPopup(popupHtml(props), {
            maxWidth: 280,
            minWidth: 200,
            autoPan: true,
            autoPanPadding: [16, 16]
        });
        return marker;
    }

    /**
     * Ascending-by-time marker list for one replay window. Markers are built
     * but not attached, so the play head controls what is on screen. The
     * lat/lon+type dedupe mirrors live mode: a second event at the same spot
     * and type would land under the first marker anyway.
     */
    function buildTimeline(features) {
        var entries = [];
        var seen = {};

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
            entries.push({
                time: time,
                type: props.event_type || '',
                marker: createEventMarker(latlng, props)
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
    }

    function setReplayNote(message) {
        if (!el.replayNote) {
            return;
        }
        el.replayNote.textContent = message || '';
        el.replayNote.hidden = !message;
    }

    function setReplayPlaying(playing) {
        if (el.replayPlay) {
            el.replayPlay.classList.toggle('is-playing', playing);
            el.replayPlay.setAttribute('aria-pressed', playing ? 'true' : 'false');
            el.replayPlay.setAttribute('aria-label', playing ? 'Pause replay' : 'Play replay');
        }
    }

    function syncScrub(headTime) {
        if (!el.replayScrub) {
            return;
        }
        var span = replay.rangeEnd - replay.rangeStart;
        // A one-event window has no span to interpolate, so fall back to
        // whether anything has been revealed.
        var ratio = span > 0
            ? clamp((headTime - replay.rangeStart) / span, 0, 1)
            : (replay.revealed > 0 ? 1 : 0);
        var step = Math.round(ratio * REPLAY_STEPS);
        el.replayScrub.value = String(step);
        el.replayScrub.style.setProperty('--m-scrub-fill', Math.round(ratio * 100) + '%');
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
                state.typeCounts[entry.type] = (state.typeCounts[entry.type] || 0) + 1;
                state.markerCount += 1;
                replay.revealed += 1;
            }
        }

        replay.playHead = headTime;

        if (el.replayClock) {
            el.replayClock.textContent = formatTime(new Date(headTime).toISOString());
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
        if (el.replayClock) {
            el.replayClock.textContent = '—';
        }
        if (el.replayProgress) {
            el.replayProgress.textContent = '0 / 0 events';
        }
        if (el.replayStart) {
            el.replayStart.textContent = '—';
        }
        if (el.replayEnd) {
            el.replayEnd.textContent = '—';
        }
        if (el.replayScrub) {
            el.replayScrub.value = '0';
        }
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

        setStatus('loading replay…', 'is-busy');
        setReplayNote('Loading replay window…');

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
                replay.timeline = buildTimeline(payload.features);
                hideBanner();

                if (!replay.timeline.length) {
                    setReplayNote('No events in this window. Try a wider window.');
                    setStatus('0 events');
                    return;
                }

                replay.rangeStart = replay.timeline[0].time;
                replay.rangeEnd = replay.timeline[replay.timeline.length - 1].time;
                if (el.replayStart) {
                    el.replayStart.textContent = formatTime(new Date(replay.rangeStart).toISOString());
                }
                if (el.replayEnd) {
                    el.replayEnd.textContent = formatTime(new Date(replay.rangeEnd).toISOString());
                }
                setReplayEnabled(true);
                applyCursor(0, replay.rangeStart);
                // Status stays static during playback; the scrubber shows progress.
                setStatus(formatNumber(replay.timeline.length) + ' events' +
                    (replay.truncated ? ' (capped)' : ''));
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
                setStatus('unavailable', 'is-error');
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

        if (el.body) {
            el.body.classList.toggle('is-replay', mode === 'replay');
        }
        if (el.replay) {
            el.replay.hidden = mode !== 'replay';
        }
        measureReplayPanel();

        if (mode === 'replay') {
            resetReplayView();
            loadReplay();
        } else {
            resetReplayView();
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
            setStatus('unavailable', 'is-error');
            showBanner('Map library failed to load');
            return;
        }

        map = L.map(el.map, {
            zoomControl: false,
            worldCopyJump: true,
            minZoom: 2,
            maxZoom: 18,
            attributionControl: true
        }).setView([20, 0], 2);

        L.tileLayer(TILE_URL, {
            attribution: TILE_ATTRIBUTION,
            subdomains: TILE_SUBDOMAINS,
            maxZoom: 19,
            crossOrigin: true
        }).addTo(map);

        L.control.zoom({ position: 'bottomright' }).addTo(map);
        markerLayer = L.layerGroup().addTo(map);

        buildTypeFilter();
        buildLegend();
        buildModeToggle();
        buildReplayControls();
        measureReplayPanel();

        if (el.bannerClose) {
            el.bannerClose.addEventListener('click', hideBanner);
        }

        if (window.matchMedia && window.matchMedia('(max-width: 720px)').matches) {
            el.legend.classList.add('is-collapsed');
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
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();