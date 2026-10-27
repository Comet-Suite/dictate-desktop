"""Download fixed, hash-verified packaging fixtures; never put them in Git."""
from pathlib import Path
import hashlib
import os
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ('unified.gguf', 'https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf/resolve/843b1b175ace04f5a89d379c79e2518c0d065b25/parakeet-unified-en-0.6b-Q4_K_M.gguf?download=true',
     'a8bf3de2b393bd14ead5a858c3748d5e3b07a20fdeabdd3b498fba4f463fa929'),
    ('tdt.gguf', 'https://huggingface.co/cstr/parakeet-tdt-0.6b-v3-GGUF/resolve/dff69b0e675921ffd5a6133f5fbe2aac690d3ec3/parakeet-tdt-0.6b-v3-q4_k.gguf?download=true',
     'addef407ab841eef45739e78920fd8c00e63f78eaa823c490e600e0ab58080a1'),
    ('unified-q5_k_m.gguf', 'https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf/resolve/843b1b175ace04f5a89d379c79e2518c0d065b25/parakeet-unified-en-0.6b-Q5_K_M.gguf?download=true',
     'f9def6f9b4e83ab7d006df3e1b676dfa1f973a3b6da232a9c99fcaa66bcd2836'),
    ('tdt-q8_0.gguf', 'https://huggingface.co/cstr/parakeet-tdt-0.6b-v3-GGUF/resolve/dff69b0e675921ffd5a6133f5fbe2aac690d3ec3/parakeet-tdt-0.6b-v3-q8_0.gguf?download=true',
     '300de963db10e991a8c3c1674000245546f2e99d396f860aecbde2dd0534e43f'),
    ('jfk.wav', 'https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/samples/jfk.wav',
     '59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e'),
]

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(2 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def main():
    directory = Path(os.environ.get('DICTATE_SMOKE_MODELS', ROOT / '.ci-models'))
    directory.mkdir(exist_ok=True)
    for name, url, expected in FILES:
        target = directory / name
        if not target.exists() or digest(target) != expected:
            print('Downloading', name, flush=True)
            request = urllib.request.Request(url, headers={'User-Agent': 'Dictate-Packaging/1.2.2'})
            temporary = target.with_suffix('.partial')
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as output:
                while block := response.read(2 * 1024 * 1024):
                    output.write(block)
            if digest(temporary) != expected:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f'Hash mismatch for {name}')
            temporary.replace(target)
        print('Verified', name, flush=True)

if __name__ == '__main__':
    main()
