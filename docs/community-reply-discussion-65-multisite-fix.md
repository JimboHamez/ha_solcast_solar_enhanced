# Community reply — discussion #65, multi-site setup never reached the sites step

Context: GitHub discussion [#65](https://github.com/JimboHamez/ha_solcast_solar_enhanced/discussions/65)
(gu3stZA, two Solcast sites, base 4.6.1). After the 1.10.3 "not detected" fix the
wizard still set up one combined site. Diagnosed as the `"solcast" in entity_id`
filter in `discover_sites` dropping rooftops created on a pre-2026.4 core.

Status: **shipped in v1.11.1** (stable, 17 Sep 2026). Reply below is written for that
state. Post as a reply in the existing thread, then mark it as the answer.

## Reply

Hey — this is fixed in **v1.11.1**, out today on the stable channel.

It was what I suspected above. The wizard finds your arrays through the base
integration's per-site rooftop sensors, and it was only keeping the ones whose
entity id contained `solcast`. The base integration never asks Home Assistant to
put the device name in front of those ids, so the id is whatever the core you
were running at the time came up with: `sensor.<site>` on anything before
2026.4, and `sensor.solcast_pv_forecast_<site>` only on 2026.4 and later. Entity
ids stick once created, so a base that was first set up on an older core keeps
the short form forever. Both of your rooftops were being thrown away, discovery
came back empty, and the flow quietly fell back to the single-array setup.

Rooftops are now recognised by which integration owns them in the entity
registry, so it no longer matters what they're called.

To pick it up:

1. Update to v1.11.1 in HACS and restart Home Assistant.
2. Open **Settings → Devices & services → Solcast Solar Enhanced → Configure**.
   Step 1 will now detect two arrays, and you'll get the **sites** step after it.
3. On the sites step, choose the measurement topology that matches how your two
   arrays are metered — separate generation sensors per array (*direct*), one
   shared inverter with per-array DC readings (*DC split*), or one combined
   meter with nothing per array (*one combined meter*) — and point each array
   at its own sensor.

Nothing else changed in this release, so if you're already on 1.11.0 it's a
drop-in update. If you did rename the entity ids as a workaround, you can leave
them as they are or change them back — either form is found now.

Once you've saved the sites step you'll get a device per array with its own
measured PV Power, Shading and Tuned Tilt sensors, and the per-array dampening
starts building from there. It needs a few weeks of history before the
per-array factors move much, same as the property-wide one did.

Let me know if the sites step still doesn't appear after updating and I'll dig
in again.

Cheers
