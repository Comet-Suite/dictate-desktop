# Privacy

Speech recognition is local. Dictate does not upload audio to a transcription service, and the app does not create recording files. Audio and intermediate transcripts remain in process memory while a recording is processed.

Downloading model metadata or weights contacts Hugging Face and its download infrastructure. The selected model must be available locally before offline inference can work.

Fast insertion uses the operating-system clipboard. The previous clipboard is restored when possible and only if the user has not replaced it. Clipboard managers, Windows clipboard history, remote-desktop clipboard sharing and Windows clipboard sync may retain or transmit text according to their own settings. Disable those services when your workflow requires it.

Preferences are saved under `%APPDATA%\Dictate`; error diagnostics may be saved there as `error.log`. Model files stay in the chosen download directory. Uninstalling the program does not intentionally remove these user files.
