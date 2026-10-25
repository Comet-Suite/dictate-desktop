"""Copy installed dependency notices and document matching source locations."""
from importlib import metadata
from pathlib import Path
import json
import re
import shutil
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'packaging/THIRD_PARTY_LICENSES'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    inventory = []
    sources = ['# Dependency source locations\n',
               'Exact installed versions used in this build. Follow the source-distribution and upstream links for corresponding source.\n',
               'Qt 6.11.2 source: https://download.qt.io/official_releases/qt/6.11/6.11.2/\n',
               f'Python source ({sys.version.split()[0]}): https://www.python.org/downloads/source/\n']
    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name, version = distribution.metadata['Name'], distribution.version
        folder = OUTPUT / re.sub(r'[^A-Za-z0-9_.-]', '_', name)
        notices = []
        for item in distribution.files or []:
            lowered = str(item).lower()
            if any(term in item.name.lower() for term in ('license', 'copying', 'notice')) and ('.dist-info/' in lowered or '.data/' in lowered):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    folder.mkdir(exist_ok=True)
                    destination = folder / str(item).replace('/', '__').replace('\\', '__')
                    shutil.copyfile(source, destination)
                    notices.append(str(destination.relative_to(OUTPUT)))
        links = distribution.metadata.get_all('Project-URL') or []
        row = dict(name=name, version=version, notices=notices, upstream=links)
        inventory.append(row)
        sources.append(f'\n## {name} {version}\n\n- Package: https://pypi.org/project/{name}/{version}/#files\n')
        for link in links:
            sources.append('- ' + link + '\n')
        try:
            request = urllib.request.Request(f'https://pypi.org/pypi/{name}/{version}/json')
            with urllib.request.urlopen(request, timeout=15) as response:
                info = json.load(response)
            for file in info.get('urls', []):
                if file.get('packagetype') == 'sdist':
                    sources.append('- Source archive: ' + file['url'] + '\n')
        except Exception:
            # The versioned package files page above is retained if PyPI is unavailable.
            pass
    (OUTPUT / 'inventory.json').write_text(json.dumps(inventory,indent=2), encoding='utf-8')
    (OUTPUT / 'SOURCES.md').write_text(''.join(sources), encoding='utf-8')
    print('Collected notices for', len(inventory), 'distributions')

if __name__ == '__main__':
    main()
