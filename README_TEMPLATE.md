# README template

The canonical section order and contents for `README.md`. Keep `README.md` in this
shape so releases and feature additions land in predictable places. Sections are
listed top-to-bottom in the order they must appear. **Required** sections always
exist; **Optional** sections appear only when relevant (and keep their slot in the
order when they do).

Separate every top-level section with a `---` horizontal rule, as the current
README does.

---

## Section order at a glance

| # | Section | Heading | Required? |
|---|---|---|---|
| 1 | Title, badges & elevator pitch | `# <Name>` | Required |
| 2 | Why this exists | `## Why this exists` | Required |
| 3 | What's new | `## 🆕 What's new in vX.Y.Z` | Required |
| 4 | Prerequisites | `## Prerequisites` | Required |
| 5 | Installation | `## Installation` | Required |
| 6 | Configuration | `## Configuration` | Required |
| 7 | How it works | `## How it works` | Required |
| 8 | Sensors | `## Sensors` | Required |
| 9 | Services | `## Services` | Required |
| 10 | Standalone tools | `## Standalone tools` | Optional |
| 11 | Roadmap | `## Roadmap` | Optional |
| 12 | Compatibility | `## Compatibility` | Required |
| 13 | License | `## License` | Required |

---

## 1. Title, badges & elevator pitch — *Required*

- `# <Integration name>` as the H1.
- **Badge block**, two rows, in this order:
  1. Status/meta shields (`style=for-the-badge`): HACS, GitHub Release, downloads,
     license, commit activity, maintenance year.
  2. Workflow status badges: Tests, Validate, Security.
- One-sentence **value proposition** linking the base integration.
- An **`It adds:`** bulleted feature list — each bullet **bold lead-in** + plain-English
  benefit (History storage, Automatic panel tuning, Adaptive dampening, Multi-site,
  Flexible inputs, Curtailment-aware…).
- A closing **call-out** for the headline differentiator (e.g. "No extra Solcast API calls").

## 2. Why this exists — *Required*

The motivation / problem statement. Two or three short paragraphs: what changed in the
world (e.g. Solcast discontinued free PV Tuning), what this restores, and why the result
can beat the thing it replaces. No tables, no config detail.

## 3. What's new in vX.Y.Z — *Required*

- Heading carries the **current release version** and the 🆕 emoji.
- 1–2 paragraphs in plain language describing the headline change for **this** release only.
- Include an **"Upgrading?"** note when behaviour or migration affects existing users.
- Close with links: `[CHANGELOG](CHANGELOG.md)` · `[release notes](…/releases/tag/vX.Y.Z)`.
- **Update this every release** (see the release-doc-sync rule) — version in the heading,
  body, and the release-notes link must all match the new tag.

## 4. Prerequisites — *Required*

Numbered `### N. <topic>` subsections covering everything needed before install, in
dependency order. For this project:

1. **Base integration** — the hard dependency and any one-property/one-DB limits.
2. **Generation / export sensors** — accepted input kinds (energy counter preferred,
   power helper fallback), the "don't use a raw instantaneous sensor" ⚠️ warning, and a
   collapsible `<details>` block with the `mean_linear` helper YAML.
3. **History storage** — what it powers and that it's zero-config / on by default.
4. **Weather & irradiance** — the keyless default source (Open-Meteo), what it supplies,
   the optional legacy alternative (OWM), and how to verify it's working.

Use `<details><summary>…</summary>` for long optional snippets so the section stays scannable.

## 5. Installation — *Required*

- Dashboard screenshot up top:

  ```markdown
  ![Solcast Solar Enhanced sensors in Home Assistant](images/dashboard.png)
  ```

- `### HACS (recommended)` numbered steps.
- `### Manual` numbered steps.
- A closing note on runtime deps (stdlib storage, numpy ships with HA).

## 6. Configuration — *Required*

- Entry-point sentence (Settings → Devices & Services → Add Integration → …).
- A line stating how many wizard steps there are and which are conditional.
- One `### Step N — <name>` subsection per wizard step. **Each step leads with a
  screenshot of that wizard page**, then a **field table**
  (`| Field | Default | Description |`, drop the Default column where it doesn't apply).
- Mark conditional steps in the heading (e.g. "(multi-site only)").
- End with any operational ⚠️/heads-up call-outs (e.g. disable the base's auto-dampening).

Keep step numbers and field names in lockstep with `config_flow.py`/`strings.json`.

Per-step screenshot placeholders — one image per wizard step, named `config-stepN-<slug>.png`
under `images/`. Swap each placeholder for a real screenshot of that page:

```markdown
### Step 1 — Site & System
![Step 1 — Site & System](images/config-step1-site.png)
<!-- placeholder: capture the Site & System wizard page -->

### Step 2 — Storage
![Step 2 — Storage](images/config-step2-storage.png)
<!-- placeholder: capture the Storage wizard page -->

### Step 3 — Weather & Irradiance
![Step 3 — Weather & Irradiance](images/config-step3-weather.png)
<!-- placeholder: capture the Weather & Irradiance wizard page -->

### Step 4 — Battery Storage
![Step 4 — Battery Storage](images/config-step4-battery.png)
<!-- placeholder: capture the Battery Storage wizard page -->

### Step 5 — PV Tuning & Dampening
![Step 5 — PV Tuning & Dampening](images/config-step5-tuning.png)
<!-- placeholder: capture the PV Tuning & Dampening wizard page -->

### Step 6 — Per-site sensor mapping (multi-site only)
![Step 6 — Per-site sensor mapping](images/config-step6-sites.png)
<!-- placeholder: capture the Per-site mapping page (multi-site setups only) -->
```

## 7. How it works — *Required*

- Bulleted explanation of each core behaviour (PV tuning, adaptive dampening,
  curtailment handling), in plain language with the *why*, not code detail.
- Link out to `DESIGN_DOCUMENT.md` for the deep maths/model.
- `### Multi-site` subsection covering per-array storage and the AC topologies
  (dedicated-AC vs shared-inverter DC apportionment).

## 8. Sensors — *Required*

Single table `| Sensor | Unit | Description |` listing every entity the integration
creates. Note diagnostic-only entities and any that stay unavailable until configured.
Keep in sync with `sensor.py`.

## 9. Services — *Required*

Single table `| Service | Description |` of every registered service
(`<domain>.<service>`). Keep in sync with `services.yaml` / `__init__.py`.

## 10. Standalone tools — *Optional*

Present only while `tools/` ships user-facing CLIs. Describe each tool, show a fenced
`bash` usage example, and note requirements (e.g. numpy). Use `### <tool>` subsections
when there is more than one.

## 11. Roadmap — *Optional*

Short bulleted list of planned/in-progress work with status, linking
`DESIGN_DOCUMENT.md#roadmap` for the full plan. Drop the section if there's nothing public to promise.

## 12. Compatibility — *Required*

`| Component | Version |` table: Home Assistant min version, Python, storage, and any
key runtime dep (numpy). Keep versions aligned with `manifest.json` / `hacs.json`.

## 13. License — *Required*

One line naming the license and linking `LICENSE`.

---

## Conventions

- **Horizontal rules** (`---`) separate every top-level (`##`) section.
- **Tables** for any field/sensor/service/version reference list; prose for concepts.
- **Bold lead-ins** on feature and explanation bullets.
- **Call-outs**: `> ⚠️` for warnings, `> **Heads up:**`/`> **Note:**` for advisories.
- **Collapsible `<details>`** for long optional snippets (helper YAML, etc.).
- **Images** live in `images/`: `dashboard.png` for the hero shot and
  `config-stepN-<slug>.png` for each wizard step. Every wizard step carries a screenshot.
- **Plain language first** — defer maths and internals to `DESIGN_DOCUMENT.md`.
- **Release sync**: on every release update the version-bearing bits (badges resolve
  automatically; the "What's new" heading/body/link do not) and re-check the Sensors,
  Services, Configuration and Compatibility tables against the code.
