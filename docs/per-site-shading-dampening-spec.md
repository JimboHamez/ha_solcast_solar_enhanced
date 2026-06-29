# Spec: Per-site shading-aware dampening (item 2)

Status: **draft / design discussion** · Version: **0.4** · Updated: **2026-06-28**

Analysis + options. P0 (per-site forecast) and per-site surfacing **shipped in 1.10.0b1**;
component-decomposed Option B still design-only. Decisions in §8 (1/4/6 resolved); §8.5 generalises the
mechanism test across inverter topologies. Decision trace in §10.

## 1. Observation

Multi-array property; the **ground-floor** array (MPPT2 / site `ae8c-…`) consistently
under-produces the **upper-floor** array (MPPT1 / site `8be0-…`). The user observed, on a
cloudy morning, MPPT2 DC power running 700–1500 W below MPPT1, and reasoned that **diffuse
irradiance (DHI) is impeded for the lower panels** by the double-storey property to the
**north**. The data confirms this — and adds a second mechanism.

## 2. Data analysis (`data/solcast_solar_enhanced26062026.db`, 22–27 Jun)

Per-MPPT DC power (per-site DC telemetry rows) and AC actuals:

| Condition | DHI/GHI | MPPT2 ÷ MPPT1 (ground ÷ upper) |
|---|---|---|
| Pure diffuse, DNI≈0 (morning, overcast) | ≈1.0 | **≈0.84** (~16% deficit, *no sun*) |
| Clearing afternoon, DNI rising | 0.7→0.2 | 0.66 → 0.45 → **0.17 @16:00** |
| 5-day AC aggregate, high-diffuse slots | >0.7 | 0.80 |
| 5-day AC aggregate, low-diffuse (clear) slots | <0.4 | 0.62 |

**Two components, responding oppositely to cloud:**
1. **Diffuse / sky-view deficit (~16%, ~constant).** Present with zero direct beam → a
   **reduced sky-view factor**: the northern building obstructs part of the sky dome the lower
   panels see, cutting their DHI capture.
2. **Direct / horizon-mask deficit (sun-position dependent, large at low sun).** As the sun
   drops (afternoon, winter), the obstruction blocks the **beam**; ground array → ~17% of upper.

**Corollaries**
- **The aggregate masks it**: on 26 Jun `_total` actual/forecast = **1.31** (Solcast
  under-forecast the clearing), so property-wide dampening would push the ground array's
  afternoon *up* when it needs pushing *down*. **Per-site is required.**
- Decoupling proof (27 Jun): GHI **302** @13:00 → 4.52 kW, but GHI **320** @15:00 → only
  2.91 kW. Higher irradiance, lower output = self-shading, not weather.
- Per-MPPT DC power is V×I from max-V/min-I interval telemetry (off-MPP capture, not an energy
  meter) — directionally reliable, not exact; AC apportioned actuals agree.

## 2a. The cloud field is unreliable — condition on irradiance, not cloud %

Validated over **Jun 15–27 (11 days, 184 daylight slots)** against measured Kt and the original
OWM Node-RED data (full write-up + methodology: `analysis/cloud_owm_vs_openmeteo_jun2026.md`):
- **Open-Meteo `clouds` is biased high** (mean **80%** vs OWM 57%) and **false-overcasts clear
  days** (15% of slots ≥80% cloud while Kt≥0.6; 11 slots ≥80% with DNI ≥300 W/m², contradicting
  its own irradiance).
- **OWM is under-responsive** (corr with Kt **−0.36** vs OM **−0.77**; under-called genuine
  overcast) — lower bias but doesn't discriminate, so **not a fix**.
- **Both poor** (MAE ~**37%** vs Kt-implied cloud, essentially tied); **Open-Meteo irradiance is
  sound** — only its cloud field is bad.

⇒ Any clear-sky weighting/conditioning in dampening must use **measured Kt / the DHI-DNI split**,
not total cloud % from either provider. This is the same conclusion as the tuning Kt gate,
extended to dampening ([[dampening-kt-followup]]).

**Why Open-Meteo cloud is structurally poor (data source):** the component uses `/v1/forecast`
with no `models=` → Open-Meteo **`best_match`**, which over Australia serves cloud from a coarse
global NWP (BOM ACCESS-G is ~15 km, 6-hourly, **hourly-only**; BOM open-data currently
suspended). So the stored `minutely_15` cloud is **interpolated from hourly** and cannot track a
local clearing — hence the measured 2–3 h lag and false-overcast. Radiation, by contrast, may be
**satellite-derived (Himawari)** in best_match and was sound in our data; cloud and radiation
coming from different sub-models is why a row can read 100% cloud with its own DNI clear. This is
a source-level reason to condition on irradiance, not cloud. See §8.8.

## 3. Physical model

```
POA_effective(site) ≈ SVF(site) · DHI_poa  +  mask(site, sun) · DNI_poa  +  albedo term
```
- **SVF (sky-view factor)** < 1 for an obstructed array — roughly constant (~0.84 ground
  floor); applies to the diffuse term in all conditions.
- **mask(sun)** ∈ [0,1] — 1 when the sun clears the obstruction, →0 when the beam is blocked
  (low sun behind the northern/north-west building); applies to the direct term.

Solcast (and the integration's transposition) assume an unobstructed sky, so they over-predict
an obstructed array — most on clear, low-sun slots.

## 4. Prerequisite — P0: per-site forecast

Per-site dampening needs a per-site actual **and** forecast to form a ratio. In the DB
`pv_estimate` is **0 for both arrays** (only `_total` has a forecast) — the base integration
isn't exposing `detailedForecast-<resource_id>`. Until fixed, no per-site dampening is
possible. Two routes (see item 1 §5/§9):
- **(a)** real per-site Solcast forecast (enable the base's detailed breakdown), or
- **(b)** apportion the `_total` forecast by capacity share — **valid here because the arrays
  share azimuth**; item 1's per-site azimuth makes "safe to apportion?" decidable.

## 5. Options

**Option A — per-site per-half-hour dampening (quick win).**
Once P0 lands, compute per-site slot ratios with the existing machinery; push per-site
`set_dampening`. Ground array afternoon slots get factors < 1; morning ≈ 0.84.
- *Pro:* minimal new code (per-site path already exists, just starved of forecast).
- *Con:* a per-slot factor is a **seasonal average over clear + cloudy days**; because the two
  components respond oppositely to cloud, it over-corrects diffuse afternoons and
  under-corrects clear ones.

**Option B — component-decomposed dampening (recommended target).**
Learn, per site, a **sky-view factor** on the DHI term and a **horizon mask** on the DNI term
(as a function of sun position) from stored GHI/DNI/DHI + actuals (transposition already exists
in tuning). The correction conditions on each slot's **actual DHI/DNI split**, so it
generalises across cloud instead of averaging it. Aligns with §2a (use irradiance, not cloud).
- *Pro:* physically correct; one model covers clear + cloudy; explains the data.
- *Con:* a real model addition (fit two terms per site; new state; validation).

**Option C — diffuse-fraction-conditioned slots (middle ground).**
Per-slot factors, but conditioned/bucketed by Kt or DHI-fraction (separate "clear" vs
"diffuse" factors per slot). Less rigorous than B, more robust than A.

**Variant — explicit horizon profile.** Store an obstruction elevation-by-azimuth profile per
site (user-entered or learned), driving both SVF and mask. Most transparent/accurate, most
complex; could fold into B.

## 6. Interaction with the dampening model + gate

- Current model blends the measured ratio toward neutral 1.0 and clamps **±15%** until
  confident. The ground array's afternoon needs to go well below 0.85, so the **±15% clamp is
  likely too tight** for genuine structural shading; revisit for per-site structural (vs noise)
  corrections.
- The **orientation-divergence gate** must not neutralise a correctly-tuned-but-shaded site —
  shading is not an orientation error. Ensure the gate distinguishes them.
- Tuning's Kt gate *removes* cloudy records; dampening **keeps** them — and for Option B the
  cloudy/diffuse records are **essential** (they isolate the SVF).

## 7. Recommendation (phased)

1. **P0** — per-site forecast (item 1 (b) apportionment suffices while azimuths match).
2. **Move dampening's clear-sky weighting to Kt** (§2a) — independent of the shading work, fixes
   a current degradation.
3. **Option A** — per-site per-slot dampening; ships a real correction with little new code.
4. **Option B** — component-decomposed (SVF on DHI + sun-position mask on DNI) — the
   physically-correct upgrade once A is validated.

## 8. Decisions / open questions

1. ~~P0 route~~ — **resolved/shipped (1.10.0b1): route (b)**, capacity-apportionment of the property
   forecast, gated on shared azimuth (`_apportion_total_forecast`); real per-site detail still wins.
2. Sequence: A first, or jump to B?
3. Relax the ±15% neutral clamp for structural per-site shading?
4. ~~Option B: learn SVF + mask vs user-entered horizon profile?~~ — **RESOLVED (§8.4 Result,
   2026-06-28): uniform dimming ⇒ learn the smooth two-term model; no horizon profile needed.**
5. Keep the divergence gate from suppressing a shaded (but correctly oriented) site.
6. ~~Surfacing: per-site dampening factors / a "shading" diagnostic sensor?~~ — **shipped (1.10.0b1):
   one `SiteShadingSensor` per array (avg daytime factor + shading%/tuning/confidence attrs).**
7. Cloud field: demote to display-only, derive from Kt, or keep OWM as an optional number?
8. **Spike — Open-Meteo source/resolution (largely answered 2026-06-27; see
   `analysis/cloud_owm_vs_openmeteo_jun2026.md` "Multi-model corroboration").**
   - **Cloud is model-dependent/unreliable** — ECMWF/GFS/ICON/JMA disagree by ≥50 pts in 46% of
     daylight hours; none track Kt strongly. ⇒ no cloud model is the fix; **use Kt** (closed).
   - **Radiation is consistent across models** (GHI corr 0.78–0.90) ⇒ Kt is robust (closed).
   - **AU radiation is hourly** from both NWP and satellite (Himawari satellite product exists
     for the site but `minutely_15` is empty). So no truly sub-hourly weather signal exists.
   - **Half-hour-mean GHI:** the `minutely_15` radiation is *not* pure interpolation — it carries
     the solar-geometry sub-hourly shape (~21/47 daytime blocks non-linear), so averaging the two
     in-interval samples has a **modest geometric gain** (largest near sunrise/sunset), not zero;
     it can't add sub-hourly weather. ⇒ low-priority, do it with the dampening-Kt work if cheap.
   - **Still open:** does pinning **satellite radiation** (Himawari, hourly) materially improve
     accuracy vs `best_match` GHI? (No on-site truth to settle it here.)

### 8.4 — Mechanism confirmation via 1-sec DC V/I (deciding evidence for Option B form)

The stored per-MPPT telemetry is **power-only**, from off-MPP max-V/min-I capture at 30-min
resolution (§2.39). It establishes *that* the ground array under-produces and by how much, but it
**structurally cannot** separate voltage from current — and that separation is what identifies the
shading mechanism and therefore the correct Option B shape. This is diagnostic ground truth read
off the inverter's native ~1-sec DC sensors (HA history), **not** something the integration
ingests (it persists 30-min rows only); the fitted model still learns from stored GHI/DNI/DHI +
actuals.

The signature to look for on MPPT2:

- **Current down, voltage ≈ Vmp** (the observed "low DC current at normal Vmp" — [[string2-morning-shading]]):
  the array is **uniformly dimmed**, current tracks irradiance, voltage holds. This is a clean
  **multiplicative** reduction — exactly what Option B assumes (smooth SVF·DHI + mask·DNI scalars).
  If this is the signature, the two-term fit is well-founded.
- **Voltage stepping down in chunks**: bypass diodes activating on a **hard partial shadow**
  crossing part of the string → **nonlinear** loss a scalar SVF/mask cannot model; needs the
  explicit horizon-profile variant (§5 variant) instead.

⇒ This decides **open question 4** (learn smooth SVF+mask vs store a horizon profile) and validates
that a two-term model is even the right shape. The single most useful artifact is a **clear-morning
vs cloudy-morning trace of MPPT2 voltage and current plotted separately** (not power): uniform
dimming ⇒ A→B as written; bypass-diode steps ⇒ B-with-horizon-profile. Treat this trace as a
**prerequisite check before committing to Option B's form** (it does not block Option A).

#### Result — RESOLVED 2026-06-28: uniform dimming, Option B smooth form confirmed

Analysed a 30-day 1-sec V/I export (`tools/analyse_vi_shading.py`; ~11 days of per-second data;
writeup `analysis/session-2026-06-28-vi-shading-mechanism.md`). Over **7 shaded mornings / 645
samples**:

| Metric | Median | Reading |
|---|---|---|
| `I2/I1` | **0.52** | MPPT2 makes ~half MPPT1's current — shading deficit confirmed |
| `V2/V1` (unshaded control) | **0.996** | voltage ≈ the unshaded string — no collapse |
| `V2/Vmp2` | **1.24** | voltage at/above its own Vmp (rides high at low current) |
| `V2 < 0.8·Vmp2` | **8.5 %** | no clustering at 0.66/0.33 bypass fractions |

**The deficit is carried entirely by current; voltage holds near each string's own Vmp.** That is
the uniform-dimming signature — **no bypass-diode collapse**. So **open question 4 is resolved:
Option B's smooth multiplicative `SVF·DHI + mask·DNI` form is correct; the horizon-profile/bypass
variant is not needed** for this site.

**Seasonal corollary (user, 2026-06-28).** MPPT2's *midday* Vmp also runs ~17 % below MPPT1's in
winter, but **both strings are 9 identical panels** — so this is **not hardware**; it's the same
diffuse-deficit shading, and **in summer the two strings are ~equal**. This makes the deficit
**season/sun-position dependent** (both the constant diffuse SVF deficit and the direct horizon-mask
peak at low winter sun, fade by summer). Design consequence: a **fixed annual per-site factor is
wrong**; the dampening's **±14-day day-of-year window already makes the correction seasonal** (so
even Option A self-adjusts winter↔summer), and Option B's value-add is the **within-season**
clear-vs-cloudy DHI/DNI conditioning, not the seasonal axis. (The 17 % winter Vmp gap exceeds what a
purely uniform diffuse reduction would give — hinting at a mild intra-string gradient, lowest ground
panels seeing least sky — still smooth and Option-B-compatible.) Reinforces §2a (condition on
irradiance, not a clock factor) and §3 (the sun-position mask).

### 8.5 — Generalising the mechanism test across inverter topologies

§8.4 settled the mechanism *for this site* (string inverter, per-MPPT DC). Others will use this, so
the classifier has to handle the whole population — and the topology changes both the physics and the
available signal.

**Key insight: the V/I test is string-inverter physics.** "Voltage holds vs collapses" is a *series
string* phenomenon. Panel-level power electronics change it:

| Topology | Bypass nonlinearity | DC V/I mechanism signal | Mechanism route |
|---|---|---|---|
| **String inverter** + per-MPPT DC (this site) | real — genuine bypass risk | available (`dc_vmed1/2`) | **V/I audit** earns its keep |
| **Optimiser** (SolarEdge, Tigo) | **hardware-mitigated** (per-panel MPPT recovers partial-shade loss) | **gone** — string voltage is **regulated ≈ constant** | smooth model is correct *by construction*; V/I N/A |
| **Microinverter** (Enphase) | **hardware-mitigated** | **absent** — no DC string exposed | smooth model by construction; V/I N/A |

So for the modern optimiser/microinverter population the smooth `SVF·DHI + mask·DNI` model isn't just
a safe default — it's the *physically appropriate* one, because the hardware removed the nonlinearity
the horizon-profile variant exists to catch.

**Two routes to the per-site verdict:**

1. **V/I audit (string inverters with per-MPPT DC).** The discriminator we validated, automated:
   over recurring shaded slots, does the string's median operating voltage (`dc_vmed`) hold near its
   learned Vmp or collapse? Captured forward-only as of 1.10.0b1. **Self-disqualify guard:** a string
   whose voltage is *near-constant across conditions* is optimiser-regulated (SolarEdge) → skip the
   test; it carries no mechanism information.
2. **AC residual-shape proxy (universal — no DC needed).** Bin the actual-vs-forecast residual by sun
   position: a **broad, smooth** deficit scaling with diffuse fraction ⇒ uniform/SVF; a **sharp,
   sun-position-locked notch** that recurs at the same solar az/elevation and marches seasonally ⇒ a
   fixed near-object **hard shadow** (bypass-prone). Weaker than V/I but available to every site, and
   the only route for optimiser/microinverter installs.

**Topology-aware routing — reuse the shipped `CONF_SITE_TOPOLOGY` gate:** `direct` (microinverter /
per-array inverter) → no V/I, smooth model + AC residual-shape proxy; `dc_split` (shared string
inverter, per-MPPT DC) → V/I audit available, but apply the regulated-voltage self-disqualify first.

**Module-level monitoring (bonus, out of scope).** SolarEdge/Enphase expose per-panel power — a far
richer shading map than any string V/I — but it lives in the vendor cloud and is rarely in HA cleanly.
"If the entities exist" opportunity, not a dependency.

**Design stance.** The classifier is a **per-site model-form selector + diagnostic**, *not* a
continuous input to the sum: default everyone to the smooth model (correct for uniform dimming and for
hardware-mitigated optimiser sites; for hard partial shadow it only *under-fits*, never breaks), and
use the classifier to catch the **minority of string-inverter sites** that genuinely need the
horizon-profile escalation — and to surface a useful "partial-shadow on string X around HH:MM"
diagnostic regardless. Data status: `dc_vmed1/2` collection shipped (1.10.0b1) as forward-only
groundwork; the classifier/verdict is future.

## 9. Docs to sync at implementation

README (multi-site shading behaviour + cloud-source note), `DESIGN_DOCUMENT.md` (dampening
model + per-site + Kt weighting), `CLAUDE.md` (dampening + multi-site paragraphs),
strings/translations for any new options/sensors. Ties to item 1 (per-site config/forecast),
[[dampening-kt-followup]], and `analysis/cloud_owm_vs_openmeteo_jun2026.md`.

## 10. Revision history

Each row is a content version of this spec; **Commit** is the commit where that version's design
content landed. Bump the version and add a row whenever a decision lands or analysis materially
changes. The version header and this table may post-date some rows' content (versioning was
retrofitted onto already-committed specs), so a row's commit can predate the table;
infrastructure-only commits — the versioning retrofit and hash back-fills — are intentionally not
given their own rows. Full trace lives in git; this table keeps the decision evolution legible
without archaeology.

| Ver | Date       | Change                                                                 | Commit    |
|-----|------------|------------------------------------------------------------------------|-----------|
| 0.4 | 2026-06-28 | Add §8.5 — generalise the mechanism test across inverter topologies (string V/I audit + SolarEdge regulated-voltage self-disqualify, AC residual-shape proxy as universal route, topology-gate routing, smooth model as default); `dc_vmed1/2` capture shipped in 1.10.0b1 | `d5c7ad8` |
| 0.3 | 2026-06-28 | §8.4 RESOLVED from 30-day 1-sec V/I — uniform dimming ⇒ Option B smooth form; seasonal (summer-equal) corollary; decisions 1/4/6 closed (P0 + surfacing shipped in 1.10.0b1) | `e5999a9` |
| 0.2 | 2026-06-28 | Add §8.4 — 1-sec DC V/I as deciding evidence for Option B's form        | `5d881f5` |
| 0.1 | 2026-06-27 | Initial spec: two-component shading, §2a cloud-unreliability, options A/B/C | `9df5f90` |
