# Dictate 1.2.4

## Fix: updater text visibility

- Explicit foreground/background/control palettes prevent the updater from inheriting unreadable Windows/Qt color combinations.
- Active, inactive and disabled states are covered in both light and dark themes.
- Stronger secondary-text and action-button contrast; an open updater follows the app’s theme switch immediately.
- Automated rendering checks deliberately supply black-on-black and white-on-white application palettes. Text and button states must meet a 4.5:1 contrast floor.

The existing rounded layout, supplied logo, update progress, SHA-256 verification, **Install and restart / Later** confirmation, and recording-safety guards are retained. Dictation, the model catalog and unload/preload behavior are unchanged from 1.2.3.

## Upgrade

Choose **Check for updates**, then approve **Install and restart**. Preferences, shortcuts, custom mappings, downloaded models, startup preference and the registered installation folder are retained.

An already-running older app draws its existing prompt until the update finishes and it restarts. If that old prompt is unreadable, its **Install and restart** button still starts the verified update, or quit Dictate and run the official 1.2.4 installer manually into the existing folder. The fixed palette is active after restarting into 1.2.4.

The installer is unsigned. Use official release downloads and verify `SHA256SUMS.txt`. No physical reboot or microphone recording is claimed by the automated checks.
