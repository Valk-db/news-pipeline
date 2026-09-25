/**
 * Globe Timeline Module
 * Handles temporal playback, time slider, and timeline visualization
 */

let timelineData = [];
let timelinePlaying = false;
let timelineAnimationId = null;
let timelineSpeed = 1;
let currentTimeIndex = 0;

// Initialize timeline
function initTimeline() {
    const slider = document.getElementById('timeline-slider');
    const playBtn = document.getElementById('timeline-play');
    const pauseBtn = document.getElementById('timeline-pause');
    const speedSelect = document.getElementById('timeline-speed');

    if (slider) {
        slider.addEventListener('input', onTimelineSliderChange);
    }
    if (playBtn) {
        playBtn.addEventListener('click', playTimeline);
    }
    if (pauseBtn) {
        pauseBtn.addEventListener('click', pauseTimeline);
    }
    if (speedSelect) {
        speedSelect.addEventListener('change', (e) => {
            timelineSpeed = parseFloat(e.target.value);
        });
    }

    // Load initial timeline data
    loadTimelineData();
}

// Load timeline data from events
async function loadTimelineData() {
    const events = window.GlobeCore?.eventData?.() || [];

    // Group events by day
    const dayMap = new Map();

    events.forEach(feature => {
        const props = feature.properties || {};
        if (!props.start_time) return;

        const date = new Date(props.start_time);
        const dayKey = date.toISOString().split('T')[0];

        if (!dayMap.has(dayKey)) {
            dayMap.set(dayKey, {
                date: dayKey,
                count: 0,
                events: [],
                types: new Set()
            });
        }

        const day = dayMap.get(dayKey);
        day.count++;
        day.events.push(props);
        day.types.add(props.event_type);
    });

    // Convert to sorted array
    timelineData = Array.from(dayMap.values())
        .sort((a, b) => a.date.localeCompare(b.date))
        .map(day => ({
            ...day,
            types: Array.from(day.types)
        }));

    renderTimelineChart();
    updateTimelineLabels();
    updateTimelineSlider();
}

// Render timeline chart (canvas)
function renderTimelineChart() {
    const canvas = document.getElementById('timeline-canvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const container = canvas.parentElement;
    const width = container.clientWidth;
    const height = 150;

    canvas.width = width * window.devicePixelRatio;
    canvas.height = height * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

    // Clear
    ctx.clearRect(0, 0, width, height);

    if (timelineData.length === 0) return;

    // Calculate scales
    const maxCount = Math.max(...timelineData.map(d => d.count));
    const barWidth = Math.max(2, (width - 40) / timelineData.length);
    const xScale = (width - 40) / timelineData.length;
    const yScale = (height - 40) / Math.max(maxCount, 1);

    // Draw bars
    timelineData.forEach((day, i) => {
        const x = 20 + i * xScale;
        const barHeight = day.count * yScale;
        const y = height - 20 - barHeight;

        // Color based on dominant event type
        const dominantType = getDominantType(day);
        const color = window.GlobeCore?.EVENT_TYPE_COLORS?.[dominantType] || '#e74c3c';

        // Bar
        ctx.fillStyle = color + '80';
        ctx.fillRect(x, y, barWidth - 1, barHeight);

        // Highlight current time
        if (i === currentTimeIndex) {
            ctx.fillStyle = color;
            ctx.fillRect(x, y, barWidth - 1, barHeight);
        }
    });

    // Draw axes
    ctx.strokeStyle = 'var(--color-border)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(20, 20);
    ctx.lineTo(20, height - 20);
    ctx.lineTo(width - 20, height - 20);
    ctx.stroke();

    // Draw date labels
    ctx.fillStyle = 'var(--color-text-muted)';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';

    const labelInterval = Math.max(1, Math.floor(timelineData.length / 8));
    timelineData.forEach((day, i) => {
        if (i % labelInterval === 0 || i === timelineData.length - 1) {
            const x = 20 + i * xScale + barWidth / 2;
            const date = new Date(day.date);
            ctx.fillText(date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }), x, height - 5);
        }
    });
}

function getDominantType(day) {
    if (!day.events || day.events.length === 0) return 'other';

    const typeCounts = {};
    day.events.forEach(e => {
        typeCounts[e.event_type] = (typeCounts[e.event_type] || 0) + 1;
    });

    return Object.entries(typeCounts).sort((a, b) => b[1] - a[1])[0][0];
}

// Timeline playback controls
function playTimeline() {
    if (timelinePlaying) return;
    if (timelineData.length === 0) return;

    timelinePlaying = true;
    document.getElementById('timeline-play').style.display = 'none';
    document.getElementById('timeline-pause').style.display = 'inline-flex';

    animateTimeline();
}

function pauseTimeline() {
    timelinePlaying = false;
    document.getElementById('timeline-play').style.display = 'inline-flex';
    document.getElementById('timeline-pause').style.display = 'none';

    if (timelineAnimationId) {
        cancelAnimationFrame(timelineAnimationId);
        timelineAnimationId = null;
    }
}

function animateTimeline() {
    if (!timelinePlaying) return;

    const slider = document.getElementById('timeline-slider');
    const max = parseInt(slider.max);

    currentTimeIndex++;
    if (currentTimeIndex > max) {
        currentTimeIndex = 0;
    }

    slider.value = currentTimeIndex;
    onTimelineSliderChange();

    // Schedule next frame based on speed
    const delay = 1000 / timelineSpeed; // ms per step
    timelineAnimationId = setTimeout(() => {
        requestAnimationFrame(animateTimeline);
    }, delay);
}

function onTimelineSliderChange() {
    const slider = document.getElementById('timeline-slider');
    currentTimeIndex = parseInt(slider.value);

    // Update current time label
    updateTimelineLabels();

    // Filter events to current time
    filterEventsByTime(currentTimeIndex);

    // Re-render chart
    renderTimelineChart();
}

// Helper to check if viewer is valid
function isViewerValid() {
    const viewer = window.GlobeCore?.viewer?.();
    return viewer && !viewer.isDestroyed();
}

function filterEventsByTime(timeIndex) {
    if (timeIndex < 0 || timeIndex >= timelineData.length) return;

    const day = timelineData[timeIndex];
    const date = new Date(day.date);
    const nextDay = new Date(date.getTime() + 24 * 60 * 60 * 1000);

    const filters = window.GlobeCore.currentFilters();
    filters.startTime = date.toISOString();
    filters.endTime = nextDay.toISOString();

    // Load filtered events
    window.GlobeCore.loadEvents(filters);
}

function updateTimelineLabels() {
    const startEl = document.getElementById('timeline-start');
    const currentEl = document.getElementById('timeline-current');
    const endEl = document.getElementById('timeline-end');

    if (timelineData.length === 0) {
        if (startEl) startEl.textContent = '';
        if (currentEl) currentEl.textContent = 'No data';
        if (endEl) endEl.textContent = '';
        return;
    }

    if (startEl) {
        const startDate = new Date(timelineData[0].date);
        startEl.textContent = startDate.toLocaleDateString();
    }

    if (currentEl && currentTimeIndex < timelineData.length) {
        const currentDate = new Date(timelineData[currentTimeIndex].date);
        currentEl.textContent = currentDate.toLocaleDateString();
    }

    if (endEl) {
        const endDate = new Date(timelineData[timelineData.length - 1].date);
        endEl.textContent = endDate.toLocaleDateString();
    }
}

function updateTimelineSlider() {
    const slider = document.getElementById('timeline-slider');
    if (!slider || timelineData.length === 0) return;

    slider.min = '0';
    slider.max = String(timelineData.length - 1);
    slider.value = '0';
    currentTimeIndex = 0;

    updateTimelineLabels();
}

// Toggle timeline panel visibility
function toggleTimelinePanel() {
    const panel = document.getElementById('timeline-panel');
    panel.classList.toggle('open');
}

// Export
window.GlobeTimeline = {
    initTimeline,
    loadTimelineData,
    playTimeline,
    pauseTimeline,
    toggleTimelinePanel,
    filterEventsByTime,
    timelineData: () => timelineData,
    currentTimeIndex: () => currentTimeIndex
};