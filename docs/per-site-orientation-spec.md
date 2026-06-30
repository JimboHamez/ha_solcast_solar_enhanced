# Spec: Per-site array config (page-per-site) — orientation, capacity, measurement

Status: **draft / pre-implementation** — design only, no code yet. Decisions in §11 to be
confirmed before an implementation plan. Item 2 (per-site shading dampening) is tracked
separately and deferred.

This supersedes the single "Per-site sensor mapping" page (and its inline topology selector)
shipped in 1.9.0–1.9.2: a multi-array property now gets **one wizard page per array**, and
property-wide settings consolidate onto page 1.

## 1. Problem

A multi-array property can have arrays at **different tilt, azimuth, and capacity**, and is
measured by different hardware (one shared inverter split by DC, vs microinverters / one
inverter per array). Today:

- Per-array **tilt/azimuth/capacity can't be entered** — Step 1 has a single value each; the
  per-site page collects only sensors; orientation comes solely from Solcast auto-discovery.
- The single per-site page lists **every site's fields at once** — with orientation added,
  that's ~8–9 fields × N arrays on one page; crowded and hard to explain.
- The headline **"Tuned Panel Azimuth"** sensor reports one value — misleading for a
  multi-azimuth property.
- Property-wide settings are scattered (export limit lives on the Tuning step).

## 2. Current behaviour (verified)

- **Discovery** (`coordinator.discover_sites`, :100) reads per-site `tilt`, `azimuth`,
  `compass_degrees`, `capacity`, `capacity_dc` from each base RooftopSensor into `self._sites`.
- **Per-site tuning** (`_run_site_tuning`, :649) tunes **tilt only**, azimuth held fixed per
  site (`_site_azimuth_seed`, :710); capacity from `site.get("capacity") or CONF_CAPACITY_KW`
  (:671). Azimuth is deliberately not tuned (non-identifiable).
- **Aggregate `_total`** tuning (:629) seeds from `CONF_TILT`/`CONF_AZIMUTH`/`CONF_CAPACITY_KW`.
- **Outputs:** `tuning_extra['per_site']` (:1674) carries `{name, resource_id, tilt, azimuth,
  rmse_kw, n_records}`, surfaced only as attributes of "Tuned Panel Tilt".
- **Convention:** azimuth uses the Solcast/base convention (0 = N, **+ = W**); internal tuner
  frame is East-positive (`panel_azimuth_to_internal` / `panel_azimuth_to_solcast`).
- **Current flow:** Step 1 (Site & System), Database, Weather, Battery, Tuning (carries the
  export limit), then a single Per-site page (topology selector + all sites' sensor fields).

## 3. Goals / non-goals

**Goals**
- One **page per array** for multi-array properties; each per-array attribute appears in
  exactly one place (no duplication).
- Per-array **tilt, azimuth, capacity** and topology-appropriate **measurement** fields,
  pre-filled from Solcast discovery.
- Consolidate property-wide settings on **page 1**: total PV kW + **export limit**.
- Make per-site tuned tilt/azimuth clearly visible.

**Non-goals**
- Not tuning azimuth (stays fixed/echoed).
- Item 2 (per-site shading dampening) — separate spec.

## 4. Wizard structure

### Single-array (`_is_single_site()` true) — unchanged
Page 1 carries everything per-array (lat/lon, total kW, tilt, azimuth, MPPT V/I, sensors) plus
export limit; then Database, Weather, Battery, Tuning. No topology step, no per-site pages.

### Multi-array — new shape
1. **Site & System (property-wide only):** latitude, longitude, **total PV kW**, **export
   limit**, **PV generation sensor**, **PV export sensor**, battery stat sensor, input modes.
   *Hides* tilt, azimuth, capacity and the flat MPPT fields (they're per-array now).
2. **Database**, 3. **Weather**, 4. **Battery**, 5. **Tuning** (export limit **removed** —
   now on page 1).
6. **Measurement topology** (property-wide): *each array has its own generation sensor*
   (direct) vs *one shared inverter, split by DC* (dc_split). (Persisted as
   `CONF_SITE_TOPOLOGY`; default inferred for existing entries.)
7..N. **One page per discovered array** — header = site name ("Array 2 of 3"):
   - **panel tilt**, **panel azimuth**, **capacity (kW DC)** — pre-filled from discovery.
   - **measurement**, by topology:
     - **direct / Enphase:** the array's **own AC generation sensor** (its microinverter sum)
       + sensor type. No DC fields.
     - **dc_split:** the array's **DC/MPPT sensor** + **MPPT 1/2 V/I** telemetry. (The shared
       AC is page 1's PV generation sensor — not re-entered per array.)

| Field | Single-array | Multi-array |
|---|---|---|
| lat / lon, total PV kW, export limit, PV gen + export sensors, battery, modes | Page 1 | **Page 1** |
| tilt, azimuth, capacity | Page 1 | **per-array page** |
| MPPT V/I telemetry | Page 1 | **per-array page** (dc_split) |
| per-array AC sensor | n/a | **per-array page** (direct) |
| per-array DC sensor | n/a | **per-array page** (dc_split) |
| topology | n/a | **step 6** |

## 5. Measurement model with page-per-site

Page 1's **PV generation sensor** does double duty by topology, so no AC entity is entered
twice:
- **dc_split:** it **is** the shared inverter AC — apportioned across arrays by each array's
  DC share. Per-array pages supply only DC. *(This eliminates the old "same AC on every row"
  pattern and the `dc_split_ac_mismatch` failure mode entirely — there's one AC, on page 1.)*
- **direct:** it's the property **total** generation, optional — if left blank, `_total` is
  derived as the **sum of the per-array AC sensors** (item 2's P0b). Per-array pages supply
  each array's own AC sensor.

`_derive_groups` builds `CONF_SITE_GROUPS` from this: dc_split → one group whose `ac_sensor`
is the page-1 sensor with a `strings` list (`{site, dc_sensor, tilt?, azimuth?, capacity?,
mppts?}`); direct → one single-site group per array (`{ac_sensor = the array's own sensor,
site, tilt?, azimuth?, capacity?, mppts?}`).

## 6. Capacity (decision: Option 1)

- **Page-1 total PV kW = property nameplate, authoritative** — drives the `_total` fit and is
  the fallback.
- **Per-array capacity defaults from Solcast discovery** and appears on the array page only as
  an override; users rarely touch it.
- `_total` capacity = the page-1 total (not a sum), so the headline stays stable; per-array
  tuning uses the per-array value (override → discovery → total).
- *Sub-decision (§11.5):* reconcile the AC vs DC capacity meaning — Step-1 label says "kW DC",
  the per-site tuner consumes `capacity` (AC), discovery has both. Pick one definition and
  document it.

## 7. Orientation/capacity precedence resolver

One resolver (used by tuning, the dampening gate, and item 2's forecast fallback):

```
per-array tilt / azimuth / capacity =
  1. CONF_SITE_GROUPS override for that array     (explicit user value)
  2. Solcast discovery for that array             (compass_degrees / azimuth / tilt / capacity)
  3. page-1 CONF_TILT / CONF_AZIMUTH / CONF_CAPACITY_KW   (last-resort fallback)
```

`_site_azimuth_seed` / `_site_orientation_seed` consult the override first; a capacity resolver
is added alongside. Discovery→page-1 fallback preserved.

## 8. Config-flow implementation (`config_flow.py`)

- **`_build_site_schema(d, *, single_site)`:** when `single_site` is false, omit `CONF_TILT`,
  `CONF_AZIMUTH`, `CONF_CAPACITY_KW` and the flat MPPT keys (extends the existing MPPT gating);
  add `CONF_EXPORT_LIMIT_KW` to page 1 (single- and multi-array). Remove it from the Tuning step.
- **New `async_step_topology`** (multi-array only): the `direct`/`dc_split` selector, persisted.
- **New per-array loop** `async_step_array` (both flows): iterate `self._discovered` by index
  held in instance state; build a per-array schema from the topology (orientation + capacity +
  topology-dependent measurement), prefilled from the stored group (`_groups_to_assignments`)
  then discovery; on submit, stash and advance; after the last array, derive `CONF_SITE_GROUPS`
  and finish.
- **`_derive_groups` / `_groups_to_assignments`:** carry `tilt`/`azimuth`/`capacity` + the
  topology-appropriate sensors per array; dc_split `ac_sensor` = page-1 PV generation sensor.
- **Options flow** mirrors the same loop.

Dynamic per-step pages replace the single `_build_sites_schema` form. The field keys are now
per-page (one array each), so the name-embedded-key trick is no longer needed for
disambiguation — labels can be plain translated `data` keys.

## 9. Coordinator changes (`coordinator.py`)

- Resolver (§7) feeds `_run_site_tuning` (per-array tilt/az/capacity) and the gate.
- `_total` multi-array seed: **capacity = page-1 total**; **tilt/azimuth = capacity-weighted
  mean of per-array** (shortest-circle mean for azimuth), since page 1 no longer carries them.
- `_read_site_actuals` unchanged in shape (dc_split `ac_sensor` now resolves to the page-1
  sensor; direct uses each array's own).
- Export-limit read unchanged (still prefers the base `site_export_limit`; the manual field
  just relocates to page 1).

## 10. Output / sensor changes

Unchanged options from the prior draft:
- **O1 (minimal):** add the `per_site` list to "Tuned Panel Azimuth" too; document the headline
  as the property/`_total` value.
- **O2:** per-array Tuned Tilt/Azimuth sensors (2×N entities, dynamic).
Recommend O1 now, O2 optional later.

## 11. Decisions required

1. **Capacity = Option 1** (page-1 total authoritative; per-array defaults from discovery). ✅
2. **Topology as its own step (6)** vs folded onto page 1 for multi-array? *(recommend its own
   short step — keeps page 1 to property basics and is read before the array pages render.)*
3. **direct page-1 generation optional** (blank → `_total` = sum of per-array AC)? *(recommend
   yes; ties to item 2 P0b.)*
4. **Tilt override = seed only** (tuning may still refine) vs hard value (skip tuning for that
   array)? *(recommend seed only.)*
5. **Capacity AC vs DC** reconciliation (§6) — one documented meaning.
6. **Persist a per-array value only when it differs from the discovered default** (avoid
   freezing a stale Solcast value)? *(recommend yes.)*
7. **Outputs O1 vs O2.**
8. **Per-array page UX:** one combined page per array (orientation + measurement together)
   vs splitting orientation and measurement into sub-pages? *(recommend one page per array.)*

## 12. Migration / backward compatibility

- Existing `CONF_SITE_GROUPS` (1.9.x single-page shape) must still load and prefill the new
  per-array pages. `CONF_SITE_TOPOLOGY` already persists; `_infer_topology` still defaults it.
- **dc_split migration:** today the shared AC is stored on the group (`ac_sensor`) *and* equals
  page-1 `CONF_PV_ACTUAL_SENSOR` (verified in the user's live config). The new model sources it
  from page 1, so `_derive_groups` sets the group `ac_sensor` = page-1 sensor — already aligned;
  no data loss. Per-array DC + MPPT prefill from the existing `strings`.
- **direct migration:** existing single-site groups keep their own `ac_sensor`.
- No SQLite schema change (config-entry options only).

## 13. Testing (once approved)

- Single-array unchanged (no topology step, no array pages; export limit now on page 1).
- Multi-array: topology step → N array pages; round-trip prefill from discovery + stored groups.
- dc_split derives one group (page-1 AC + strings); direct derives single-site groups.
- Resolver precedence (override > discovery > page-1) for tilt/azimuth/capacity.
- `_total` multi-array seed: capacity = page-1 total; tilt/azimuth = capacity-weighted mean.
- "Confirm unchanged" doesn't freeze an override.
- Convention correctness (Solcast in/out, internal East-positive in the tuner).
- Migration from a 1.9.x entry (the user's dc_split config) loads and re-derives cleanly.

## 14. Docs to sync at implementation

README (new wizard structure + per-array pages + page-1 export limit), `DESIGN_DOCUMENT.md`
(multi-site config model), `CLAUDE.md` (multi-site + config-flow paragraphs), `strings.json` +
all 11 translations (new step/array page titles, field labels, topology step — now
plain translated keys rather than name-embedded). The README config-wizard **screenshots** will
need recapturing for the new structure.
