# Spec: Short-horizon local PV decision support for load scheduling (item 3)

Status: **draft / design discussion** · Version: **0.1** · Updated: **2026-06-28**

Analysis + options, no code. **Supersedes/re-scopes the dropped Feature 4** (DESIGN_DOCUMENT.md
§"Feature 4 — Short-range Forecast Correction (Dropped)"). Depends on item 2 (per-site shading)
for the structural-correction half and on item 1 (per-site forecast) where per-site confidence is
wanted. Decisions in §10 open (surfacing resolved — §11). Decision trace in §14.

> **One-line scope.** This is **not a forecast**. It is a *confidence + best-load-window decision
> aid* layered on top of the base integration's forecast. We never publish a rival "next X hours
> PV" number — see §4 and the design constraint in §8.1.

## 1. Purpose

Help a user answer the operational question the raw forecast doesn't: **"can I trust the next few
hours enough to turn on a heavy load (EV, pool pump, hot water, dishwasher) right now, and if not,
when?"** The reasoning is short-horizon (next ~1–6 h) and decision-oriented, distinct from the
durable, time-of-year dampening corrections of items 1–2.

## 2. The two correction perspectives (why this is a separate feature)

The integration corrects/annotates the forecast from two mathematically different angles. They must
**not** share a mechanism:

| | **P1 — structural shading** (item 2) | **P2 — cloud/diffuse** (this spec) |
|---|---|---|
| Nature | Deterministic (sun geometry vs obstructions) | Stochastic (weather) |
| Predictability | Arbitrarily far ahead; repeats yearly same date/time | Decays fast; useful only next ~1–3 h |
| Mechanism | **Persistent** `set_dampening` push *into* the base | **Live, read-only** advisory *on top of* the base |
| Written back to base? | **Yes** — improves the base's own sensors in place | **No** — never; it would corrupt time-of-day dampening |

`set_dampening` is indexed by local time-of-day, so a now-relative, decaying correction structurally
cannot live there (the original Feature 4 kill-reason #3). For P1 that indexing is correct; for P2 it
is fatal — which is exactly why P2 is a separate advisory, not a forecast correction.

## 3. Supersedes Feature 4 — why the reframing is viable

Feature 4 (evaluated and dropped v1.3.0) tried to *nudge the canonical forecast* for +1–6 h from the
recent `total_pv / pv_estimate` ratio. It was dropped for four reasons; re-scoping to a
**load-scheduling decision aid** addresses all four:

| Feature 4 kill-reason | How item 3 addresses it |
|---|---|
| 1. Signal decays by +3 h — no-op exactly where error is largest | Item 3 targets the **+0–90 min** window where local persistence skill is *highest*; the objection inverts in our favour |
| 2. Cruder single-inverter ratio + coarse OWM cloud second-guesses Solcast imagery | We no longer use cloud %; we use sound Open-Meteo **irradiance** + measured **Kt** ([[dampening-kt-followup]]), and we **don't publish a rival forecast** at all |
| 3. Can't go through `set_dampening`; forks into separate sensors, rewiring automations | For a **decision-support sensor this is the intended design**, not a drawback — nothing rewires |
| 4. Durable part already captured by DB dampening | Correct — that's P1. Item 3 is the *non-durable* residual dampening deliberately doesn't touch |

At implementation, flip Feature 4's status in `DESIGN_DOCUMENT.md` from "no implementation planned" to
"re-scoped — see item 3 / this spec."

## 4. Relationship to the base component (the load-bearing decision)

The base integration already answers "how much PV in the next X hours" directly from Solcast
(`forecast_now`, next-hour / next-X-hour sensors, `detailedForecast`, the Energy-dashboard feed). A
second number that silently disagrees would be cruder, confusing ("which do I trust?"), and is
Feature 4 kill-reason #2. **So we do not compete on the forecast number.** The link-back is
asymmetric:

- **P1 writes back** into the base via `set_dampening` — a base user who never opens our sensors
  still gets a shading-corrected `pv_estimate`. Symbiosis, no overlap.
- **P2 is read-only**, consuming the base forecast as its base layer and annotating it. It cites the
  base as the source and never overwrites it.

**The only defensible unique value is the closed loop.** The base is open-loop (Solcast → you); we
are the only component that measures actual production and compares it back. Everything P2 offers
*requires* that loop and is therefore something the base structurally cannot produce:

| The base can't know… | …but we measure/derive it |
|---|---|
| Has this array run *below* forecast for the last 1–3 h? | actual-vs-forecast per slot in the DB |
| Does local reality (measured Kt, independent POA) **agree** with Solcast's next 1–3 h? | both signals available to compare |
| Is the upcoming window shaded **at this site**? | item 2 SVF / horizon mask ([[string2-morning-shading]]) |
| Given **my** battery + export limit, when is the usable surplus window? | battery state + per-site export limit already read |

## 5. Signals available (the closed loop)

- **Stored actuals vs forecast** — `total_pv` / `pv_estimate` per 30-min slot, per site and `_total`.
- **Measured Kt** — `ghi / clearsky_ghi(zenith)`, the same gate used in tuning; the sound clear-sky
  discriminator (Open-Meteo cloud % is unreliable — `analysis/cloud_owm_vs_openmeteo_jun2026.md`).
- **Open-Meteo irradiance** — GHI/DNI/DHI; transposable to POA with the already-tuned tilt +
  `capacity_scale`. **Forward** horizon not yet fetched (see §7 gap).
- **Base forecast** — `detailedForecast` / `pv_estimate(10/90)` for the upcoming slots (avg kW).
- **Battery + export headroom** — battery SOC/charge reads and the per-site export limit.

## 6. Method — confidence + window, not a rival number

**6.1 Local short-horizon estimate (internal, not published as a forecast).** A horizon-weighted blend:

```
estimate(h) = w(h)·local(h) + (1 − w(h))·solcast(h),   w(h): ~1 at t+0 → 0 by the skill crossover (~+3 h)
```

Two candidate `local(h)` signals (phased — see §9):

- **(a) Bias-persistence (MVP).** Recent `total_pv / pv_estimate` ratio carried forward with
  exponential decay. This is the Feature 4 mechanism, re-scoped to the short window and used only to
  derive confidence — never published as kW.
- **(b) Independent POA second opinion (upgrade).** Open-Meteo **forecast** GHI/DNI/DHI transposed
  with the tuned tilt + `capacity_scale` → an independent next-hours power signal. Its role is
  **divergence detection feeding confidence**, not "more accurate kW."

**6.2 Confidence / agreement signal (the primary output).** How well do the local signals agree with
Solcast's next 1–3 h? High agreement (recent bias ≈ 1, measured Kt and POA consistent with
`pv_estimate`) → high confidence → safe to commit a load. Divergence → Solcast may be locally off →
hold. The base cannot emit this; it has no ground truth.

**6.3 Best-load-window recommendation.** Combine the *base* forecast + structural shading (P1) +
battery SOC + export headroom + the confidence signal into "the next good window to run a heavy load
is HH:MM–HH:MM, expected usable surplus ≈ N kWh." A decision, not a duplicate forecast number.

## 7. Capability gap

`OpenMeteoClient` currently fetches **current + archive** only (CLAUDE.md). Path (a) needs nothing
new. Path (b) requires a new **forward-horizon** irradiance fetch (Open-Meteo `/v1/forecast` future
hours) — a discrete, additive client method, transposed by existing tuning machinery.

## 8. Design constraints / principles

1. **No colliding forecast sensor.** Never emit a sensor that looks like the base's forecast and
   quietly disagrees. Outputs are differently-named, differently-typed: a confidence %, a recommended
   timestamp/window, an expected-surplus-kWh-given-battery. (Hard rule; this is what keeps us out of
   the encroachment trap and out of Feature 4's failure mode.)
2. **Cite, don't overwrite.** P2 reads the base forecast as its base layer and references it; only P1
   writes back (via `set_dampening`).
3. **No cloud %.** Condition on measured Kt + irradiance + the Solcast ensemble band only (§2a of the
   item 2 spec).
4. **Honest uncertainty.** Surface `pv_estimate10/90` as the band; don't overclaim skill past ~+3 h.
5. **Compose with P1, don't double-count.** Base forecast → P1 shading correction → P2 confidence
   overlay; per-site shading applied once.

## 9. Honest limits

- 30-min irradiance sampling is coarse for true nowcasting — adequate for "should I run the EV/pool
  pump in the next few hours," not minute-scale.
- Persistence breaks on fast frontal cloud; the ensemble band is the honest fallback.
- **We will not out-nowcast Solcast's irradiance** — their satellite near-term product is their
  strength and a single-site cruder signal won't beat it generally. Our edge is **site-specific
  deviation** (persistent local bias, structural shading, "is it underperforming right now"), not a
  better cloud forecast. This boundary is why the local signal leans toward the bias/agreement role
  (a) rather than a rival POA forecast (b).

## 10. Decisions / open questions

1. **Deliverable shape** — raw "PV next X hours" numbers (rejected by §8.1), a derived "best load
   window" recommendation, a confidence %, or the latter two together? What does the user wire an
   automation to?
2. **Local signal** — ship bias-persistence (a) first, or is the independent POA second-opinion (b)
   the point, justifying the forward-fetch (§7) from the start?
3. **Granularity** — property-wide only, or per-site confidence (needs item 1 per-site forecast)?
4. **Horizon X and skill-crossover** — fixed (e.g. 6 h, crossover +3 h) or learned from the site's
   own bias-decay history?
5. **Confidence encoding** — a 0–100 % number, a 3-state (high/medium/low) category, or both?
6. **Window objective** — maximise absolute PV, PV-minus-baseload headroom, or battery-aware usable
   surplus (PV beyond what the battery will already soak up)?
7. ~~**Surfacing**~~ — **resolved (§11):** two sensors + one binary_sensor (MVP), calendar entity as
   a Phase-2 option.

## 11. Presentation (MVP)

Three net-new entities, all distinct *types* from the base's kW forecast sensors, so the
no-colliding-forecast rule (§8.1) holds by construction. They follow the existing pattern (15
`CoordinatorEntity` sensors; `_attr_has_entity_name = True` + translation keys, native `STATE_ON`/
`STATE_OFF`). Property-wide for MVP (§10.3).

| Entity | Platform | State | Key attributes | Default |
|---|---|---|---|---|
| **PV Forecast Confidence** | `sensor` | `0–100` | `rating` (`high`/`medium`/`low`), `recent_bias`, `kt_agreement`, `poa_divergence` (phase 2), `horizon_hours`, `based_on` (cites base forecast) | enabled |
| **Good Load Window** | `binary_sensor` | `on`/`off` | `window_end`, `expected_surplus_kwh`, `confidence`, `reason` | enabled |
| **Next Load Window** | `sensor` (`device_class: timestamp`) | window start, or `unknown` | `window_end`, `duration_min`, `expected_surplus_kwh`, `confidence` | enabled |

**The surplus attribute is decision-framed, not a forecast (§8.1 guard):** `expected_surplus_kwh` is
**PV beyond what the battery will already absorb + the dwelling baseload** over the window — usable
headroom for a deferrable load, *not* a raw PV total. Never expose a raw "PV next X h" number.

### 11.1 Draft user-facing text (strings)

> Entity names use translation keys, not raw `_attr_name` (project standard). Draft English copy for
> `strings.json` / `translations/en.json`:

```jsonc
"entity": {
  "sensor": {
    "pv_forecast_confidence": {
      "name": "PV forecast confidence",
      "state_attributes": {
        "rating":        { "name": "Rating" },
        "recent_bias":   { "name": "Recent actual vs forecast" },
        "kt_agreement":  { "name": "Clear-sky (Kt) agreement" },
        "horizon_hours": { "name": "Horizon" },
        "based_on":      { "name": "Based on" }
      }
    },
    "next_load_window": {
      "name": "Next load window",
      "state_attributes": {
        "window_end":           { "name": "Window ends" },
        "duration_min":         { "name": "Duration" },
        "expected_surplus_kwh": { "name": "Expected usable surplus" },
        "confidence":           { "name": "Confidence" }
      }
    }
  },
  "binary_sensor": {
    "good_load_window": {
      "name": "Good load window",
      "state_attributes": {
        "window_end":           { "name": "Window ends" },
        "expected_surplus_kwh": { "name": "Expected usable surplus" },
        "reason":               { "name": "Reason" }
      }
    }
  }
}
```

**Surrounding copy — Configure page / README blurb (draft):**

> **PV forecast confidence & load windows.** These entities do *not* replace the Solcast forecast —
> they sit on top of it and tell you *how much to trust the next few hours at your site*, using your
> measured production. **Good load window** turns `on` when now is a good time to run a deferrable
> heavy load (EV, pool pump, hot water); **Next load window** tells you when the next one starts; and
> **PV forecast confidence** scores how well local measurements agree with the forecast (low
> confidence ⇒ the forecast may be off at your site right now). "Expected usable surplus" is solar
> beyond what your battery and baseload will already use — headroom for an extra load.

**Tooltip / longer description (draft, for docs):** *"Confidence compares your array's recent actual
output and measured clear-sky index against Solcast's forecast for the next 1–3 hours. High = local
reality matches the forecast; low = your site is diverging (local cloud, shading, or a bias the
forecast hasn't caught), so hold heavy loads."*

### 11.2 Example automation (the binary_sensor is the actionable trigger)

```yaml
automation:
  - alias: "Run pool pump in a good PV window"
    trigger:
      - platform: state
        entity_id: binary_sensor.good_load_window
        to: "on"
    condition:
      - condition: numeric_state
        entity_id: sensor.pv_forecast_confidence
        above: 60
    action:
      - service: switch.turn_on
        target: { entity_id: switch.pool_pump }
  # turn back off when the window ends:
  - alias: "Stop pool pump when window ends"
    trigger:
      - platform: state
        entity_id: binary_sensor.good_load_window
        to: "off"
    action:
      - service: switch.turn_off
        target: { entity_id: switch.pool_pump }
```

### 11.3 Phase-2 surfacing option

A `calendar` entity exposing each upcoming load window as an event (start/end) — native HA calendar
triggers + dashboard visibility. Elegant but more work and less conventional; not MVP.

## 12. Recommendation (phased)

1. **MVP** — signal (a) bias-persistence → the **confidence** sensor (§6.2) + the **Good/Next Load
   Window** entities (§6.3, §11) over the base forecast. No new fetch, no rival number.
2. **Upgrade** — add (b) the independent POA second opinion (§7 forward-fetch) feeding `poa_divergence`
   into confidence; mirrors item 2's A→B phasing. Optionally add the §11.3 calendar entity.

## 13. Docs to sync at implementation

`README` (load-scheduling decision aid + "what's new"), `DESIGN_DOCUMENT.md` (flip Feature 4 status →
"re-scoped, item 3"; add the confidence/window section), `CLAUDE.md` (new sensors + base-coupling
read paths), `strings.json` + every `translations/*.json` for any new sensors/options. Ties to item 1
(per-site forecast), item 2 ([[string2-morning-shading]], per-site shading), [[dampening-kt-followup]],
and `analysis/cloud_owm_vs_openmeteo_jun2026.md`.

## 14. Revision history

Each row is a content version of this spec; **Commit** is the commit where that version's design
content landed. Bump the version and add a row whenever a decision lands or analysis materially
changes. The version header and this table may post-date some rows' content (versioning was
retrofitted onto already-committed specs), so a row's commit can predate the table;
infrastructure-only commits — the versioning retrofit and hash back-fills — are intentionally not
given their own rows. Full trace lives in git; this table keeps the decision evolution legible
without archaeology.

| Ver | Date       | Change                                                        | Commit    |
|-----|------------|---------------------------------------------------------------|-----------|
| 0.1 | 2026-06-28 | Initial spec: re-scope dropped Feature 4 as a load-scheduling decision aid (confidence + window), P1 write-back / P2 read-only, no-colliding-forecast constraint | `b37458f` |
