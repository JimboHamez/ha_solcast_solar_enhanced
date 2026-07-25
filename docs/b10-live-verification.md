# v1.10.0b10 — live verification checklist

Everything in b10 is quality-scale housekeeping: **no change to tuning, dampening or storage**. A forecast corrected by b10 is corrected identically to b9. What *did* change is how the integration is wired into Home Assistant — and those are exactly the parts a test suite has to mock, so they need confirming on a real instance.

The automated suite (506 tests, `mypy --strict`, ruff, 100% config-flow coverage) has been run and is green. It was run against **Home Assistant 2026.2.3** in the dev venv, while the manifest requires **2026.5.4** — so passing tests are weaker evidence than usual here.

Four changes carry real risk on a live box:

| Change | If it's wrong, you'd see |
|---|---|
| Coordinator moved to `entry.runtime_data` | The entry fails to set up at all |
| Actions register in `async_setup` | Actions missing, or erroring when they shouldn't |
| Base entity classes moved to `entity.py` | Entities on the wrong device, or restore broken |
| Icons moved to `icons.json` | Blank or generic icons |

---

## Install

No tag yet, so HACS won't offer this — copy it in manually:

```
https://github.com/JimboHamez/ha_solcast_solar_enhanced/archive/refs/heads/release/1.10.0b10.zip
```

Replace the whole of `config/custom_components/solcast_solar_enhanced/` with the copy from the zip, then restart Home Assistant.

> **Replace the directory, don't merge into it.** b10 adds two new files (`entity.py`, `icons.json`). Copying files over the top of a b9 tree leaves a half-old mix that won't import.

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

## 4. The OpenWeatherMap key test

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

## 5. Restart, and check the restoring sensors

The restore path moved into `entity.py`. The move shouldn't have changed it, which is exactly why it's worth checking — this is the kind of thing that breaks quietly.

- [ ] Restart Home Assistant fully
- [ ] `PV Power 30min Average` shows its previous value, not `unknown`
- [ ] `PV Export 30min Average` likewise

(Battery Charge 30min Average uses the same path, but there's no home battery here to test it with.)

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

The error text usually identifies which of the four changes caused it:

| Symptom in the log | Likely cause |
|---|---|
| `AttributeError: … runtime_data` | The `entry.runtime_data` move |
| `ServiceNotFound`, or actions missing entirely | The `async_setup` registration move |
| `ImportError` / `ModuleNotFoundError` naming `entity` | Half-copied install — replace the whole directory |
| Icons blank or generic | `icons.json` malformed or not copied |

---

## Notes

_Anything unexpected, however minor:_

```




```
