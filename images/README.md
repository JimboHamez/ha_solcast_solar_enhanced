# Images

Documentation assets referenced by the project `README.md`.

- `dashboard.png` — screenshot of the Solcast Solar Enhanced sensors/dashboard in
  Home Assistant. **Placeholder:** drop the real screenshot here with this exact
  filename and it will appear in the README automatically.

## Config wizard screenshots

One screenshot per setup-wizard step, embedded under each `### Step N` heading in the
README. They currently ship as 1×1 PNG placeholders — replace each with a real capture
using the **same filename** and it shows up automatically.

| Step | Filename | What to capture |
|---|---|---|
| 1 — Site & System | `config-step1-site.png` | The Site & System form |
| 2 — Storage | `config-step2-storage.png` | The Storage form |
| 3 — Weather & Irradiance | `config-step3-weather.png` | The Weather & Irradiance form |
| 4 — Battery Storage | `config-step4-battery.png` | The Battery Storage form |
| 5 — PV Tuning & Dampening | `config-step5-tuning.png` | The PV Tuning & Dampening form |
| 6 — Per-site sensor mapping | `config-step6-sites.png` | The per-site mapping form (multi-site setups only) |

### Capture checklist

- **Where:** Settings → Devices & Services → Add Integration → **Solcast Solar Enhanced**.
  To re-walk the wizard without removing a live setup, use the integration's **Configure**
  (Options) flow — it mirrors the same six steps. Step 6 only appears when more than one
  Solcast site is discovered.
- **Format:** PNG, exact filenames above. Keep them small (these are docs assets, not
  hero art) — a single wizard dialog, not the whole browser window.
- **Framing:** capture just the dialog/card (step title + fields + Next/Submit), trimmed
  of surrounding chrome. Consistent width across all six reads best in the README.
- **Theme:** use the default light theme for legibility unless the README hero shot is
  dark — keep all six consistent with each other.
- **State:** show representative, populated fields (real sensor names are fine) so each
  field's purpose is clear.
- **Redact before committing:** blur or replace anything sensitive — latitude/longitude,
  any OpenWeatherMap API key (Step 3), and any private entity IDs you'd rather not expose.
- **Check after dropping in:** the placeholder reference resolves automatically; preview
  the README to confirm each image renders and is legible at GitHub's rendered width.
