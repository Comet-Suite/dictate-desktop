# Supported speech models

The Windows application bundles CrispASR 0.8.44 and transcribe.cpp 0.4.1. The curated catalog below is deliberately narrower than every architecture those upstream projects may describe. Only compatible GGUF speech models work; arbitrary LLM GGUF files do not. No dependency upgrade or native architecture conversion is implied.

## Curated, revision-pinned downloads

Sizes below are decimal MB (1 MB = 1,000,000 bytes), **not peak RAM**. Available upstream variants and release-tested variants are distinct. The release workflow must pass its native fixtures before publication; inspect the published `smoke-installed.json` for that release’s actual results.

### Parakeet Unified English 0.6B

- **Repository / license:** [handy-computer/parakeet-unified-en-0.6b-gguf](https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf) — NVIDIA Open Model License.
- **Language coverage:** English.
- **Runtime:** transcribe.cpp / RNN-T, local CPU.
- **Curated revision:** `843b1b175ace04f5a89d379c79e2518c0d065b25`.
- **Release fixtures:** Q4_K_M, Q5_K_M; fixture audio is an English public-domain sample, not a multilingual accuracy certification.

| Format | File | Download |
|---|---|---:|
| Q4_K_M | `parakeet-unified-en-0.6b-Q4_K_M.gguf` | 477.3 MB |
| Q5_K_M | `parakeet-unified-en-0.6b-Q5_K_M.gguf` | 540.8 MB |
| Q6_K | `parakeet-unified-en-0.6b-Q6_K.gguf` | 602.2 MB |
| Q8_0 | `parakeet-unified-en-0.6b-Q8_0.gguf` | 731.4 MB |
| F16 | `parakeet-unified-en-0.6b-F16.gguf` | 1,239.1 MB |
| F32 | `parakeet-unified-en-0.6b-F32.gguf` | 2,473.3 MB |

True NVIDIA Unified English. This is not the similarly named TDT conversion.

### Parakeet TDT 0.6B v3

- **Repository / license:** [cstr/parakeet-tdt-0.6b-v3-GGUF](https://huggingface.co/cstr/parakeet-tdt-0.6b-v3-GGUF) — CC-BY-4.0.
- **Language coverage:** 25 European languages, including English.
- **Runtime:** CrispASR / TDT, local CPU.
- **Curated revision:** `dff69b0e675921ffd5a6133f5fbe2aac690d3ec3`.
- **Release fixtures:** Q4_K, Q8_0; fixture audio is an English public-domain sample, not a multilingual accuracy certification.

| Format | File | Download |
|---|---|---:|
| Q4_K | `parakeet-tdt-0.6b-v3-q4_k.gguf` | 417.9 MB |
| Q5_0 | `parakeet-tdt-0.6b-v3-q5_0.gguf` | 470.3 MB |
| Q8_0 | `parakeet-tdt-0.6b-v3-q8_0.gguf` | 674.3 MB |
| F16 | `parakeet-tdt-0.6b-v3.gguf` | 1,255.3 MB |

NVIDIA multilingual Parakeet v3. Automatic speech-language recognition; English text commands are unchanged.

### Orukeet

- **Repository / license:** [cstr/orukeet-GGUF](https://huggingface.co/cstr/orukeet-GGUF) — CC-BY-SA-4.0.
- **Language coverage:** 25 European languages, including English.
- **Runtime:** CrispASR / TDT, local CPU.
- **Curated revision:** `2a93474c2771a6ca2228a7e001bd06ce0c97f880`.
- **Release fixtures:** Q4_K; fixture audio is an English public-domain sample, not a multilingual accuracy certification.

| Format | File | Download |
|---|---|---:|
| Q4_K | `orukeet-q4_k.gguf` | 402.2 MB |
| Q5_0 | `orukeet-q5_0.gguf` | 470.3 MB |
| Q8_0 | `orukeet-q8_0.gguf` | 674.3 MB |
| F16 | `orukeet-f16.gguf` | 1,255.3 MB |

Multilingual Parakeet-family derivative. Read the model card for language and accuracy details.

### Phonon-2

- **Repository / license:** [cstr/phonon2-GGUF](https://huggingface.co/cstr/phonon2-GGUF) — CC-BY-4.0.
- **Language coverage:** English.
- **Runtime:** CrispASR / TDT, local CPU.
- **Curated revision:** `3ed3e6ad6e7ce63affffeede37328ff756efaa2f`.
- **Release fixtures:** Q4_K; fixture audio is an English public-domain sample, not a multilingual accuracy certification.

| Format | File | Download |
|---|---|---:|
| Q4_K | `phonon2-q4_k.gguf` | 402.2 MB |
| Q8_0 | `phonon2-q8_0.gguf` | 674.3 MB |
| F16 | `phonon2-f16.gguf` | 1,255.3 MB |

English Parakeet-family derivative. Standard GGUF weights, not the small custom upstream transport.

### Parakeet Redux

- **Repository / license:** [cstr/parakeet-redux-GGUF](https://huggingface.co/cstr/parakeet-redux-GGUF) — CC-BY-4.0.
- **Language coverage:** Multilingual; see model card.
- **Runtime:** CrispASR / TDT, local CPU.
- **Curated revision:** `3f288b7fb33caff80f1424bd5ea1cecaf9808209`.
- **Release fixtures:** Q4_K; fixture audio is an English public-domain sample, not a multilingual accuracy certification.

| Format | File | Download |
|---|---|---:|
| Q4_K | `parakeet-redux-q4_k.gguf` | 402.2 MB |
| Q8_0 | `parakeet-redux-q8_0.gguf` | 674.3 MB |
| F16 | `parakeet-redux-f16.gguf` | 1,255.3 MB |

Parakeet v3 architecture with converted ternary-trained weights. GGUF file size is not runtime RAM.

## Selection and verification limits

- The model picker offers only the formats recorded for its pinned revision. “Any” selects that model’s low-memory default. Missing requested files/quantizations fail explicitly; they are not replaced by a larger random file.
- The unsuffixed TDT v3 GGUF is explicitly mapped as F16 by this catalog; generic custom-repository selection still does not guess an unlabeled file’s quantization.
- Choose a custom repository ID or a local GGUF for other compatible speech models. Custom repositories are not pinned by Dictate’s catalog; you are responsible for their provenance, native compatibility and license.
- True Parakeet Unified English uses the `handy-computer` conversion and transcribe.cpp. A similarly named TDT conversion is not substituted.
- GGUF headers route the runtime and show architecture/quantization. A filename or changed label is not proof of engine support.
- English spoken numbers, symbols and text commands remain English. Upstream multilingual coverage is not a release test of every language, accent or microphone.
- New model weights/quantizations can have different memory use, speed and recognition behavior. F16/F32 are much larger downloads; do not infer RAM from file size alone.

## Upstream references

- [CrispASR Python bindings and architecture limits](https://pypi.org/project/crispasr/0.8.44/)
- [transcribe.cpp Python bindings](https://github.com/handy-computer/transcribe.cpp/tree/main/bindings/python)
- Model cards linked above contain the original weight licenses and source-model references. Models are downloaded separately, not included in the app installer.
