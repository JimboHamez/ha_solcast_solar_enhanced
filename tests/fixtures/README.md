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

### `--seasonal`: the band the dampening query actually reads

```
python tools/build_test_fixtures.py --seasonal [--target-date YYYY-MM-DD]
```

Writes `seasonal_*.db` instead of `topology_*.db`. Rather than a trailing window,
it keeps the **day-of-year band across every year present** — `-28/+14` days
around the target, the same `DAMPENING_WINDOW_*` bounds
`async_get_records_for_dampening` uses. The day-of-year predicate is copied
verbatim from `sqlite_store.py`, including the `+548` wrap bias, so the fixture
selects exactly the rows the query would; a fixture built on an approximation
would silently include or drop the boundary rows that matter most.

The target defaults to the newest date in the source. `--target-date 2026-07-15`
against a store ending in September yields a **22 Jun – 29 Jul** band, not the
recent data — day-of-year, not recency.

**Why it matters, and when.** The forward half of that window can only ever be
filled from a *previous* year, so on a single-year store it is empty — which is
the correct year-one behaviour, and why the generator prints a note saying so.
Once the source spans 13+ months, `--seasonal` produces one band per year and
becomes the first fixture able to exercise:

- the **year-2 seasonal path**, where the window is populated on both sides
- the **New Year day-of-year wrap**, only reachable with data either side of 1 Jan

At `-28/+14` per year that is ~42 days of data per year rather than a full year,
so a two-year seasonal fixture stays small.

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
- **Forward-only columns stratify with age.** `pv_estimate_undampened` exists only
  from 1.10.0b6 and `dc_vmed*`/`dc_imed*` only from 1.11.0b2, so the further back a
  window reaches the more of them are zero. That is faithful — every upgraded store
  looks like this, and it exercises the fallback paths — but a longer window buys
  more rows, not more capability, for the features that depend on them. Only
  `ghi`/`dni`/`dhi` can be backfilled (`tools/backfill_irradiance.py`).
- **Retention bounds what can ever be rebuilt.** These are derived, not archival. If
  `CONF_DB_RETENTION_DAYS` is set below `DB_RETENTION_MIN_RECOMMENDED_DAYS` (400) the
  source prunes and no regeneration recovers that history — so leave retention at the
  default `0` if a multi-year seasonal fixture is wanted.
