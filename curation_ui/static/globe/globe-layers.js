/**
 * Globe Layers Module
 * Handles layer management, filtering, and styling
 */

let layers = [];
let activeLayers = new Set();

// Load layers from API
async function loadLayers() {
    try {
        const response = await fetch('/api/globe/layers', {
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();
        layers = data.layers || [];

        renderLayerList();
        applyDefaultLayers();

        return layers;
    } catch (error) {
        console.error('Failed to load layers:', error);
        return [];
    }
}

// Render layer list in sidebar
function renderLayerList() {
    const container = document.getElementById('layer-list');
    if (!container) return;

    container.innerHTML = layers.map(layer => {
        const style = layer.style || {};
        const color = style.color || '#e74c3c';
        const isVisible = layer.is_visible !== false;
        const isDefault = layer.is_default === true;

        if (isDefault || isVisible) {
            activeLayers.add(layer.id);
        }

        return `
            <div class="layer-item" data-layer-id="${layer.id}">
                <input type="checkbox" ${isVisible ? 'checked' : ''} onchange="toggleLayer('${layer.id}', this.checked)">
                <div class="layer-color" style="background: ${color}"></div>
                <span>${layer.name}</span>
                ${isDefault ? '<span class="badge badge-xs">Default</span>' : ''}
            </div>
        `;
    }).join('');
}

// Apply default visible layers
function applyDefaultLayers() {
    layers.forEach(layer => {
        if (layer.is_default || layer.is_visible) {
            applyLayerFilter(layer);
        }
    });
}

// Toggle layer visibility
function toggleLayer(layerId, visible) {
    const layer = layers.find(l => l.id === layerId);
    if (!layer) return;

    if (visible) {
        activeLayers.add(layerId);
        applyLayerFilter(layer);
    } else {
        activeLayers.delete(layerId);
        removeLayerFilter(layer);
    }

    // Reload events with new filters
    if (window.GlobeCore && window.GlobeCore.loadEvents) {
        window.GlobeCore.loadEvents(window.GlobeCore.currentFilters());
    }
}

// Apply layer filter to current event filters
function applyLayerFilter(layer) {
    const filters = window.GlobeCore.currentFilters();
    const criteria = layer.filter_criteria || {};

    if (criteria.event_type) {
        filters.eventTypes = [...new Set([...filters.eventTypes, ...criteria.event_type])];
    }
    if (criteria.min_confidence) {
        filters.minConfidence = Math.max(filters.minConfidence, criteria.min_confidence);
    }
    if (criteria.max_events) {
        filters.maxEvents = Math.min(filters.maxEvents, criteria.max_events);
    }
}

// Remove layer filter (simplified - in practice would need more sophisticated filter management)
function removeLayerFilter(layer) {
    // For simplicity, reload all events and reapply all active layer filters
    const filters = { ...window.GlobeCore.currentFilters() };
    filters.eventTypes = [];
    filters.minConfidence = 0;
    filters.maxEvents = 500;

    activeLayers.forEach(layerId => {
        const l = layers.find(l => l.id === layerId);
        if (l) applyLayerFilter(l);
    });

    window.GlobeCore.loadEvents(filters);
}

// Render event type filters in sidebar
function renderEventTypeFilters() {
    const container = document.getElementById('event-type-filters');
    if (!container) return;

    const eventTypes = Object.keys(window.GlobeCore.EVENT_TYPE_COLORS);

    container.innerHTML = eventTypes.map(type => {
        const color = window.GlobeCore.EVENT_TYPE_COLORS[type];
        const label = type.charAt(0).toUpperCase() + type.slice(1);
        const isActive = window.GlobeCore.currentFilters().eventTypes.includes(type);

        return `
            <label class="event-type-filter">
                <input type="checkbox" ${isActive ? 'checked' : ''}
                    onchange="toggleEventType('${type}', this.checked)">
                <div class="color-swatch" style="background: ${color}"></div>
                <span>${label}</span>
            </label>
        `;
    }).join('');
}

// Toggle event type filter
function toggleEventType(type, checked) {
    const filters = window.GlobeCore.currentFilters();

    if (checked) {
        if (!filters.eventTypes.includes(type)) {
            filters.eventTypes.push(type);
        }
    } else {
        const idx = filters.eventTypes.indexOf(type);
        if (idx > -1) filters.eventTypes.splice(idx, 1);
    }

    // Update UI
    document.getElementById('event-type-select-all').textContent =
        filters.eventTypes.length === Object.keys(window.GlobeCore.EVENT_TYPE_COLORS).length
            ? 'Deselect All' : 'Select All';

    // Reload events
    window.GlobeCore.loadEvents(filters);
}

// Select/deselect all event types
function toggleAllEventTypes() {
    const allTypes = Object.keys(window.GlobeCore.EVENT_TYPE_COLORS);
    const filters = window.GlobeCore.currentFilters();
    const btn = document.getElementById('event-type-select-all');

    if (filters.eventTypes.length === allTypes.length) {
        // Deselect all
        filters.eventTypes = [];
        btn.textContent = 'Select All';
        allTypes.forEach(type => {
            const cb = document.querySelector(`input[onchange*="toggleEventType('${type}'"]`);
            if (cb) cb.checked = false;
        });
    } else {
        // Select all
        filters.eventTypes = allTypes;
        btn.textContent = 'Deselect All';
        allTypes.forEach(type => {
            const cb = document.querySelector(`input[onchange*="toggleEventType('${type}'"]`);
            if (cb) cb.checked = true;
        });
    }

    window.GlobeCore.loadEvents(filters);
}

function clearAllEventTypes() {
    const filters = window.GlobeCore.currentFilters();
    filters.eventTypes = [];
    document.getElementById('event-type-select-all').textContent = 'Select All';

    Object.keys(window.GlobeCore.EVENT_TYPE_COLORS).forEach(type => {
        const cb = document.querySelector(`input[onchange*="toggleEventType('${type}'"]`);
        if (cb) cb.checked = false;
    });

    window.GlobeCore.loadEvents(filters);
}

// Update confidence filter
function updateConfidenceFilter(value) {
    const filters = window.GlobeCore.currentFilters();
    filters.minConfidence = parseFloat(value);
    document.getElementById('confidence-value').textContent = parseFloat(value).toFixed(1);
    window.GlobeCore.loadEvents(filters);
}

// Update max events filter
function updateMaxEventsFilter(value) {
    const filters = window.GlobeCore.currentFilters();
    filters.maxEvents = parseInt(value);
    window.GlobeCore.loadEvents(filters);
}

// Apply time range filter
function applyTimeFilter() {
    const start = document.getElementById('time-start').value;
    const end = document.getElementById('time-end').value;

    const filters = window.GlobeCore.currentFilters();
    filters.startTime = start ? new Date(start).toISOString() : null;
    filters.endTime = end ? new Date(end).toISOString() : null;

    window.GlobeCore.loadEvents(filters);
}

// Reset time filter
function resetTimeFilter() {
    document.getElementById('time-start').value = '';
    document.getElementById('time-end').value = '';

    const filters = window.GlobeCore.currentFilters();
    filters.startTime = null;
    filters.endTime = null;

    window.GlobeCore.loadEvents(filters);
}

// Quick time filter (last N hours)
function setQuickTime(hours) {
    const end = new Date();
    const start = new Date(end.getTime() - hours * 60 * 60 * 1000);

    document.getElementById('time-start').value = start.toISOString().slice(0, 16);
    document.getElementById('time-end').value = end.toISOString().slice(0, 16);

    applyTimeFilter();
}

// Export
window.GlobeLayers = {
    loadLayers,
    renderLayerList,
    toggleLayer,
    renderEventTypeFilters,
    toggleEventType,
    toggleAllEventTypes,
    clearAllEventTypes,
    updateConfidenceFilter,
    updateMaxEventsFilter,
    applyTimeFilter,
    resetTimeFilter,
    setQuickTime,
    layers: () => layers,
    activeLayers: () => activeLayers
};