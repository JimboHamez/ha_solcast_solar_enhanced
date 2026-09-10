# Solcast Solar Enhanced — 1.11.0 beta: your roof gets a shading map, plus a HACS submission

> Draft update post for the Home Assistant Community forum.
> Copy everything below the line into a reply on the existing topic
> (or a new one under Share your Projects / Custom Integrations).

---

Hi all 👋

Quick update on **[Solcast Solar Enhanced](https://github.com/JimboHamez/ha_solcast_solar_enhanced)** — the companion integration for [BJReplay's ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) that keeps your own actual-vs-forecast history, tunes your panel geometry from it, and pushes a measured shading correction back to Solcast. It's an **add-on, not a replacement**, and it makes **zero extra Solcast API calls** — it reads the forecast the base integration already fetched. All credit to BJReplay for the foundation. 🙏

The **1.11.0 beta line** has been rolling out over the last week and there's a fair bit in it.

## 🗺️ The headline: a shading map of your roof (b3)

Until now, the integration measured shading by asking *"how much less than forecast does this hour usually produce?"* — and that question is asked in the wrong units.

Shading depends on **where the sun is**. But an hour on the clock is a completely different sun position in July than in December, so a real morning shadow gets averaged away across the season. On my own system the measured morning loss was 30–59% while the correction actually being applied was 0–3%.

The new **Shading Loss (Measured)** sensor asks the right question. Using the irradiance and output already sitting in your database, it works out how much *direct sun* each array is losing and **from which part of the sky** — reporting a compass bearing and a sun elevation. You get one for the whole property and one per array.

It also tells you **what kind of shadow it is**, from the per-tracker DC figures:

- A shadow lying **evenly across the panels** drops the current while the voltage holds steady, and loses power roughly in proportion to the area covered.
- One that trips the panels' **bypass diodes** drops the voltage too, and doesn't behave proportionally at all.

Only the first kind can be described by a simple correction factor, so the sensor tells you which you've got.

**This is read-only.** Nothing here is sent to Solcast and your dampening factors are untouched. It's a diagnostic — the point is that you can *see* what your roof is doing before anything acts on it.

Two limits, both reported rather than hidden: below about 15° of sun elevation it can't cleanly separate "the sun is blocked" from "the sky is blocked", so results down there are flagged uncertain; and shading that dims *every* array equally is invisible to it, because there's nothing left to compare against.

## 🔌 One combined meter? There's now an option for that (b4)

If your system reports all its solar through a **single sensor** with nothing per array — a **Tesla Powerwall 3** being the clearest case, since Tesla removed per-string data from every consumer API — the setup wizard previously had no option that fitted, and you simply couldn't get past the per-array page.

There's now a third choice: **"one combined meter, no per-array data."** It asks for nothing else and tracks your property as a single system.

You lose less than that sounds like. Dampening is worked out **per hour**, so with an east array and a west array, the east one dominates your morning total and the west one your afternoon — a morning shading loss still lands in the morning hours of the correction. What a single meter genuinely can't give you is the per-array sensors and per-array tilt tuning.

**⚠️ Worth checking if you have a multi-array system.** The same release fixed a bug that could quietly switch your dampening off. If you'd mapped the *same* generation sensor to two arrays — easy to do, because that field used to come pre-filled with it — each array recorded the **whole property's** output. Every per-array ratio then read roughly double, and since a factor above 1 gets clamped back to 1, the result was **no dampening at all**. Nothing warned you. It's now a clear error, and the field isn't pre-filled where that suggestion was the trap. Open **Configure** and have a look at the per-array page.

## 🔧 Also in the beta line

- **b1 — an array's DC share can span several MPPTs.** Reported by a Sigenergy owner running 4 MPPTs grouped into 2 Solcast sites. The DC field is now a multi-entity picker; select all of an array's trackers and they're summed. Only the *ratio* is used, so this is exact, not an approximation — and it replaces a template sensor per array.
- **b2 — better DC data collection.** The per-tracker current column stored the interval *minimum* (deliberately, to catch inverter throttling), which turns out to be useless for measuring shade — one passing cloud pins the whole half hour near zero, worst at low sun. On a 74-day store that left **56 of 67 mornings** with no usable 08:00 reading. There's now a median alongside it. Forward-only, since a minimum can't be turned back into a median after the fact.
- **b5 — documentation and tooling.** No functional change. Two sensors were named inexactly in the docs; both are fixed and now enforced by tests. Also adds an offline tool for anyone with **two arrays at the same tilt and azimuth**, which measures shading by comparing the arrays against *each other* rather than against the forecast — that cancels the forecast's own error out of the measurement entirely.

## 📦 HACS submission

The integration has been **submitted to the HACS default repository** ([hacs/default#10336](https://github.com/hacs/default/pull/10336), in the queue since late August). If it's accepted it'll be installable directly from HACS without adding a custom repository first.

**Please don't comment on or react to that PR** — HACS explicitly ask people not to, it doesn't speed anything up, and it makes more work for volunteer reviewers. It's processed oldest-first and it'll get there. 🙂

**In the meantime, installing is still easy:**

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/JimboHamez/ha_solcast_solar_enhanced` as an **Integration**
3. Install, restart, then add it from **Settings → Devices & services**

For the 1.11.0 betas, enable **"show beta versions"** on the integration in HACS. Or stay on **v1.10.3** if you'd rather have the stable line — the betas are all additive and read-only where it counts, but they're betas.

## ✨ Why you might want it

- **📡 No extra Solcast API calls.** It reads what the base integration already fetched. Your poll budget is untouched.
- **🗄️ History with no setup.** Built-in SQLite in your HA config folder — no database server, no add-on, nothing to configure. It just starts recording.
- **📐 It works out your real tilt.** From clear-sky history, via a physical irradiance model. And when the data *can't* determine a tilt, it says so and reports nothing rather than handing you a confident wrong number to paste into Solcast. (Azimuth is deliberately **not** tuned — it isn't recoverable from this data, so it's reported back as configured rather than guessed.)
- **🌥️ Dampening that earns its confidence.** It starts as a complete no-op and ramps toward the measured correction as evidence accumulates — it won't do anything drastic on a fortnight of winter data.
- **⚡ Curtailment-aware.** If your inverter is export-limited, that throttled output isn't mistaken for shading and dampened away.
- **🍓 Raspberry Pi friendly.** numpy only — **no scipy**, which has no Pi wheel and fails to build under HA. numpy already ships with Home Assistant, so there's nothing extra to install.
- **🔑 No API key needed for irradiance.** Open-Meteo is keyless and on by default. OpenWeatherMap is optional, and if you do enable it the setup wizard tests your key before storing it.
- **🏠 Proper multi-array support.** Arrays auto-discovered from Solcast, each on its own device, with per-array shading, tilt and dampening where your hardware can support it.
- **🌍 Eleven languages**, and every sensor name is translated rather than hard-coded to English.

## 🙏 Feedback wanted

Two things I genuinely can't test myself:

- **Powerwall 3 owners** — the new combined-meter option was built from a user's description of what those integrations expose. I don't have one. If you try it, I'd love to know whether it goes through cleanly.
- **Anyone with heavy morning or afternoon shading** — the shading map is the first release of a fairly involved bit of physics. It's read-only so it can't hurt your forecast, but I'd like to know whether the bearing and elevation it reports match what you can actually see from your roof.

Issues and questions: [GitHub](https://github.com/JimboHamez/ha_solcast_solar_enhanced/issues) or reply here. 🌞
