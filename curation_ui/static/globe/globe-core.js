/**
 * Globe Core Module
 * Initializes Cesium viewer and handles core globe functionality
 */

// Global Cesium viewer instance
let viewer = null;
let scene = null;
let camera = null;

// Entity collections. Wrapped in a CustomDataSource so they can be
// registered with viewer.dataSources (see initGlobe) -- but eventEntities/
// clusterEntities themselves still point at the underlying EntityCollection
// (CustomDataSource#entities), so every existing .removeAll()/.add()/.values
// call in this file, and in globe-interaction.js's window.GlobeCore.eventEntities
// reads, keeps working exactly as before -- only how the collection reaches the
// screen (dataSources vs. primitives) changes.
const eventEntitiesDataSource = new Cesium.CustomDataSource('events');
const clusterEntitiesDataSource = new Cesium.CustomDataSource('clusters');
const eventEntities = eventEntitiesDataSource.entities;
const clusterEntities = clusterEntitiesDataSource.entities;

// Event data store
let eventData = [];
let currentFilters = {
    bbox: null,
    startTime: null,
    endTime: null,
    eventTypes: [],
    minConfidence: 0,
    maxEvents: 500
};

// Color mapping for event types
const EVENT_TYPE_COLORS = {
    'conflict': '#e74c3c',
    'protest': '#f39c12',
    'election': '#3498db',
    'disaster': '#e67e22',
    'accident': '#95a5a6',
    'political': '#9b59b6',
    'economic': '#27ae60',
    'health': '#e91e63',
    'environmental': '#2ecc71',
    'crime': '#c0392b',
    'sports': '#f1c40f',
    'cultural': '#8e44ad',
    'scientific': '#1abc9c',
    'other': '#7f8c8d'
};

// Initialize Cesium viewer
async function initGlobe() {
    // Configure Cesium
    Cesium.Ion.defaultAccessToken = ''; // Using OpenStreetMap, no token needed

    // Use OpenStreetMap imagery to avoid Cesium Ion token requirement
    const osmImagery = new Cesium.UrlTemplateImageryProvider({
        url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        credit: '© OpenStreetMap contributors',
        subdomains: 'abc',
        maximumLevel: 19
    });

    viewer = new Cesium.Viewer('cesium-container', {
        animation: false,
        baseLayerPicker: false,
        fullscreenButton: false,
        geocoder: false,
        homeButton: false,
        infoBox: true,
        sceneModePicker: false,
        selectionIndicator: false,
        timeline: false,
        navigationHelpButton: false,
        navigationInstructionsInitiallyVisible: false,
        scene3DOnly: true,
        shadows: true,
        terrainProvider: new Cesium.EllipsoidTerrainProvider(),
        imageryProvider: osmImagery,
        skyBox: new Cesium.SkyBox({
            sources: {
                positiveX: 'https://cesium.com/downloads/cesiumjs/releases/1.115/Build/Cesium/Assets/Textures/SkyBox/tycho2t3_80_px.jpg',
                negativeX: 'https://cesium.com/downloads/cesiumjs/releases/1.115/Build/Cesium/Assets/Textures/SkyBox/tycho2t3_80_mx.jpg',
                positiveY: 'https://cesium.com/downloads/cesiumjs/releases/1.115/Build/Cesium/Assets/Textures/SkyBox/tycho2t3_80_py.jpg',
                negativeY: 'https://cesium.com/downloads/cesiumjs/releases/1.115/Build/Cesium/Assets/Textures/SkyBox/tycho2t3_80_my.jpg',
                positiveZ: 'https://cesium.com/downloads/cesiumjs/releases/1.115/Build/Cesium/Assets/Textures/SkyBox/tycho2t3_80_pz.jpg',
                negativeZ: 'https://cesium.com/downloads/cesiumjs/releases/1.115/Build/Cesium/Assets/Textures/SkyBox/tycho2t3_80_mz.jpg'
            }
        }),
        skyAtmosphere: new Cesium.SkyAtmosphere()
    });

    scene = viewer.scene;
    camera = viewer.camera;

    // Enable depth testing for better rendering
    scene.globe.depthTestAgainstTerrain = true;

    // Set initial camera position (world view)
    camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(0, 20, 30000000),
        orientation: {
            heading: 0,
            pitch: -Cesium.Math.PI_OVER_TWO,
            roll: 0
        }
    });

    // Register the data sources with the viewer (NOT scene.primitives --
    // EntityCollection/CustomDataSource are not Primitives; scene.primitives.add()
    // calls .isDestroyed() on whatever it's given the way it expects a Primitive
    // to implement it, which throws "isDestroyed is not a function" here and
    // aborted initGlobe() before the click handlers below were ever set up).
    viewer.dataSources.add(eventEntitiesDataSource);
    viewer.dataSources.add(clusterEntitiesDataSource);

    // Setup clock for timeline
    viewer.clock.shouldAnimate = false;
    viewer.clock.multiplier = 1;

    // Event handler for entity selection
    const handler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
    handler.setInputAction(onLeftClick, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    handler.setInputAction(onMouseMove, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

    console.log('Globe initialized');
    return viewer;
}

// Convert GeoJSON to Cesium entities
function geojsonToEntities(geojson, options = {}) {
    const entities = [];
    const color = options.color || '#e74c3c';
    const eventId = options.eventId;
    const properties = options.properties || {};

    if (!geojson || !geojson.type) return entities;

    const material = Cesium.Color.fromCssColorString(color).withAlpha(0.7);
    const outlineMaterial = Cesium.Color.fromCssColorString(color).withAlpha(1.0);

    switch (geojson.type) {
        case 'Point':
            entities.push(createPointEntity(geojson.coordinates, color, properties));
            break;

        case 'MultiPoint':
            geojson.coordinates.forEach(coord => {
                entities.push(createPointEntity(coord, color, properties));
            });
            break;

        case 'LineString':
            entities.push(createLineEntity(geojson.coordinates, color, properties));
            break;

        case 'MultiLineString':
            geojson.coordinates.forEach(line => {
                entities.push(createLineEntity(line, color, properties));
            });
            break;

        case 'Polygon':
            entities.push(createPolygonEntity(geojson.coordinates, color, properties));
            break;

        case 'MultiPolygon':
            geojson.coordinates.forEach(polygon => {
                entities.push(createPolygonEntity(polygon, color, properties));
            });
            break;

        case 'Feature':
            if (geojson.geometry) {
                entities.push(...geojsonToEntities(geojson.geometry, {
                    ...options,
                    properties: { ...properties, ...geojson.properties }
                }));
            }
            break;

        case 'FeatureCollection':
            geojson.features.forEach(feature => {
                entities.push(...geojsonToEntities(feature, options));
            });
            break;
    }

    return entities;
}

function createPointEntity(coordinates, color, properties) {
    const [lon, lat, height = 0] = coordinates;
    const entity = new Cesium.Entity({
        position: Cesium.Cartesian3.fromDegrees(lon, lat, height),
        point: {
            pixelSize: calculatePointSize(properties),
            color: Cesium.Color.fromCssColorString(color),
            outlineColor: Cesium.Color.WHITE,
            outlineWidth: 2,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            disableDepthTestDistance: Number.POSITIVE_INFINITY
        },
        label: {
            text: properties.location_name || '',
            font: '12px sans-serif',
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            outlineWidth: 2,
            outlineColor: Cesium.Color.BLACK,
            pixelOffset: new Cesium.Cartesian2(0, -20),
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
            show: false // Show on hover
        },
        properties: properties
    });
    return entity;
}

function createLineEntity(coordinates, color, properties) {
    const positions = coordinates.map(([lon, lat, height = 0]) =>
        Cesium.Cartesian3.fromDegrees(lon, lat, height)
    );

    return new Cesium.Entity({
        polyline: {
            positions: positions,
            width: 3,
            material: Cesium.Color.fromCssColorString(color),
            clampToGround: true,
            classificationType: Cesium.ClassificationType.BOTH
        },
        properties: properties
    });
}

function createPolygonEntity(coordinates, color, properties) {
    // coordinates is [outerRing, innerRing1, innerRing2, ...]
    const outerRing = coordinates[0];
    const holes = coordinates.slice(1);

    const hierarchy = new Cesium.PolygonHierarchy(
        outerRing.map(([lon, lat, height = 0]) => Cesium.Cartesian3.fromDegrees(lon, lat, height))
    );

    holes.forEach(hole => {
        hierarchy.holes.push(new Cesium.PolygonHierarchy(
            hole.map(([lon, lat, height = 0]) => Cesium.Cartesian3.fromDegrees(lon, lat, height))
        ));
    });

    return new Cesium.Entity({
        polygon: {
            hierarchy: hierarchy,
            material: Cesium.Color.fromCssColorString(color).withAlpha(0.4),
            outline: true,
            outlineColor: Cesium.Color.fromCssColorString(color),
            outlineWidth: 2,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            classificationType: Cesium.ClassificationType.BOTH
        },
        properties: properties
    });
}

function calculatePointSize(properties) {
    // Base size on confidence and source count
    const confidence = properties.confidence || 0.5;
    const sourceCount = properties.source_count || 1;
    const tier1Count = properties.tier1_source_count || 0;

    let size = 8 + (confidence * 8) + Math.min(sourceCount, 10) * 0.5 + tier1Count * 0.5;
    return Math.min(Math.max(size, 6), 24);
}

// Load events from API
async function loadEvents(filters = {}) {
    const params = new URLSearchParams();

    if (filters.bbox) params.append('bbox', filters.bbox);
    if (filters.startTime) params.append('start_time', filters.startTime);
    if (filters.endTime) params.append('end_time', filters.endTime);
    if (filters.eventTypes && filters.eventTypes.length > 0) {
        params.append('event_type', filters.eventTypes.join(','));
    }
    if (filters.minConfidence > 0) params.append('min_confidence', filters.minConfidence);
    if (filters.maxEvents) params.append('limit', filters.maxEvents);

    try {
        const response = await fetch(`/api/globe/events?${params.toString()}`, {
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();
        eventData = data.features || [];

        updateEventCount();
        renderEvents(eventData);

        return eventData;
    } catch (error) {
        console.error('Failed to load events:', error);
        showError('Failed to load events: ' + error.message);
        return [];
    }
}

// Render events on globe
function renderEvents(events) {
    // Clear existing entities
    eventEntities.removeAll();

    if (!events || events.length === 0) return;

    events.forEach(feature => {
        const props = feature.properties || {};
        const color = EVENT_TYPE_COLORS[props.event_type] || EVENT_TYPE_COLORS.other;

        const entities = geojsonToEntities(feature.geometry, {
            color: color,
            eventId: props.event_id,
            properties: props
        });

        entities.forEach(entity => {
            eventEntities.add(entity);
        });
    });

    // Update clustering if enabled
    if (window.globeClusteringEnabled) {
        updateClustering();
    }
}

// Camera control functions
function resetView() {
    camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(0, 20, 30000000),
        duration: 1.5
    });
}

function fitEvents() {
    if (!viewer || viewer.isDestroyed()) return;
    if (eventEntities.values.length === 0) return;

    const boundingSphere = Cesium.BoundingSphere.fromPoints(
        eventEntities.values
            .filter(e => e.position)
            .map(e => e.position.getValue(viewer.clock.currentTime))
    );

    if (boundingSphere) {
        camera.flyToBoundingSphere(boundingSphere, {
            duration: 1.5,
            offset: new Cesium.HeadingPitchRange(0, -Cesium.Math.PI_OVER_TWO, 0)
        });
    }
}

// Event handlers
let hoveredEntity = null;

function onLeftClick(movement) {
    if (!viewer || viewer.isDestroyed()) return;
    const picked = viewer.scene.pick(movement.position);
    // scene.pick() sets .id to the owning Entity (Cesium's standard picking
    // API), not .entity -- picked.entity is always undefined, so clicks never
    // opened the detail panel even once initGlobe() stopped throwing.
    if (Cesium.defined(picked) && picked.id) {
        showEventDetail(picked.id);
    } else {
        hideEventDetail();
    }
}

function onMouseMove(movement) {
    if (!viewer || viewer.isDestroyed()) return;
    const picked = viewer.scene.pick(movement.endPosition);
    if (Cesium.defined(picked) && picked.id && picked.id !== hoveredEntity) {
        hoveredEntity = picked.id;
        // Show label on hover
        if (hoveredEntity.label) {
            hoveredEntity.label.show = true;
        }
    } else if (hoveredEntity && (!Cesium.defined(picked) || !picked.id)) {
        if (hoveredEntity.label) {
            hoveredEntity.label.show = false;
        }
        hoveredEntity = null;
    }
}

function showEventDetail(entity) {
    const props = entity.properties || {};
    const panel = document.getElementById('event-detail-panel');
    const content = document.getElementById('panel-content');
    const title = document.getElementById('panel-title');

    title.textContent = props.location_name || 'Event Details';

    const eventTypeColor = EVENT_TYPE_COLORS[props.event_type] || EVENT_TYPE_COLORS.other;
    const eventTypeLabel = props.event_type ? props.event_type.charAt(0).toUpperCase() + props.event_type.slice(1) : 'Unknown';

    content.innerHTML = `
        <div class="event-detail">
            <div class="event-header">
                <span class="event-type-badge" style="background: ${eventTypeColor}20; color: ${eventTypeColor}; border: 1px solid ${eventTypeColor};">
                    ${eventTypeLabel}
                </span>
            </div>

            <div class="meta-row">
                <span class="meta-label">Location:</span>
                <span class="meta-value">${props.location_name || 'Unknown'}</span>
            </div>

            <div class="meta-row">
                <span class="meta-label">Type:</span>
                <span class="meta-value">${props.location_type || 'Unknown'}</span>
            </div>

            <div class="meta-row">
                <span class="meta-label">Confidence:</span>
                <span class="meta-value">${(props.confidence * 100).toFixed(0)}%</span>
            </div>

            <div class="meta-row">
                <span class="meta-label">Sources:</span>
                <span class="meta-value">${props.source_count || 0} (Tier-1: ${props.tier1_source_count || 0})</span>
            </div>

            <div class="meta-row">
                <span class="meta-label">Radius:</span>
                <span class="meta-value">${props.radius_km ? props.radius_km + ' km' : 'Point'}</span>
            </div>

            <div class="meta-row">
                <span class="meta-label">Start Time:</span>
                <span class="meta-value">${props.start_time ? new Date(props.start_time).toLocaleString() : 'Unknown'}</span>
            </div>

            ${props.end_time ? `
            <div class="meta-row">
                <span class="meta-label">End Time:</span>
                <span class="meta-value">${new Date(props.end_time).toLocaleString()}</span>
            </div>
            ` : ''}

            ${props.entities && Object.keys(props.entities).length > 0 ? `
            <div>
                <span class="meta-label">Entities:</span>
                <div class="entities">
                    ${Object.entries(props.entities).flatMap(([type, names]) =>
                        names.map(name => `<span class="entity-tag">${type}: ${name}</span>`).join('')
                    ).join('')}
                </div>
            </div>
            ` : ''}

            <div class="actions">
                <button class="btn btn-primary btn-sm" onclick="openStory('${props.story_id}')">
                    View Story
                </button>
                <button class="btn btn-secondary btn-sm" onclick="flyToEvent('${props.event_id}')">
                    Fly To
                </button>
            </div>
        </div>
    `;

    panel.classList.add('open');
}


// Hide the event detail panel (mirrors showEventDetail above; this module
// owns #event-detail-panel, so it must not depend on globe-interaction.js's
// same-named function, which runs in a separate scope and loads after this
// script — referencing it here threw a ReferenceError while building the
// window.GlobeCore export object, which in turn left window.GlobeCore
// undefined and broke globe-init.js's window.GlobeCore.initGlobe() call).
function hideEventDetail() {
    const panel = document.getElementById('event-detail-panel');
    if (panel) panel.classList.remove('open');
}


// Clustering support
let clusteringEnabled = false;

function updateClustering() {
    // Simple client-side clustering based on zoom level
    const zoomLevel = getZoomLevel();

    if (zoomLevel < 3) {
        // High level - cluster everything
        enableClustering();
    } else {
        // Show individual events
        disableClustering();
    }
}

function getZoomLevel() {
    const cartographic = camera.positionCartographic;
    if (!cartographic) return 0;
    const height = cartographic.height;
    return Math.log2(30000000 / Math.max(height, 1000));
}

function enableClustering() {
    // TODO: Implement clustering using Cesium's EntityCluster or custom logic
    clusteringEnabled = true;
}

function disableClustering() {
    clusteringEnabled = false;
}

// Utility functions
function updateEventCount() {
    const countEl = document.getElementById('event-count');
    if (countEl) {
        countEl.textContent = `${eventData.length} events loaded`;
    }
}

function showError(message) {
    console.error(message);
    // Could add toast notification here
}

// Export for other modules
window.GlobeCore = {
    initGlobe,
    loadEvents,
    renderEvents,
    resetView,
    fitEvents,
    showEventDetail,
    hideEventDetail,
    updateClustering,
    getZoomLevel,
    eventEntities,
    eventData: () => eventData,
    currentFilters: () => currentFilters,
    EVENT_TYPE_COLORS,
    viewer: () => viewer,
    scene: () => scene,
    camera: () => camera
};