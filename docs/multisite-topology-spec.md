# Spec: Topology-aware per-site sensor mapping

Status: **implemented and shipped in v1.9.0** (PR #34, 2026-06-26). The §13 decisions
were confirmed and built as PR-A (the topology gate: selector, `_infer_topology`,
`_validate_dc_split`, mode-aware schema/parse/derive) and PR-B (derive `_total` from the
per-site sum when no system sensor is configured). The only deferred item is the T3
`Σ per-site ≈ total` sanity warning (§13.5). This document is retained as the design
record; behaviour is now documented in `README.md`, `DESIGN_DOCUMENT.md`, and `CLAUDE.md`.

## 1. Problem

The "Per-site sensor mapping" step presents a "generation sensor" + an optional
"DC/MPPT sensor" per site with no indication of which physical topology applies.
Distinct topologies collapse onto one ambiguous form, causing:

- **Silent data loss** — a DC sensor on a non-shared site is dropped
  (`_derive_groups`, the `len(members) == 1` branch).
- **A hidden requirement** — the split only works if the AC entity is *byte-identical*
  on every shared row.
- **Mislabelling** — "generation sensor" gives no hint it is the AC basis, and a stale
  stored value silently overrides the page-1 pre-fill (`_build_sites_schema`,
  `a.get("ac") or default_ac`).

Solcast carries **no inverter-type signal** (confirmed against `data/sites_*.json` —
only orientation / capacity / `capacity_dc`), so topology must be a user choice, not
inferred from Solcast.

## 2. Topology taxonomy

| ID | Topology | Solcast sites | Per-site generation | Property total generation | Property export | Per-site step |
|----|----------|---------------|---------------------|---------------------------|-----------------|---------------|
| **T0** | One string inverter, one array | 1 | n/a (single) | page-1 sensor → `_total` | page-1 sensor | **skipped** (`_is_single_site()`) |
| **T1** | One inverter, multiple MPPTs | ≥2 | apportioned `ac × dcᵢ/Σdc` | page-1 sensor → `_total` | page-1 sensor | shown — **DC-split mode** |
| **T2** | Microinverters / one inverter per array, **no** total sensor | ≥2 | each site's own AC sensor | *(none configured)* | *(none/optional)* | shown — **direct mode** |
| **T3** | Microinverters **with** property totals | ≥2 | each site's own AC sensor | page-1 sensor → `_total` | page-1 sensor | shown — **direct mode** |

Notes:
- T0 already works (step skipped; flat MPPT V/I fields live on Step 1). The gate is only
  relevant when there are ≥2 sites.
- T2 and T3 are the **same** "direct" gate branch; the only difference is whether the
  page-1 total generation/export sensors are populated (an independent layer).
- Solcast data has no inverter-type signal; `capacity_dc == capacity` is a *weak*,
  unreliable microinverter hint and must **not** drive auto-selection.

## 3. Design — the gate

Add a **single mode selector** at the top of the per-site step (not a separate step —
keeps the flow at 6 steps and the choice adjacent to the fields it controls):

> **"How are your arrays measured?"**
> - **Each array has its own generation sensor** — microinverters (e.g. Enphase) or one
>   inverter per array. *(T2 / T3)*
> - **One inverter shared across arrays, split by DC** — single inverter with multiple
>   MPPTs. *(T1)*

Behaviour by mode:

- **Direct (T2/T3):** per site render **generation sensor** + **sensor type** + MPPT V/I
  telemetry fields. **No DC field.** Each site → single-site group. Silent-drop becomes
  structurally impossible.
- **DC-split (T1):** per site render **AC generation sensor** (pre-filled from page-1 on
  *every* row) + **DC sensor** + **sensor type** + MPPT V/I fields. Derives one shared
  group with `strings`.

The selector is a `SelectSelector` (radio/dropdown), `vol.Required`, default inferred (§6).

## 4. Data model

**No change to `CONF_SITE_GROUPS` shape.** The derived groups already encode the topology
(`strings` present ⇒ T1; bare `site` ⇒ T2/T3).

Optionally persist the chosen mode as a new `CONF_SITE_TOPOLOGY` key
(`"direct"` | `"dc_split"`):
- Re-infer (no new key): simpler, zero migration, but a fresh second site with no groups
  has no signal → falls to a default.
- Persist: stable across edits, self-documenting in `.storage`, costs one const + one
  translation line.

**Recommendation: persist**, keeping inference as the *default* for pre-feature entries.

## 5. Config-flow changes

Both `SolcastEnhancedConfigFlow` and `SolcastEnhancedOptionsFlow` (they mirror):

| Function | Change |
|---|---|
| `_build_sites_schema` | New `mode` param. Insert the mode selector as the first field. When `mode == "direct"`, omit every `k_dc` field. Still return `field_map` (DC keys absent in direct mode). Relabel the generation field per mode ("AC generation sensor (shared)" vs "generation sensor"). |
| `_parse_sites_input` | Read the mode field; when direct, force `dc = None` regardless of input. Keep `if not ac: continue`. |
| `_derive_groups` | Accept `mode`. Direct: never build `strings` (every member → single-site group). DC-split: keep current logic, but surface a **UI error** (not just a log) when a member has no DC under a shared AC. |
| `async_step_sites` (both flows) | Compute default mode (§6); pass into `_build_sites_schema`; on submit read mode, pass into `_derive_groups`, persist `CONF_SITE_TOPOLOGY` (if adopted). |
| `_groups_to_assignments` | Unchanged. |

The mode field key must be a fixed, name-free key (e.g. `"__site_topology__"`) so it is
distinguishable from the per-site name-embedded keys during `field_map` parsing.

## 6. Default-mode inference (backward compatible)

On entering the step with no explicit stored `CONF_SITE_TOPOLOGY`:
1. Any existing group has `strings` → default **dc_split**.
2. Else ≥2 groups each with a bare `site` and distinct `ac_sensor` → default **direct**.
3. Else (fresh discovery, nothing mapped) → default **direct** (safer, no-data-loss,
   microinverter-friendly).

Existing T1 users keep dc_split; existing T2/T3-shaped configs keep direct; nobody is
surprised on upgrade.

## 7. Validation / UX feedback (the silent-drop fix)

In dc_split mode, replace the silent omission (currently warning-only in `_derive_groups`)
with a **returned form error**: if a row has an AC sensor but no DC sensor (and another row
shares that AC), re-show the step with `errors={"base": "dc_split_missing_dc"}` naming the
site. This converts "it vanished" into "you're missing a DC sensor for Second Site" — the
single most valuable behavioural change.

## 8. Property totals vs per-site (T2/T3 layering)

Three independent layers, by design:
- Page 1 → `_total` row (whole-system generation + export).
- Per-site step → per-site `pv_actual`.
- Export → always property-wide; copied identically onto each site row (current behaviour,
  `coordinator.py` per-site write). No per-site export exists or is planned.

**The `_total` source question (the real T2-vs-T3 fork).** Today `_total.pv_actual`
*always* comes from page-1:
- **T3** (total sensor exists): works as-is.
- **T2** (no total sensor): page-1 generation blank → `_total` row gets zero generation,
  breaking aggregate tuning/dampening (they query `site=DEFAULT_SITE_ID`).

→ **Decision (§13.4):** in direct mode with no page-1 generation sensor, derive
`_total = Σ per-site generation`. Contained coordinator change; makes pure-microinverter
setups work without a redundant total helper. Recommended, but scoped as a **separate
sub-feature** so the gate work stays reviewable on its own.

**Validation opportunity (optional, future):** when both per-site sensors *and* a page-1
total exist (T3), sanity-check `Σ per-site ≈ total` and warn on large divergence
(miswired/double-counted microinverter). Nice-to-have, not v1.

## 9. strings.json + 11 translation files

Per the code-change/translation-sync rule, **every** file in `translations/`
(de, en, es, fr, it, ja, nl, pl, pt, sk, ur) plus root `strings.json` needs:
- The new mode selector label + its two option labels.
- A rewritten `sites` step `description` (the selector now disambiguates, so it can shrink).
- The new error string `dc_split_missing_dc`.

Per-site **field** labels stay dynamic (name-embedded) → no per-field translation. The
AC-vs-generation relabel is built in code from `mode`, not a translation change.

## 10. Backward compatibility & migration

- No `CONF_SITE_GROUPS` schema change → existing entries load unchanged.
- New `CONF_SITE_TOPOLOGY` is additive/optional; absence → inferred (§6). No
  `async_migrate`/`user_version` bump (config-entry options, not SQLite schema).
- Coordinator consumers (`_read_site_actuals`, `_derive_groups`) are untouched — they
  already read `strings` vs `site`. (Exception: the optional §8 `_total`-from-sum change.)

## 11. Edge cases

- **Mode switch with stale fields:** dc_split→direct must clear stored DC values on derive
  (don't resurrect). direct→dc_split with no DC entered → trips the §7 error rather than
  silently producing single-site groups.
- **Single site (T0):** step still skipped; selector never shown.
- **Mixed reality** (some arrays shared, some independent): out of scope for v1 — one mode
  per property. Manual `CONF_SITE_GROUPS` still supports mixed if hand-authored.
- **`capacity_dc == capacity`** is a weak microinverter hint — do **not** auto-select mode
  from it.

## 12. Testing

Extend `tests/` config-flow tests:
- T1 path: shared AC + per-site DC → one group, two `strings`.
- T2/T3 path: distinct AC per site, no DC field rendered → single-site groups.
- Default inference for each existing-config shape (§6).
- Mode switch clears stale DC.
- dc_split with a missing DC sensor → form error, not silent drop.
- T0 still skips the step.
- (If §8 adopted) direct mode, no page-1 total → `_total` = Σ per-site.

## 13. Decisions — CONFIRMED

1. **Persist `CONF_SITE_TOPOLOGY`.** ✅
2. **Selector in the existing sites step** (in-step). ✅
3. **One mode per property for v1** (no mixed). ✅
4. **T2 (direct, no total sensor): derive `_total` as Σ per-site** — separate sub-feature. ✅
   (Ordering verified: `_read_site_actuals` already runs before the `_total` row write, so
   no reordering needed.)
5. **Defer the T3 `Σ per-site ≈ total` sanity warning.** ✅

## 14. Docs to sync at implementation (release rule)

- `README` — feature + "What's new" header.
- `DESIGN_DOCUMENT.md` — multi-site section + this topology table.
- `CHANGELOG`.
- `CLAUDE.md` — the "Config-flow fields are placed by topology" paragraph (developer-facing).
- Every `translations/*.json` + `strings.json` (§9).
