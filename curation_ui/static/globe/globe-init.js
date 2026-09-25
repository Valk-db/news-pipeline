/**
 * Globe Initialization
 * Main entry point that initializes all globe modules
 */

// Wait for DOM and Cesium to be ready
document.addEventListener('DOMContentLoaded', async () => {
    // Check if we're on the globe page
    if (!document.getElementById('cesium-container')) {
        return;
    }

    console.log('Initializing Globe...');

    try {
        // Initialize core globe (Cesium viewer)
        await window.GlobeCore.initGlobe();

        // Initialize layers
        await window.GlobeLayers.loadLayers();

        // Initialize timeline
        window.GlobeTimeline.initTimeline();

        // Initialize interactions
        window.GlobeInteractions?.initInteractions?.();

        // Load initial events
        await window.GlobeCore.loadEvents();

        // Load timeline data
        await window.GlobeTimeline.loadTimelineData();

        // Initial render of event type filters
        window.GlobeLayers.renderEventTypeFilters();

        console.log('Globe initialization complete');

        // Dispatch ready event
        document.dispatchEvent(new CustomEvent('globe:ready'));

    } catch (error) {
        console.error('Globe initialization failed:', error);
        showInitError(error.message);
    }
});

// Show initialization error
function showInitError(message) {
    const container = document.getElementById('cesium-container');
    if (container) {
        container.innerHTML = `
            <div style="
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                height: 100%;
                color: var(--color-text);
                padding: var(--space-8);
                text-align: center;
            ">
                <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="margin-bottom: var(--space-4); opacity: 0.5;">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="8" x2="12" y2="12"></line>
                    <line x1="12" y1="16" x2="12.01" y2="16"></line>
                </svg>
                <h2>Failed to Initialize Globe</h2>
                <p style="color: var(--color-text-muted); margin-top: var(--space-2);">${message}</p>
                <button class="btn btn-primary" onclick="location.reload()" style="margin-top: var(--space-4);">
                    Retry
                </button>
            </div>
        `;
    }
}

// Handle visibility change for performance
document.addEventListener('visibilitychange', () => {
    const viewer = window.GlobeCore?.viewer?.();
    if (viewer && !viewer.isDestroyed()) {
        viewer.clock.shouldAnimate = !document.hidden;
    }
});

// Handle window resize
window.addEventListener('resize', () => {
    const viewer = window.GlobeCore?.viewer?.();
    if (viewer && !viewer.isDestroyed()) {
        viewer.resize();
    }

    // Re-render timeline chart
    if (window.GlobeTimeline) {
        window.GlobeTimeline.loadTimelineData?.();
    }
});

// Export initialization status
window.GlobeApp = {
    initialized: false,
    initPromise: null
};

// Mark as initialized when ready
document.addEventListener('globe:ready', () => {
    window.GlobeApp.initialized = true;
    console.log('Globe app ready');
});