# v1.10.0b10 — live verification checklist

Everything in b10 is quality-scale housekeeping: **no change to tuning, dampening or storage**. A forecast corrected by b10 is corrected identically to b9. What *did* change is how the integration is wired into Home Assistant — and those are exactly the parts a test suite has to mock, so they need confirming on a real instance.

The automated suite (568 tests, `mypy --strict`, ruff, 96% coverage with the config flow at 100%) has been run and is green. It was run against **Home Assistant 2026.2.3** in the dev venv, while the manifest requires **2026.5.4** — so passing tests are weaker evidence than usual here.

Nine changes carry real risk on a live box:

| Change | If it's wrong, you'd see |
|---|---|
| Coordinator moved to `entry.runtime_data` | The entry fails to set up at all |
| Actions register in `async_setup` | Actions missing, or erroring when they shouldn't |
| Base entity classes moved to `entity.py` | Entities on the wrong device, or restore broken |
| Icons moved to `icons.json` | Blank or generic icons |
| Weather / MPPT sensors made conditionally unavailable | A sensor reading `unavailable` that should be showing a number |
| `fetch_weather` rewritten to cover Open-Meteo | The action erroring, or no longer updating Cloud Cover |
| **New diagnostics download** | The download failing, or a secret surviving redaction |
| **New reconfigure flow** | Settings not prefilled, or changes not taking effect |
| **Stale per-array device cleanup runs at every setup** | A device you still use quietly disappearing |

The last three landed after this checklist was first written; they are the newest and least exercised code in the release.

---

## Install

b10 is now tagged as a **pre-release**, so HACS will offer it directly — no manual copying:

1. HACS → **Solcast Solar Enhanced** → ⋮ → **Redownload**
2. Enable **Show beta versions**
3. Pick **v1.10.0b10**, then restart Home Assistant

> **If you install manually instead, replace the whole directory rather than merging into it.** b10 adds three new files (`entity.py`, `icons.json`, `diagnostics.py`). Copying over the top of a b9 tree leaves a half-old mix that won't import.

**Rollback:** reinstall b9 via HACS. Your database (`solcast_solar_enhanced.db`) and all entity IDs are untouched by this release — no schema change, no unique-ID change — so a rollback loses nothing.

---

## Record this first

- [ ] **Home Assistant version:** `________________`

This is the cheapest useful data point even if everything else passes, because the suite has only ever run against 2026.2.3.

---

## 1. Does it load at all?

The single riskiest change. If `entry.runtime_data` is wrong the entry won't set up, and nothing below is worth trying.

- [ ] Settings → Devices & Services → **Solcast Solar Enhanced** is present
- [ ] No "Failed to set up" / "Retrying setup" banner
- [ ] Entities are listed and carrying values (or `unknown` if you're mid-cycle — the coordinator only produces data on the half-hour grid)

## 2. Reload

Exercises unload → teardown → re-setup through the new wiring.

- [ ] ⋮ → **Reload** completes without error
- [ ] Settings → System → Logs shows nothing new from `solcast_solar_enhanced`

## 3. The three actions

Developer Tools → **Actions**, search `solcast_solar_enhanced`.

- [ ] All three appear: `run_pv_tuning`, `run_dampening_update`, `fetch_weather`
- [ ] Each shows an icon — 🔧 `mdi:tune`, ⛅ `mdi:weather-partly-cloudy`, ☁️ `mdi:cloud-download` (new in b10)
- [ ] `run_dampening_update` runs and behaves as it did on b9

Then the genuinely new behaviour:

- [ ] **Disable** the integration entry (⋮ → Disable), then call any of the three actions
- [ ] You get an error dialog reading:

  > Solcast Solar Enhanced is not loaded, so this action cannot run. Check that the integration is enabled and has started successfully.

- [ ] **Re-enable** the entry afterwards

On b9 that same call did nothing whatsoever, with no feedback. Silence was the bug.

## 4. `fetch_weather` on the default (keyless) setup

This is the change most likely to show up as a regression, because on b9 the action was a no-op here and a no-op never fails. Your install has **Open-Meteo on, OWM off**, which is exactly the case b9 didn't handle.

- [ ] Note the current state and `last_changed` of **Cloud Cover** and **Weather Temperature**
- [ ] Developer Tools → Actions → run `solcast_solar_enhanced.fetch_weather`
- [ ] It completes **without** an error dialog
- [ ] Cloud Cover / Weather Temperature update — `last_changed` moves, or the value does

On b9 nothing would have moved. If you now get an error instead, that's the new `ServiceValidationError` firing when it shouldn't — send the message, it names which source it thought was missing.

## 5. Sensors that should read *unavailable*

New in b10: a sensor with no data source reads `unavailable` rather than sitting blank forever. The risk is over-reach — something reading `unavailable` that should be showing a number.

- [ ] **MPPT DC Voltage** — you have DC sensors mapped, so this should show a **voltage**, not `unavailable`
- [ ] **Weather Temperature** and **Cloud Cover** — Open-Meteo is on, so both should show **numbers**
- [ ] Neither array's **Site Output** or **Tuned Tilt** reads `unavailable` (these were deliberately left alone — they show `unknown` before their first value)
- [ ] **Tuned Tilt**, if it has no value, still shows its attributes (`unidentified_tilt`, the reason, the fit error) in Developer Tools → States

That last one is the point of the exception: an unavailable entity has no attributes in the UI, and on this install the attributes are the useful part.

## 6. The OpenWeatherMap key test

You can exercise this fully **without owning an OWM key**.

⋮ → **Configure** → click through to **Weather & Irradiance**.

- [ ] Tick **Enable OpenWeatherMap**, leave the key blank, submit → step is refused with:

  > OpenWeatherMap is enabled but no API key was entered. Enter a key, or turn OpenWeatherMap off and let Open-Meteo supply cloud cover.

- [ ] Type any garbage into the key, submit → step is refused with:

  > OpenWeatherMap rejected this API key. Check it for typos — a newly created key can also take a couple of hours to activate.

- [ ] The values you typed are still in the form after each refusal (not reset to defaults)
- [ ] **Untick OpenWeatherMap**, finish the wizard

> ⚠️ Make sure you finish with OWM **unticked**, or you'll save it enabled with a junk key. Open-Meteo supplies cloud cover on its own.

If instead you see *"Could not reach OpenWeatherMap"*, that's the network-failure path rather than the auth path — worth telling me, since it means the HTTP status didn't come back as expected.

## 7. Restart, and check the restoring sensors

The restore path moved into `entity.py`. The move shouldn't have changed it, which is exactly why it's worth checking — this is the kind of thing that breaks quietly.

- [ ] Restart Home Assistant fully
- [ ] `PV Power 30min Average` shows its previous value, not `unknown`
- [ ] `PV Export 30min Average` likewise

(Battery Charge 30min Average uses the same path, but there's no home battery here to test it with.)

## 8. The diagnostics download (new)

Brand new in this release, and the one item where a bug leaks data rather than just breaking a feature.

Settings → Devices & Services → **Solcast Solar Enhanced** → ⋮ → **Download diagnostics**.

- [ ] The file downloads without an error
- [ ] Open it and search for your **OpenWeatherMap API key** (if you have ever entered one) — it must appear as `**REDACTED**`, never in the clear
- [ ] Search for your **latitude/longitude** — both must read `**REDACTED**`
- [ ] `dampening.slots` holds **48** entries, not 24 — the half-hour grid, not the hourly one
- [ ] `coordinator.configured_sites` lists **both** your arrays, with the names you gave them
- [ ] `storage.record_count` roughly matches the **Database Records** sensor

The redaction is asserted in the test suite against the whole serialised payload, so a leak here would mean something reached the file by a path the tests don't model. Worth two minutes of searching.

## 9. The reconfigure flow (new)

⋮ on the integration entry — there should now be a **Reconfigure** item alongside Configure.

- [ ] **Reconfigure** appears in the menu
- [ ] Step 1 opens **prefilled with your current settings** — tilt, capacity, your generation/export sensors — not blank defaults
- [ ] Click through every step without changing anything and finish
- [ ] The entry reloads and your entities still carry their values
- [ ] Entity IDs are unchanged (see below)

Then confirm a change actually lands, which is the part with a real trap behind it:

- [ ] Reconfigure again, change **Panel Tilt** to something obviously different
- [ ] Finish the wizard, then check Developer Tools → States that the change took effect
- [ ] **Reconfigure once more and set it back**

If the tilt reverts to the old value, that's the shadowing bug the flow was written to avoid — settings live in both `data` and `options`, and a stale `options` value winning would make the whole flow a silent no-op. It's covered by a test, but this is the live confirmation.

## 10. Nothing disappeared

The stale-device cleanup now runs on **every** setup. On your install it should do nothing at all — both arrays are configured, so both are "live".

- [ ] Both array devices are still present after all the reloads and restarts above
- [ ] Neither array's entities have gone missing
- [ ] Logs show no `Removing stale array device` line

That log line appearing would mean the cleanup misjudged a live array — the one way this change can bite. Don't test it by deleting an array from your config; there's nothing to gain and it would cost you the array's history in the entity registry.

---

## While you're in there

- [ ] Both array devices are still present, still grouped as before, nested under the main integration device
- [ ] **Entity IDs are unchanged** — this is the assertion most worth confirming rather than assuming. Existing dashboards and automations should be untouched
- [ ] Sensor names read the same in English (they're now supplied by translation rather than hardcoded — English output should be identical)
- [ ] Sensor icons look unchanged (a malformed `icons.json` would show blank or generic icons)

> **Non-English installs only:** entity IDs are now built from the *translated* name. Existing entities are unaffected — their IDs live in the registry — but a *fresh* install in another language gets localized IDs. Not applicable here, noted for completeness.

---

## If something fails

Send the log lines around the failure rather than just the symptom:

**Settings → System → Logs → Load Full Logs**, or `config/home-assistant.log`, filtered to `solcast_solar_enhanced`.

The error text usually identifies which of the changes caused it:

| Symptom in the log | Likely cause |
|---|---|
| `AttributeError: … runtime_data` | The `entry.runtime_data` move |
| `ServiceNotFound`, or actions missing entirely | The `async_setup` registration move |
| `ImportError` / `ModuleNotFoundError` naming `entity` | Half-copied install — replace the whole directory |
| Icons blank or generic | `icons.json` malformed or not copied |
| A weather/MPPT sensor stuck on `unavailable` | The new `available` overrides reading the coordinator payload wrongly |
| `fetch_weather` raising "No weather source is enabled" | The action's source check, when Open-Meteo *is* on |
| Diagnostics download failing | `diagnostics_snapshot()` hitting state the tests don't produce |
| A reconfigured value reverting | A stale `options` entry shadowing `data` |
| `Removing stale array device` for a live array | The device cleanup misreading the configured site list |

---

## Notes

_Anything unexpected, however minor:_

```




```
