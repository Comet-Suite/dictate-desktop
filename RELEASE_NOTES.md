# Dictate 1.2.5

## Popup controls, without a redesign

- **Saved position lock, enabled by default.** Accidental drag gestures no longer move the popup or trigger a button on release. Normal microphone, pause, settings and close clicks still work. Choose a preset while locked, or unlock to drag deliberately.
- **Tiny and Extra small** sizes alongside the existing Small, Medium and Large capsule sizes. All four controls retain usable hit targets.
- **Live, saved appearance controls:** Purple/Blue/Green/Amber or a custom voice-bar color; 50–100% popup opacity; Subtle/Normal/Taller voice bars. Fade-in and hide/show preserve the chosen opacity.

## One model unload choice

**Settings → Speech model → UNLOAD** now contains exactly:

1. **5 minutes idle**
2. **10 minutes idle**
3. **15 minutes idle**
4. **Never**
5. **Immediately after pasting**

This replaces the two unload/preload switches and manual unload control. Showing the popup still preloads the selected model **without recording**. Immediately releases the native model only after successful complete insertion, never during a recording, model loading or queued work. An enabled old post-paste setting migrates to Immediately; otherwise the existing idle choice is retained.

## Preserved

The landscape three-column settings layout, capsule design, supplied logo, model catalog/downloads, true Unified and CrispASR engines, both shortcuts, explicit whole-session insertion, Auto Enter preference, numbers/symbols/custom mappings, startup registration and updater protections are retained. The readable dark/light updater palettes from 1.2.4 remain in place.

## Upgrade

Choose **Check for updates**, then approve **Install and restart**, or quit Dictate and run `Dictate-Setup-1.2.5-x64.exe` from this release into the existing installation folder. Settings, custom mappings, shortcuts, model files and startup preference are retained; old memory-control settings are migrated to one policy.

An already-running older app continues drawing its old interface until it restarts. The installer remains unsigned: use official downloads and verify `SHA256SUMS.txt`. Automated tests use synthetic popup/mouse/audio input where noted; no physical microphone recording or reboot is claimed.
