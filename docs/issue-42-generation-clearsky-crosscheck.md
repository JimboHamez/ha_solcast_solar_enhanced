# Discussion: Generation-based clear-sky cross-check (issue #42)

Status: **draft / design discussion** · Version: **0.1** · Updated: **2026-07-06**

Response draft + design framing for [issue #42 — *Use real-time solar generation curve
smoothness to validate/override Open-Meteo clear-sky windows*](https://github.com/JimboHamez/ha_solcast_solar_enhanced/issues/42).
No code. Relates to the Kt clear-sky gate (`sqlite_store.async_get_records_for_tuning` /
`async_get_records_for_dampening`, `pv_tuning.clearsky_ghi`) and to the seasonal
clear-sky-reference-quality work paused until spring 2026.

> **One-line scope.** Reframe the reporter's *curve-volatility* proposal as a **broken-cloud
> rejecter** (which it is) rather than the uniform-mist fix it's pitched as, and propose a
> physics-anchored **shape + amplitude self-clearness** cross-check that targets both failure
> modes. Optional, default-off, gates *tuning reference selection only* — never the dampening
> ratio.

## 1. Problem (as reported, validated)

BOM's ACCESS **APS4** upgrade restricted free high-resolution gridded NWP via the NCI to
commercial subscribers, so public aggregators (Open-Meteo) lost their native ACCESS feed for
Australia and fell back to coarser global models (GFS/ECMWF) that miss localized coastal
moisture (marine mist, morning fog, coastal humidity). Because our clear-sky selection leans on
Open-Meteo GHI, a bad "clear" reference feeds a depressed-but-labelled-clear slot into:

- **Tilt tuning** (`pv_tuning.run_tuning`) — MAE grid search; a false-clear slot pulls the fitted
  tilt/scale.
- **Shading dampening** (`shading_dampening.compute_dampening`) — the quality-weighted
  actual/forecast ratio reads the array as shaded/dirty, which is the "over-predicts later"
  failure the reporter describes.

The existing **Kt gate** (`Kt = ghi / clearsky_ghi(zenith) ≥ 0.75`, per slot) is the defense; the
request is really *"the Kt reference (Open-Meteo GHI) is no longer trustworthy for AU coastal, add
a generation-based cross-check."*

## 2. The drafted reply

> Thanks for such a well-researched write-up — the APS4 → NCI restriction → Open-Meteo losing its
> native ACCESS feed for Australia is a real regression, and you've traced the failure path
> correctly. Since our clear-sky selection leans on Open-Meteo GHI, a bad "clear" reference does
> exactly what you describe: it feeds a depressed-but-labelled-clear day into the tilt tuner and
> the shading-dampening ratio, which then reads the array as shaded/dirty and over-predicts later.
> So the motivation is spot on.
>
> I want to be upfront about one thing before committing to the *curve-volatility* form
> specifically, because I think it changes the design.
>
> **Volatility is a strong _broken-cloud_ rejecter — but it's blind to the headline mist case.**
> Your opening scenario is a *uniform* marine mist layer that "reflects sunlight smoothly."
> Physically that produces a smooth, bell-shaped, just-**scaled-down** curve — its first-derivative
> variance is *low*, so a volatility gate reads it as "smooth = clear = keep." The metric shines on
> *passing/broken* cloud and variable burn-off mornings (jagged profiles, sharp rate-of-change
> spikes) — which AU coastal mornings often are, so it'd still catch a good fraction — but the
> perfectly-uniform-haze tail slips straight through.
>
> There's also a deeper limit worth naming: a smooth-but-depressed day is **fundamentally
> ambiguous with panel soiling or fixed shading** when generation is your only sensor. Thin uniform
> mist and dirty panels both give a smooth curve at reduced amplitude, and no derivative-variance
> math separates them — only an independent irradiance measurement can, which is the very thing
> that degraded.
>
> **Where I'd take it instead.** We already store solar `zenith` per half-hour and compute a
> pure-physics clear-sky reference `clearsky_ghi(zenith)` (Haurwitz). So rather than raw volatility,
> I'd anchor to that with two arms:
> 1. **Shape correlation** between measured generation and `clearsky_ghi(zenith)` across the day —
>    catches broken cloud like volatility does, but physics-anchored rather than threshold-guessed.
> 2. **Amplitude / ceiling check** — measured peak vs clear-sky-expected peak — which is what
>    actually flags the smooth-mist *amplitude deficit* that volatility misses.
>
> That pair covers both failure modes. (The amplitude arm still can't beat the mist-vs-soiling
> ambiguity in absolute terms, but for *relative* day-ranking — reject the day sitting well below
> your own recent clear-day ceiling — it's serviceable.)
>
> A few implementation notes on your example so you know where the edges are:
> - Our clear-sky gate runs **per half-hour slot in SQL**, not per day, so this becomes a new
>   day-grouping pass rather than a drop-in filter.
> - The fixed `index > 2.5` cutoff won't generalise — it's sensitive to system size, half-hourly
>   (not hourly) sampling, and especially **inverter clipping / export-limit flat-tops** (a clipped
>   clear day has a near-zero-derivative midday that reads as ultra-smooth). Any threshold would
>   need to be **self-calibrating per site** (e.g. a percentile of that site's own recent daily
>   scores).
> - Critically, this can only gate the **clear-sky reference selection for tuning** — *not* the
>   dampening ratio, which is *supposed* to see real shading. Otherwise we'd reject the very morning
>   slots that carry the shading signal.
>
> So: I'm keen to build this, scoped as an **optional, default-off generation clear-sky
> cross-check** that vetoes Kt-passed slots for tuning, using the shape+amplitude form with a
> per-site threshold. One honest caveat on timing — validating any such threshold needs a stretch
> of real *clear-and-misty* AU days to tune against, which the current winter won't provide; I can
> build the scaffolding now but wouldn't trust the cutoffs until spring.
>
> Does the shape+amplitude framing match what you're seeing on your site — i.e. is your
> bad-tracking mostly *variable* mist/cloud (jagged) or *uniform* haze (smooth-but-low)? That tells
> me which arm is doing the heavy lifting for your install.

## 3. Key technical points behind the reply

- **Curve-volatility blind spot.** Uniform mist → smooth scaled-down curve → low first-derivative
  variance → passes a volatility gate. Volatility rejects *broken/variable* cloud, not *uniform*
  haze (the reporter's headline case).
- **Irreducible ambiguity.** Smooth-but-depressed ≡ thin uniform mist ≈ soiling/fixed shading, with
  generation as the only sensor. Absolute disambiguation needs an independent irradiance source —
  the degraded input. Only *relative* day-ranking is recoverable.
- **Better signal (shape + amplitude).** Anchor to the physics reference we already have
  (`clearsky_ghi(zenith)`): shape-correlation catches broken cloud; amplitude/ceiling deficit flags
  smooth mist. Together they cover both modes.

## 4. Integration constraints

1. **Granularity.** Gates today are per-slot in SQL (`async_get_records_for_tuning` /
   `async_get_records_for_dampening`); a volatility/shape score is per-day → a new grouping pass,
   not a `WHERE` clause.
2. **No magic threshold.** `index > 2.5` is per-site in disguise (system size, half-hourly
   sampling, clipping/export-limit flat-tops). Must be data-derived (per-site percentile).
3. **Protect the shading measurement.** Gate **tuning reference selection only**. The dampening
   ratio is *meant* to see real per-slot shading (e.g. the known MPPT2 morning shade); rejecting
   jagged mornings there would discard the signal we want.

## 5. Proposed scope

Optional, **default-off** "generation clear-sky cross-check": vetoes Kt-passed slots for the
**tuning** reference set only, using shape-correlation + amplitude-deficit against
`clearsky_ghi(zenith)`, with a **self-calibrating per-site threshold**. Build scaffolding now
(`curve_clearness.py` against the existing DB); defer threshold tuning/validation to **spring 2026**
when clear-and-misty AU days are available.

## 6. Open decisions

- Post the §2 reply as-is, or trim for length first.
- Build the `curve_clearness.py` prototype now (scaffold + offline evaluation on the existing DB),
  or hold entirely until spring data exists.
- Volatility-only (broken-cloud rejecter) vs the fuller shape+amplitude self-clearness — pending the
  reporter's answer on whether their bad days are jagged or smooth-but-low.
