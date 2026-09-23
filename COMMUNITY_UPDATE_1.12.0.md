# Solcast Solar Enhanced — 1.12.0: requires Home Assistant 2026.8, plus what's landed since the beta post

> Draft update post for the Home Assistant Community forum.
> Copy everything below the line as a reply on the existing topic.
> The last forum post covered the 1.11.0 betas up to b5, so this one also catches readers up
> on b7, the 1.11.0 stable release and the 1.11.1 patch.

---

Hi all 👋

Another update on **[Solcast Solar Enhanced](https://github.com/JimboHamez/ha_solcast_solar_enhanced)**, the companion to [BJReplay's ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) that keeps your own actual-vs-forecast history, tunes your panel tilt from it, and pushes a measured shading correction back to Solcast. It's still an **add-on, not a replacement**, and it still makes **zero extra Solcast API calls**. 🙏 to BJReplay as always.

## 🆕 1.12.0: now requires Home Assistant 2026.8.0 or later

This is a maintenance release. **No new features, and nothing changes in how it behaves.** The one thing to know is the new minimum: **Home Assistant 2026.8.0** (it was 2026.5.4).

**Why raise the minimum for a release that changes nothing?** Home Assistant has been retiring some of the ways integrations talk to it, and two of them had deadlines this integration was on course to miss:

- **HA 2026.12 would have rejected how the integration reloads itself after you change its options.** The fix is a small change to how the options screen reloads the integration. Without it, updating HA in December would have broken this integration.
- **The link from each array's device to the main integration device was built the old way**, which HA deprecated in 2026.8 and stops accepting in 2027.8. The new way only exists from HA 2026.8.0, so that's what sets the new minimum.

I'd rather ship both fixes now, while nothing is broken, than rush them out when an HA release breaks the integration.

**Upgrading:**

- **On HA 2026.8 or later?** It's a drop-in update. Your devices, entities, history and settings are all unchanged, and there's nothing to reconfigure.
- **On something older?** HACS won't offer you 1.12.0 until you update Home Assistant, and **1.11.1 keeps working** in the meantime. You don't miss out on anything functional by waiting.
- **Your Solcast base integration doesn't need updating.** Base **4.5.2, 4.6.0 and 4.6.1** all work with this release.

## 📬 What you missed since the beta post

The last post here covered the 1.11.0 betas up to b5. Since then:

### ⚡ If you have an export limit, partial curtailment is no longer mistaken for shading (1.11.0b7)

The curtailment check compared your export limit against the **average** export over each half hour. But an export limit bites **instantly**. So a half hour that was capped for ten of its thirty minutes averaged out to well under the limit, looked uncapped, and its held-down output got booked as shading and pushed to Solcast.

Each half hour now also records the **peak** export. If the peak reached the limit, that half hour counts as neutral instead of being scored as shading. I verified this on a live 8 kW system behind a 5 kW export limit. It builds up from the day you upgrade, and systems with no export limit are unaffected. ([#86](https://github.com/JimboHamez/ha_solcast_solar_enhanced/issues/86))

### ✅ 1.11.0 went stable (17 Sep)

The whole beta line was promoted unchanged: the **shading map** sensor, the **one combined meter** option for systems like the Powerwall 3, the **multi-MPPT** picker for arrays spread across several trackers, and the curtailment fix above.

**⚠️ Still worth doing if you have a multi-array system:** open **Configure** once and check each array points at **its own** generation sensor. Putting the same whole-system sensor on two arrays used to be accepted, and it silently turned dampening off. The wizard now refuses it, but it can't fix a setup saved before that check existed.

### 🏠 1.11.1: multi-array setups that never showed the per-array step (17 Sep)

If you have two or more arrays and the setup wizard has always treated your property as **one** array, this was why. The wizard only recognised your Solcast rooftop sensors if their entity id contained `solcast`. On a base integration first installed before HA 2026.4, those ids don't contain it, so none were found. Rooftops are now recognised by which integration owns them, whatever they're called. After updating, open **Configure** and you'll get the per-array step. Thanks to the user in [discussion #65](https://github.com/JimboHamez/ha_solcast_solar_enhanced/discussions/65) who stuck with that one. 🍻

## 📦 HACS default repository

The submission to the HACS default list ([hacs/default#10336](https://github.com/hacs/default/pull/10336)) is **still in the queue**. **Please don't comment on or react to that PR.** HACS ask people not to, it doesn't speed anything up, and it adds work for volunteer reviewers.

Until it's accepted, installing takes one extra step:

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/JimboHamez/ha_solcast_solar_enhanced` as an **Integration**
3. Install, restart, then add it from **Settings → Devices & services**

## 🙏 Feedback

The standing requests are still open. **Powerwall 3 owners**: does the combined-meter option set up cleanly for you? **Anyone with obvious morning or afternoon shade**: does the shading map's bearing and elevation match what you can actually see from your roof?

Full detail is in the [CHANGELOG](https://github.com/JimboHamez/ha_solcast_solar_enhanced/blob/main/CHANGELOG.md) and the [1.12.0 release notes](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/tag/v1.12.0). Issues and questions go on [GitHub](https://github.com/JimboHamez/ha_solcast_solar_enhanced/issues), or reply here. 🌞
