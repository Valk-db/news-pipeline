/**
 * Globe Interaction Module
 * Handles click/tooltip, story panel sync, search, and clustering
 */

let searchDebounceTimer = null;
let recentEventsCache = [];

// Initialize interaction handlers
function initInteractions() {
    // Search input
    const searchInput = document.getElementById('event-search');
    if (searchInput) {
        searchInput.addEventListener('input', debounce(handleSearch, 300));
    }

    // Panel close buttons
    document.getElementById('panel-close')?.addEventListener('click', hideEventDetail);
    document.getElementById('timeline-close')?.addEventListener('click', () => {
        document.getElementById('timeline-panel').classList.remove('open');
        window.GlobeTimeline?.pauseTimeline?.();
    });

    // Toolbar buttons
    document.getElementById('btn-reset-view')?.addEventListener('click', window.GlobeCore?.resetView);
    document.getElementById('btn-fit-events')?.addEventListener('click', window.GlobeCore?.fitEvents);
    document.getElementById('btn-toggle-clustering')?.addEventListener('click', toggleClustering);
    document.getElementById('btn-toggle-timeline')?.addEventListener('click', window.GlobeTimeline?.toggleTimelinePanel);

    // Quick time buttons
    document.querySelectorAll('.quick-time button').forEach(btn => {
        btn.addEventListener('click', () => {
            const hours = parseInt(btn.dataset.hours);
            window.GlobeLayers?.setQuickTime(hours);
        });
    });

    // Time controls
    document.getElementById('time-apply')?.addEventListener('click', window.GlobeLayers?.applyTimeFilter);
    document.getElementById('time-reset')?.addEventListener('click', window.GlobeLayers?.resetTimeFilter);

    // Event type buttons
    document.getElementById('event-type-select-all')?.addEventListener('click', window.GlobeLayers?.toggleAllEventTypes);
    document.getElementById('event-type-clear-all')?.addEventListener('click', window.GlobeLayers?.clearAllEventTypes);

    // Filters
    document.getElementById('min-confidence')?.addEventListener('input', (e) => {
        window.GlobeLayers?.updateConfidenceFilter(e.target.value);
    });
    document.getElementById('max-events')?.addEventListener('change', (e) => {
        window.GlobeLayers?.updateMaxEventsFilter(e.target.value);
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboardShortcuts);

    // Load recent events and stats
    loadRecentEvents();
    loadGlobeStats();

    // Setup periodic refresh
    setInterval(() => {
        if (!document.hidden && window.GlobeCore?.viewer) {
            const viewer = window.GlobeCore.viewer();
            if (viewer && !viewer.isDestroyed()) {
                loadRecentEvents();
                loadGlobeStats();
            }
        }
    }, 60000); // Every minute
}

// Debounce helper
function debounce(fn, delay) {
    return (...args) => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => fn(...args), delay);
    };
}

// Handle search
async function handleSearch(e) {
    const query = e.target.value.trim().toLowerCase();
    const resultsContainer = document.getElementById('search-results');

    if (query.length < 2) {
        resultsContainer.innerHTML = '';
        return;
    }

    const events = window.GlobeCore?.eventData?.() || [];
    const filtered = events.filter(feature => {
        const props = feature.properties || {};
        const searchable = [
            props.location_name,
            props.event_type,
            ...(props.entities ? Object.values(props.entities).flat() : [])
        ].filter(Boolean).join(' ').toLowerCase();

        return searchable.includes(query);
    }).slice(0, 10);

    resultsContainer.innerHTML = filtered.map(feature => {
        const props = feature.properties || {};
        const color = window.GlobeCore?.EVENT_TYPE_COLORS?.[props.event_type] || '#e74c3c';

        return `
            <div class="event-list-item" onclick="flyToEvent('${props.event_id}'); hideSearchResults()">
                <div class="event-marker" style="background: ${color}"></div>
                <div class="event-info">
                    <div class="event-title">${props.location_name || 'Unknown'}</div>
                    <div class="event-meta">
                        <span>${props.event_type || 'unknown'}</span>
                        <span>${props.start_time ? new Date(props.start_time).toLocaleDateString() : ''}</span>
                        <span>${(props.confidence * 100).toFixed(0)}% conf</span>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function hideSearchResults() {
    document.getElementById('event-search').value = '';
    document.getElementById('search-results').innerHTML = '';
}

// Load recent events for sidebar
async function loadRecentEvents() {
    try {
        const response = await fetch('/api/globe/events?limit=20', {
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) return;

        const data = await response.json();
        const events = data.features || [];

        recentEventsCache = events;

        const container = document.getElementById('recent-events');
        const countEl = document.getElementById('recent-count');

        if (container) {
            container.innerHTML = events.map(feature => {
                const props = feature.properties || {};
                const color = window.GlobeCore?.EVENT_TYPE_COLORS?.[props.event_type] || '#e74c3c';

                return `
                    <div class="event-list-item" onclick="flyToEvent('${props.event_id}')">
                        <div class="event-marker" style="background: ${color}"></div>
                        <div class="event-info">
                            <div class="event-title">${props.location_name || 'Unknown'}</div>
                            <div class="event-meta">
                                <span>${props.event_type || 'unknown'}</span>
                                <span>${props.start_time ? new Date(props.start_time).toLocaleString() : ''}</span>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        if (countEl) {
            countEl.textContent = `(${events.length})`;
        }
    } catch (error) {
        console.error('Failed to load recent events:', error);
    }
}

// Load globe stats
async function loadGlobeStats() {
    try {
        const response = await fetch('/api/globe/stats', {
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) return;

        const data = await response.json();

        const statTotal = document.getElementById('stat-total');
        if (statTotal) statTotal.textContent = data.total_events || 0;

        // Calculate last 24h
        const eventsByDay = data.events_by_day || {};
        const last24h = Object.values(eventsByDay).reduce((a, b) => a + b, 0);
        const stat24h = document.getElementById('stat-24h');
        if (stat24h) stat24h.textContent = last24h;

        // Top locations
        const topLocations = data.top_locations || [];
        const container = document.getElementById('top-locations');
        if (container) {
            container.innerHTML = topLocations.map(loc => `
                <div class="location-item">
                    <span class="location-name">${loc.name}</span>
                    <span class="location-count">${loc.count}</span>
                </div>
            `).join('');
        }

        // Initialize event type filters if not done
        if (document.getElementById('event-type-filters')?.children.length === 0) {
            window.GlobeLayers?.renderEventTypeFilters?.();
        }
    } catch (error) {
        console.error('Failed to load globe stats:', error);
    }
}

// Toggle clustering
function toggleClustering() {
    const btn = document.getElementById('btn-toggle-clustering');
    const enabled = !window.globeClusteringEnabled;
    window.globeClusteringEnabled = enabled;

    btn.classList.toggle('active', enabled);
    btn.title = enabled ? 'Disable Clustering (C)' : 'Enable Clustering (C)';

    if (enabled) {
        window.GlobeCore?.updateClustering?.();
    } else {
        // Re-render all events without clustering
        window.GlobeCore?.renderEvents?.(window.GlobeCore?.eventData?.());
    }
}

// Keyboard shortcuts
function handleKeyboardShortcuts(e) {
    // Ignore if typing in input
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

    const key = e.key.toLowerCase();

    switch (key) {
        case 'r':
            e.preventDefault();
            window.GlobeCore?.resetView?.();
            break;
        case 'f':
            e.preventDefault();
            window.GlobeCore?.fitEvents?.();
            break;
        case 'c':
            e.preventDefault();
            toggleClustering();
            break;
        case 't':
            e.preventDefault();
            window.GlobeTimeline?.toggleTimelinePanel?.();
            break;
        case 'escape':
            hideEventDetail();
            document.getElementById('timeline-panel')?.classList.remove('open');
            hideSearchResults();
            break;
    }
}

// Fly to event (exposed globally for onclick handlers)
function flyToEvent(eventId) {
    const viewer = window.GlobeCore?.viewer?.();
    if (!viewer || viewer.isDestroyed()) return;

    const entity = window.GlobeCore?.eventEntities?.values?.find?.(e =>
        e.properties?.event_id === eventId
    );

    if (entity && entity.position) {
        viewer.camera.flyTo({
            destination: entity.position.getValue(viewer.clock.currentTime),
            duration: 1.5,
            offset: new Cesium.HeadingPitchRange(0, -Cesium.Math.PI_OVER_FOUR, 50000)
        });
    }
}

// Hide event detail panel
function hideEventDetail() {
    const panel = document.getElementById('event-detail-panel');
    if (panel) panel.classList.remove('open');
}

// Open story from event
function openStory(storyId) {
    window.location.href = `/story/${storyId}/edit`;
}

// Event type color legend for UI
function renderEventTypeLegend() {
    const container = document.getElementById('event-type-legend');
    if (!container) return;

    const eventTypes = Object.entries(window.GlobeCore?.EVENT_TYPE_COLORS || {});

    container.innerHTML = eventTypes.map(([type, color]) => `
        <div class="event-type-legend-item">
            <div class="color-dot" style="background: ${color}"></div>
            <span>${type.charAt(0).toUpperCase() + type.slice(1)}</span>
        </div>
    `).join('');
}

// Cluster events at current zoom level
function clusterEvents() {
    const viewer = window.GlobeCore?.viewer?.();
    if (!viewer || viewer.isDestroyed()) return;

    const entities = window.GlobeCore?.eventEntities?.values;

    if (!entities || entities.length === 0) return;

    // Get visible entities
    const visibleEntities = entities.filter(e => {
        if (!e.position) return false;
        const pos = e.position.getValue(viewer.clock.currentTime);
        return viewer.scene.camera.frustum.contains(pos) !== Cesium.Intersect.OUTSIDE;
    });

    // Simple grid-based clustering
    const clusters = new Map();
    const gridSize = 0.5; // degrees

    visibleEntities.forEach(entity => {
        const pos = entity.position.getValue(viewer.clock.currentTime);
        const cartographic = viewer.scene.globe.ellipsoid.cartesianToCartographic(pos);

        if (!cartographic) return;

        const lat = Cesium.Math.toDegrees(cartographic.latitude);
        const lon = Cesium.Math.toDegrees(cartographic.longitude);

        const gridLat = Math.floor(lat / gridSize) * gridSize;
        const gridLon = Math.floor(lon / gridSize) * gridSize;
        const key = `${gridLat},${gridLon}`;

        if (!clusters.has(key)) {
            clusters.set(key, []);
        }
        clusters.get(key).push(entity);
    });

    // Hide individual entities in clusters > 1
    clusters.forEach((clusterEntities, key) => {
        if (clusterEntities.length > 1) {
            clusterEntities.forEach(e => e.show = false);
            // Show cluster marker (could create a new entity)
            showClusterMarker(clusterEntities, key);
        } else {
            clusterEntities[0].show = true;
        }
    });
}

function showClusterMarker(entities, key) {
    const viewer = window.GlobeCore?.viewer?.();
    if (!viewer || viewer.isDestroyed()) return;

    // Calculate center
    const positions = entities.map(e => e.position.getValue(viewer.clock.currentTime));
    const center = Cesium.Cartesian3.fromDegrees(
        positions.reduce((sum, p) => {
            const c = viewer.scene.globe.ellipsoid.cartesianToCartographic(p);
            return sum + Cesium.Math.toDegrees(c.longitude);
        }, 0) / positions.length,
        positions.reduce((sum, p) => {
            const c = viewer.scene.globe.ellipsoid.cartesianToCartographic(p);
            return sum + Cesium.Math.toDegrees(c.latitude);
        }, 0) / positions.length
    );

    // Create or update cluster entity
    // This is simplified - in production would reuse entities
}

window.GlobeInteractions = {
    initInteractions,
    loadRecentEvents,
    loadGlobeStats,
    handleSearch,
    toggleClustering,
    flyToEvent,
    hideEventDetail,
    openStory,
    renderEventTypeLegend,
    clusterEvents
};