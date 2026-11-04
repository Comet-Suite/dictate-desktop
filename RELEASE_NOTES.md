# Dictate 1.2.3

A smoother updater, deliberate model-memory controls, and a clearer model catalog—without rearranging the familiar app.

## What’s new

- **Visible update progress:** animated, measured download progress in Settings and a dedicated rounded window; transferred size, speed and estimated time; a separate persisted-file SHA-256 verification stage; cancellation and retry. Installation still requires explicit **Install and restart** approval. The native installer then displays real file-install progress.
- **Unload after pasting:** release model/native-worker memory after a successful completed insertion, never while recording, loading or processing pending work.
- **Preload when opening the popup:** Show / Hide preloads the selected model without starting recording. With this enabled the model is not eagerly loaded at startup. Dictation remains on its own shortcut.
- **Both new model switches are enabled by default**, including for existing settings. Each can be disabled independently. Existing idle timers and manual unload remain.
- **Supported-model listing:** readable names, languages, runtime, exact available formats/download sizes, pinned curated revisions, license links and clearly identified release-test variants. Custom repositories and local compatible GGUF files remain available.
- **Rounded polish:** consistent rounded cards, controls, progress and dialogs, retaining the current three-column layout, capsule popup, colors and supplied logo.

## Upgrade

On **1.2.1 or 1.2.2**, choose **Check for updates** in the tray or Settings, then approve **Install and restart** after the verified download. The old client necessarily uses its existing updater UI for this one upgrade; the new progress interface is part of 1.2.3 for subsequent updates.

Settings, shortcuts, Auto Enter, custom mappings, selected models, downloaded model files, startup preference and the registered installation directory are retained. Version 1.2.0 needs a manual installer upgrade once. Installers are unsigned; verify official downloads against `SHA256SUMS.txt`.

No microphone audio or transcripts are uploaded by release checks or model downloads. Existing explicit Start/Stop sessions, one bulk paste, spoken newlines, leading-zero number handling and native-runtime isolation are retained.
