"""Run the source Win32 keyboard preflight before expensive packaging/model work.

Uses the application's native smoke implementation and an independent safety
observer. All injected/counted events are tagged synthetic input, never physical
keyboard history. No security chord is injected or Windows policy changed.
"""
import argparse
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True, type=Path)
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This native integration preflight requires real Windows.')
    try:
        import dictate
        application = dictate.QApplication.instance() or dictate.QApplication(['Dictate keyboard verification'])
        report = dictate._check_windows_shortcuts(application)
        assert report['status'] == 'ok', report
    except Exception:
        report = {'status': 'error', 'error': traceback.format_exc(),
                  'physical_keyboard_test': False, 'reboot_performed': False}
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
