# AGENT_TASKS.md v16

Supersedes v15. v15's P0-C/P0-D/P0-E are merged into `main` (commit `c078281`) and confirmed
live. This revision adds two more bugs in the same `/globe` init path that P0-E's fix
uncovered: `TypeError: y.isDestroyed is not a function` thrown by `initGlobe()` itself
(P0-F), plus a picking bug in the same file that would have surfaced the moment P0-F stopped
throwing (P0-G).

Status: **both fixes (P0-F, P0-G) are implemented in this checkout** (see diff below) and
verified against a stubbed-Cesium Node harness (real Cesium can't run headless here, so this
is the same style of verification used for P0-E in v15 — see Verification). This doc is the
record of what was wrong and why, for the PR description / commit message.

---

## P0-F — `/globe` page crashes: `TypeError: y.isDestroyed is not a function`

### Symptom

Once P0-E's `hideEventDetail` fix let `window.GlobeCore` build successfully, the console
error changed from a `ReferenceError` to this (`y.isDestroyed` is the minified name inside
Cesium's own `PrimitiveCollection.add`):

```
globe-init.js:13 Initializing Globe...
globe-init.js:43 Globe initialization failed: TypeError: y.isDestroyed is not a function
    at d (Cesium.js:14612:35596)
    at ML.raiseEvent (Cesium.js:95:1311)
    at ca.add (Cesium.js:8798:109871)
    at Object.initGlobe (globe-core.js:103:22)
    at HTMLDocument.<anonymous> (globe-init.js:17:32)
```

### Diagnosis

`curation_ui/static/globe/globe-core.js` declares:

```js
const eventEntities = new Cesium.EntityCollection();
const clusterEntities = new Cesium.EntityCollection();
```

and `initGlobe()` then does:

```js
scene.primitives.add(eventEntities);
scene.primitives.add(clusterEntities);
```

`scene.primitives` is a `PrimitiveCollection` — it's the API for things that implement
Cesium's `Primitive` interface (billboards, polylines, point primitives, custom primitives:
objects with their own `update()`/`isDestroyed()`/`destroy()`). A `Cesium.EntityCollection`
is a completely different kind of object — it's the backing store for `Entity` instances, and
the correct way to get it rendered is to hand it (via a `DataSource`) to `viewer.dataSources`,
never to `scene.primitives`.

`PrimitiveCollection.add()` immediately calls `.isDestroyed()` on whatever it's given, to
raise its `primitiveAdded` event correctly. `EntityCollection` doesn't implement that method,
so the very first call — `scene.primitives.add(eventEntities)` at line 103 — throws
`TypeError: ... .isDestroyed is not a function` and aborts `initGlobe()` before it reaches
the `ScreenSpaceEventHandler` setup a few lines later, which is why the globe never
initializes at all.

This was unreachable before P0-E's fix: `window.GlobeCore` didn't exist yet, so
`globe-init.js` was throwing on `window.GlobeCore.initGlobe()` itself and never got far enough
to run `initGlobe()`'s body and hit this line. It's a second, independent bug in the same
function, not a regression from the P0-E fix.

Confirmed the mechanism (real Cesium can't run outside a browser in this container) with a
minimal Node harness stubbing just enough of the `Cesium`/`document` surface for `initGlobe()`
to execute: against the original file, `scene.primitives.add` gets called twice with
`EntityCollection` instances and `.entities.add()` (the delegation the fix below relies on)
throws immediately because there's no `.entities` on a raw `EntityCollection` — see
Verification.

### Fix

`curation_ui/static/globe/globe-core.js`:

```js
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
```

and in `initGlobe()`:

```js
// Register the data sources with the viewer (NOT scene.primitives --
// EntityCollection/CustomDataSource are not Primitives; scene.primitives.add()
// calls .isDestroyed() on whatever it's given the way it expects a Primitive
// to implement it, which throws "isDestroyed is not a function" here and
// aborted initGlobe() before the click handlers below were ever set up).
viewer.dataSources.add(eventEntitiesDataSource);
viewer.dataSources.add(clusterEntitiesDataSource);
```

**Why the wrapper split (`eventEntitiesDataSource` vs. `eventEntities`), instead of just
swapping in `CustomDataSource` directly:** `globe-interaction.js` reads
`window.GlobeCore.eventEntities.values` and `.find(...)` directly (in `flyToEvent()` and
`clusterEvents()`) — it expects `eventEntities` itself to be the `EntityCollection`, not a
`DataSource` wrapping one (a `CustomDataSource` has no top-level `.values`; you'd need
`.entities.values`). Keeping `eventEntities`/`clusterEntities` pointed at
`...DataSource.entities` means every other read/write of them — inside `globe-core.js`
(`removeAll()`, `add()`, `.values` in `fitEvents()`) and in `globe-interaction.js` — needed
zero changes. Only `initGlobe()`'s registration call and the two declaration lines change.

### Verification

Real Cesium needs a browser (WebGL/DOM), which isn't available in this container, so this was
verified the same way P0-E was in v15 — a minimal Node harness stubbing the handful of
`Cesium`/`document` APIs `initGlobe()` actually touches (`Viewer`, `CustomDataSource`,
`EntityCollection`, `ScreenSpaceEventHandler`, etc.), with `scene.primitives.add` and
`viewer.dataSources.add` both instrumented to record what they're called with:

- **Before the fix:** `scene.primitives.add` is called twice, both times with an
  `EntityCollection`-shaped stub; `viewer.dataSources.add` is never called. Exercising
  `eventEntities.entities.add(...)` (what the fixed code's contract requires) throws
  `TypeError: Cannot read properties of undefined (reading 'add')`, confirming a bare
  `EntityCollection` has no `.entities`.
- **After the fix:** `scene.primitives.add` is called zero times; `viewer.dataSources.add` is
  called exactly twice, both times with `CustomDataSource`-shaped stubs;
  `window.GlobeCore.eventEntities` is confirmed to still be the raw `EntityCollection`
  (`instanceof` check) with working `.add()` / `.removeAll()` / `.values`, matching what
  `globe-interaction.js` expects.

```
node --check curation_ui/static/globe/globe-core.js   # syntax OK
```

---

## P0-G — Globe clicks never open the event detail panel (latent, in the same functions)

### Symptom

No console error — this is a silent functional bug. Once P0-F stops `initGlobe()` from
throwing, clicking an event marker on the globe does nothing (no detail panel), and hovering
never shows the location label.

### Diagnosis

`onLeftClick()` and `onMouseMove()` in the same file do:

```js
const picked = viewer.scene.pick(movement.position);
if (Cesium.defined(picked) && picked.entity) {
    showEventDetail(picked.entity);
} else {
    hideEventDetail();
}
```

Cesium's picking API sets the picked object's owning `Entity` on `.id`, not `.entity` — this
is the standard, documented pattern (every Cesium Sandcastle picking example and the Cesium
community docs use `Cesium.defined(pickedObject) && pickedObject.id`). `.entity` is never set
by Cesium on a pick result, so `picked.entity` is always `undefined`: the `if` branch never
runs, every click falls into the `else` (`hideEventDetail()`), and `onMouseMove`'s equivalent
check for hover labels never fires either.

This bug has presumably been here since the picking code was written, but P0-F's crash meant
`initGlobe()` never got far enough to attach the `ScreenSpaceEventHandler` at all, so it was
never exercised or visibly broken until now.

### Fix

`onLeftClick()`:

```js
const picked = viewer.scene.pick(movement.position);
// scene.pick() sets .id to the owning Entity (Cesium's standard picking
// API), not .entity -- picked.entity is always undefined, so clicks never
// opened the detail panel even once initGlobe() stopped throwing.
if (Cesium.defined(picked) && picked.id) {
    showEventDetail(picked.id);
} else {
    hideEventDetail();
}
```

`onMouseMove()` — both branches:

```js
if (Cesium.defined(picked) && picked.id && picked.id !== hoveredEntity) {
    hoveredEntity = picked.id;
    ...
} else if (hoveredEntity && (!Cesium.defined(picked) || !picked.id)) {
    ...
}
```

### Verification

No live browser/WebGL available in this container to click-test end to end. This is a
one-line-per-branch identifier rename (`.entity` → `.id`) against Cesium's documented pick
result shape, checked by `node --check` for syntax and by manual trace through both functions
confirming every remaining reference to the picked object (`showEventDetail(picked.id)`,
`hoveredEntity = picked.id`, the label-show/hide toggles) is internally consistent post-rename.
**Flagging for the IDE agent to click-test in an actual browser against the deployed
`/globe` page once P0-F is live** — this is the one piece of this revision not verified
against real Cesium/WebGL.

---

## Full diff (P0-F + P0-G), `curation_ui/static/globe/globe-core.js`

```diff
--- a/curation_ui/static/globe/globe-core.js
+++ b/curation_ui/static/globe/globe-core.js
@@ -8,9 +8,17 @@
 let scene = null;
 let camera = null;

-// Entity collections
-const eventEntities = new Cesium.EntityCollection();
-const clusterEntities = new Cesium.EntityCollection();
+// Entity collections. Wrapped in a CustomDataSource so they can be
+// registered with viewer.dataSources (see initGlobe) -- but eventEntities/
+// clusterEntities themselves still point at the underlying EntityCollection
+// (CustomDataSource#entities), so every existing .removeAll()/.add()/.values
+// call in this file, and in globe-interaction.js's window.GlobeCore.eventEntities
+// reads, keeps working exactly as before -- only how the collection reaches the
+// screen (dataSources vs. primitives) changes.
+const eventEntitiesDataSource = new Cesium.CustomDataSource('events');
+const clusterEntitiesDataSource = new Cesium.CustomDataSource('clusters');
+const eventEntities = eventEntitiesDataSource.entities;
+const clusterEntities = clusterEntitiesDataSource.entities;

 // Event data store
 let eventData = [];
@@ -99,9 +107,13 @@
         }
     });

-    // Add event entities to scene
-    scene.primitives.add(eventEntities);
-    scene.primitives.add(clusterEntities);
+    // Register the data sources with the viewer (NOT scene.primitives --
+    // EntityCollection/CustomDataSource are not Primitives; scene.primitives.add()
+    // calls .isDestroyed() on whatever it's given the way it expects a Primitive
+    // to implement it, which throws "isDestroyed is not a function" here and
+    // aborted initGlobe() before the click handlers below were ever set up).
+    viewer.dataSources.add(eventEntitiesDataSource);
+    viewer.dataSources.add(clusterEntitiesDataSource);

     // Setup clock for timeline
     viewer.clock.shouldAnimate = false;
@@ -358,8 +370,11 @@
 function onLeftClick(movement) {
     if (!viewer || viewer.isDestroyed()) return;
     const picked = viewer.scene.pick(movement.position);
-    if (Cesium.defined(picked) && picked.entity) {
-        showEventDetail(picked.entity);
+    // scene.pick() sets .id to the owning Entity (Cesium's standard picking
+    // API), not .entity -- picked.entity is always undefined, so clicks never
+    // opened the detail panel even once initGlobe() stopped throwing.
+    if (Cesium.defined(picked) && picked.id) {
+        showEventDetail(picked.id);
     } else {
         hideEventDetail();
     }
@@ -368,13 +383,13 @@
 function onMouseMove(movement) {
     if (!viewer || viewer.isDestroyed()) return;
     const picked = viewer.scene.pick(movement.endPosition);
-    if (Cesium.defined(picked) && picked.entity && picked.entity !== hoveredEntity) {
-        hoveredEntity = picked.entity;
+    if (Cesium.defined(picked) && picked.id && picked.id !== hoveredEntity) {
+        hoveredEntity = picked.id;
         // Show label on hover
         if (hoveredEntity.label) {
             hoveredEntity.label.show = true;
         }
-    } else if (hoveredEntity && (!Cesium.defined(picked) || !picked.entity)) {
+    } else if (hoveredEntity && (!Cesium.defined(picked) || !picked.id)) {
         if (hoveredEntity.label) {
             hoveredEntity.label.show = false;
         }
```

---

## Follow-up (not blocking, worth doing next)

1. **Click-test P0-G for real** once deployed: open `/globe`, click a marker, confirm the
   detail panel opens with the right event's data; hover a marker, confirm its label appears.
   This is the one thing in this revision not verified against live Cesium.
2. Actually implement clustering. `clusterEntitiesDataSource`/`clusterEntities` is created and
   now correctly registered with `viewer.dataSources`, but nothing ever populates it —
   `enableClustering()` (line ~502) is still a bare `TODO` stub that only flips a boolean.
   Low urgency (it's a no-op today, not broken), but worth a follow-up ticket since the
   plumbing is now actually wired up correctly for the first time.
3. From v15's follow-up list, still open: a JS test harness for this repo. This revision's
   Node-stub-Cesium approach (see Verification above) could be generalized into one — it would
   have caught both P0-F and P0-E before they shipped, and P0-G once real click simulation is
   in scope.
4. From v15's follow-up list, still open: the regression test for P0-D
   (`test_index_with_media_and_snippets`), and the `.in_()` audit outside `curation_ui/main.py`.