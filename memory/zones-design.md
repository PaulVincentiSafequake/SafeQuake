# B4 — Rescue-team zones: design (2026-08-17, DESIGN ONLY — build with #116 restructure)

## Decision summary
Assignment is **derived from location, never hand-set**. Zones are polygons; every
trapped pin inside a polygon belongs to that zone; recomputed on every poll, so
status/location changes reassign automatically.

## Data model (`zones` collection)
```json
{
  "zone_id": "uuid", "name": "Team North", "contact": "callsign or phone",
  "polygon": [[lat, lon], ...],       // freeform polygon, ordered vertices
  "color": "#RRGGBB",                 // auto-assigned from palette
  "drawn_seq": 17,                    // monotonic; higher = drawn later
  "created_by": "operator@email", "created_at": "...", "updated_at": "..."
}
```

## API
- `POST /api/admin/zones`, `PATCH /api/admin/zones/{id}` (rename, re-shape,
  re-contact), `DELETE /api/admin/zones/{id}` — admin + operator.
- Assignment computed **server-side** in `GET /api/devices` (single source of
  truth so exports and any future field view agree): ray-cast point-in-polygon.
  Scale check: 30k devices × ~20 zones ≈ 600k tests/poll — fine in pure Python
  (<100 ms); use shapely STRtree only if load tests say otherwise.

## Edge cases (explicit, per Paul)
1. **Unassigned is a first-class zone.** Anyone outside every polygon appears
   under a permanent "⚠ Unassigned" group with the same severity breakdown,
   visually flagged — never silently dropped.
2. **Overlaps: most recently drawn wins** (`drawn_seq` desc). When a pin sits in
   2+ zones the response includes `also_inside: [...]`, and the zone panel shows
   "overlaps Team X — overlapping people are counted HERE" so the rule is
   visible whenever it applies. Never double-counted in totals.
3. **Reassignment is automatic** — derived data, recomputed every poll.

## Dashboard UI (built on the #116 tabbed layout, Live tab + pop-out map)
- Drawing: Leaflet-Geoman (free, works with existing Leaflet) — freeform
  polygon draw + vertex editing + drag. Rectangle/circle fall-back not needed;
  polygons ship first.
- Zone panel per zone: name, contact (tap-to-call link), total trapped,
  severity breakdown (immediate / serious / minor), edit + delete.
- Map: polygon fill at low opacity in zone colour; pins inherit a small zone
  colour tag; Unassigned pins get a distinct hatched badge.

## Open question for Paul (blocks auth scope, not the map/database design)
Does "Team North" need its own **field login** to see just its zone, or is this
coordinator-facing only (operator radios the team)? Field logins mean a new
role + scoped views (#94 extension); coordinator-only means zero auth work.
The data model above supports both — the answer only changes UI/auth scope.
