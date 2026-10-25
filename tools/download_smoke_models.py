"""Download fixed, hash-verified packaging fixtures; never put them in Git."""
from pathlib import Path
import hashlib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ('unified.gguf', 'https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf/resolve/main/parakeet-unified-en-0.6b-Q4_K_M.gguf?download=true',
     'a8bf3de2b393bd14ead5a858c3748d5e3b07a20fdeabdd3b498fba4f463fa929'),
    ('tdt.gguf', 'https://huggingface.co/cstr/parakeet-tdt-0.6b-v3-GGUF/resolve/main/parakeet-tdt-0.6b-v3-q4_k.gguf?download=true',
     'addef407ab841eef45739e78920fd8c00e63f78eaa823c490e600e0ab58080a1'),
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
    directory = ROOT / '.ci-models'
    directory.mkdir(exist_ok=True)
    for name, url, expected in FILES:
        target = directory / name
        if not target.exists() or digest(target) != expected:
            print('Downloading', name, flush=True)
            request = urllib.request.Request(url, headers={'User-Agent': 'Dictate-Packaging/1.2.0'})
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
