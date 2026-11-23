# Changelog

## 1.2.6 — Hover recording controls and Windows-key shortcuts

- Fix the popup mic to start an explicit complete-session recording, retaining cold-load intent until the model is ready. Passive popup show/preload remains silent.
- Use a centered idle mic and a full-width animated waveform. Show ✕ / Pause–Resume / ✓ **only on hover and over the bars**, never as a separate row. Move Settings to the right-click menu.
- Cancel discards without pasting, hides and safely unloads after inference/loading finishes; the tray app remains running. Confirm commits once with the existing Auto Enter preference and hides without flashing a visible idle mic.
- Add Win/Windows/Meta alias support and native shortcut capture, including Win+H and Win+V. Keep Ctrl+Alt+D and Ctrl+Alt+Space as the two defaults.
- Transfer duplicate assignments automatically, stop cleared bindings, roll back failed registrations, invalidate queued stale activations and join native registration threads at teardown.
- Intercept exact supported assigned Win chords on a dedicated runtime-only low-level hook; consume their repeats/key-up and mask the Start menu. Do not change registry/policies or claim protected security combinations are overrideable.
- Preserve the v1.2.5 lock, all five sizes, saved appearance, one unload policy, settings layout, artwork, model/download catalog, native isolation, symbols/numbers/mappings, whole-session paste, focus, startup and update protections.
- Expand synthetic hover/controller/keyboard regressions and frozen/installed Windows shortcut verification. Advance the real in-place upgrade fixture to the final hash-pinned published 1.2.5 installer, preserving its saved popup preferences. Add a no-publication Windows preflight and refuse to overwrite historical releases.

## 1.2.5 — Popup controls and one unload policy

- Add a saved, default-on position lock that blocks drag-release mistakes without disabling popup buttons; preserve deliberate preset positioning and unlocked dragging.
- Add Tiny and Extra small without changing the existing three popup sizes or capsule design; retain usable hit targets.
- Add live, saved voice-bar presets/custom colors, 50–100% popup opacity, and Subtle/Normal/Taller bar heights. Preserve opacity through all fades.
- Replace the two lifecycle switches and manual unload control with one ordered 5/10/15/Never/Immediately-after-pasting dropdown, migrating old preferences safely.
- Keep automatic silent popup preloading and all existing recording/loading/pending-insertion unload guards.
- Preserve the complete dictation/native-engine pipeline, model catalog, updater contrast/consent/hash checks, startup behavior, installation identity and supplied artwork.
- Add popup rendering/mouse checks to frozen/installed smoke tests and extend the actual published 1.2.4 → 1.2.5 Windows upgrade check to cover legacy policy and saved position migration.

## 1.2.4 — Updater contrast fix

- Set explicit native Qt palettes for active, inactive and disabled updater text and controls to avoid black-on-black Windows theme combinations.
- Increase secondary-text and action-button contrast in both themes; preserve the rounded layout, logo and existing update consent/safety policy.
- Refresh an already-open updater when the app theme changes.
- Add automated adversarial-palette rendering and contrast checks to source, frozen and installed Windows smoke tests.
- No dictation, model lifecycle/catalog, shortcuts, insertion, settings or installer identity changes.

## [1.2.3] — Model controls and update progress

### Added
- Measured updater progress with byte totals, speed/ETA, separate persisted-file SHA-256 verification, cancel/retry, and an animated rounded progress window and Settings bar.
- Native installation file-progress display after explicit consent; integrity, parent-wait, recording and in-place installation safeguards remain.
- Independent unload-after-successful-paste and preload-on-popup options, both enabled by default for new/upgraded settings; safe lazy startup and no recording from visibility shortcuts.
- Curated model catalog with friendly names, language/runtime details, exact formats/sizes, revision-pinned downloads, model licenses and explicit release-test variants.

### Improved
- Consistent rounded corners without rearranging the settings columns, popup, theme or logo.
- Full regression gating before Windows release publication and expanded native fixtures across all five preset families.

## 1.2.2

- Replace the app, tray/taskbar, executable, installer/shortcut and repository logo with the supplied new artwork.
- Preserve its colors, transparency and aspect ratio; provide multi-resolution Windows icons.
- Logo/version-only runtime update: no changes to dictation, layout, shortcuts, symbols, model handling or updater behavior.
- Distribute through the existing confirmed-install updater, retaining installation location and user data.

## 1.2.1

- Add safe manual/idle native-model unloading, efficient binary audio IPC and runtime-specific GGUF quantization metadata.
- Add editable UTF-8 custom phrase mappings and spoken enter as a literal newline rather than an Enter keypress.
- Add official stable-release download verification, explicit installation consent and a native in-place upgrade/restart helper.
- Retain the existing UI layout, both shortcuts, Auto Enter choice, settings and models.

## 1.2.0

- Restore independent show/hide and start/commit dictation shortcuts.
- Preserve customized shortcut settings, including migration from the single-shortcut build.
- Transcribe long recordings in the background; hold all insertion until explicit commit.
- Insert each completed recording in one bulk paste, with an optional single Enter.
- Add natural currency amounts, percentages, numeric formulas and spoken email addresses.
- Keep support for 167 explicitly named symbols and true Parakeet Unified GGUF models.
- Preserve the settings clipping and input-focus corrections.
- Embed the supplied Dictate logo in the application, tray, executable and Windows shortcuts.
- Add a Windows installer, portable package, checksums and native runtime smoke tests.
