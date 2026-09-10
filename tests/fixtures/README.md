# Test fixtures

## `reference_ha.db`

Home Assistant recorder reference database (pre-existing).

## `topology_*.db` — one store per supported measurement topology

> **Local-only, not in the repository.** These are derived from a real store and
> carry a genuine solar-position series, which fixes the site's latitude and
> longitude, plus 28 days of real generation. The global `*.db` rule in
> `.gitignore` keeps them out of git. **Generate them yourself:**
>
> ```
> python tools/build_test_fixtures.py --db <your solcast_solar_enhanced.db>
> ```
>
> The generator needs a **dc_split source** — a store with two per-array sites —
> since the multi-array fixtures are derived by reduction from one. It exits with
> a message if the source has fewer than two.
>
> (`reference_ha.db` alongside them *is* tracked; it predates that ignore rule.)

Four `solcast_data` stores, one per way the integration can be told a property is
measured.

| Fixture | Rows written | DC columns | Represents |
|---|---|---|---|
| `topology_single_array.db` | `_total` only | populated | One array, one inverter, flat MPPT sensors on step 1 |
| `topology_direct.db` | `_total` + one per site | empty | Microinverters, or one inverter per array |
| `topology_dc_split.db` | `_total` + one per site | populated per site | One shared inverter, apportioned by DC share |
| `topology_shared_no_dc.db` | `_total` only | empty | One combined meter, no per-array telemetry (e.g. Powerwall 3) |

`single_array` and `shared_no_dc` have the same *shape* — `shared_no_dc` derives an
empty group list, so only `_total` is written. They differ in whether any DC
telemetry exists, which is the distinction the readers care about.

### Provenance

Derived from a real 28-day dc_split store (two co-oriented arrays, 24.75° tilt,
Solcast azimuth 7, 8 kW), so distributions, gaps, cloud and irradiance are genuine
rather than synthesised. Each is built to be physically self-consistent for its
topology:

- **`dc_split`** is the source, trimmed to the window. No transformation.
- **`direct`** keeps the same rows with DC cleared. A direct install measures each
  array on its own AC sensor: the per-array magnitudes are unchanged, and there is
  no shared inverter to apportion, so the DC telemetry that drove the
  apportionment does not exist.
- **`single_array`** is built from **one array alone**, relabelled `_total`, so
  output and DC describe the same physical array rather than pairing one tracker
  with two arrays' output.
- **`shared_no_dc`** keeps the real property-wide `_total` — both arrays summed,
  which is what one combined meter reads — with DC cleared.

Schema fidelity comes from copying the source file and deleting rows, so each
fixture carries the exact column set, indexes and `PRAGMA user_version` that
`SqliteStore` creates. No fixture is hand-written, and none contains invented
readings.

### They discriminate

Running the shipped analysis over them gives materially different results, which is
the point of having four rather than one:

```
topology_single_array    shading  7.5%  cells=30  mechanism=uniform
topology_shared_no_dc    shading  5.7%  cells=32  mechanism=undetermined
topology_direct          shading  5.7%  cells=32  mechanism=undetermined
topology_dc_split        both arrays fit separately; differential fit runs
```

`classify_mechanism` returning `undetermined` exactly where the DC columns are
empty is correct behaviour that previously had no fixture exercising it.

### Caveats

- **Nothing consumes these yet.** They are fixtures looking for tests, not a
  regression suite. Any test written against them must skip cleanly when they are
  absent, since they are not in the repository — see `tests/test_sites_fixtures.py`,
  which is itself gitignored for depending on local data.
- `tilt_identifiable` is `false` on every one of them. That is not a defect in the
  fixtures — it is the known tilt/capacity-scale degeneracy, faithfully preserved.
- The window is 28 days, matching `DAMPENING_WINDOW_BACK_DAYS`, which gives ~614
  daylight rows per site — above `SHADING_MIN_RECORDS` (200) but not by a wide
  margin. Regenerate with `--days` for more.
- `battery_charge` is zero throughout: the source system has no battery. These
  cannot exercise anything battery-related (see issues #85, #86).
