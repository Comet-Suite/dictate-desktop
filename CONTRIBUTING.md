# Contributing

Issues and focused pull requests are welcome. Please describe the problem, expected behavior, Windows version, selected model and quantization. Do not include private audio, transcripts, access tokens or credentials.

## Development

Install `requirements-dev.txt`, then run:

```text
python -m pytest -q
python -m ruff check dictate.py --select F,E9
```

Keep the application runnable as a single Python file. Preserve capture ordering, commit-only output, one insertion per recording and the separate visibility/dictation shortcuts. Changes to native loading should be tested against both compatible Unified and CrispASR model layouts.

See `docs/BUILDING.md` before changing packaging. Contributions are submitted under this repository's GPL-3.0 license.
