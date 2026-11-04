# Performance notes

Long shortcut recordings are transcribed in ordered pieces while recording continues. Pieces are held privately and joined only after the explicit stop/commit press. Quiet gaps are conservatively compacted. Overlapping context helps avoid missing words at hard inference boundaries.

The target receives one bulk paste. Auto Enter is optional and, when enabled, occurs once after that paste. This avoids the per-character event stream that can cause uneven text arrival or treat embedded newlines as send keys.

## Local comparison

The development comparison used identical 35-second speech input, replayed at twice real time, with four native CPU inference threads and already-loaded models. Input and output devices were mocked; native speech inference was not.

| Runtime/model | Before: stop to output | Background pipeline: stop to output |
|---|---:|---:|
| Unified Q4_K_M / transcribe.cpp | 7.122 s | 0.968 s |
| TDT v3 Q4_K / CrispASR | 9.728 s | 1.204 s |

These are local observations, not minimum-speed guarantees. Performance depends on processor, quantization, recording content, other workloads and the destination application. If inference cannot keep up with speech on a given machine, some queued work remains after Stop.

## Optional unload / preload trade-off

v1.2.3 enables **Unload after pasting** and **Preload when opening pop-up** by default. Unloading returns the native worker’s address space on Windows, but a cold reload and warm-up cost time. Open the popup before recording to preload without capturing dictation. Disable post-paste unloading if keeping a model ready matters more than its RAM use. Existing Never/5/10/15-minute idle timers are independent.

A failed or empty insertion does not trigger post-paste unload. Queued work/new recordings and model loading block it. Settings microphone monitoring is separate; not all application memory is model memory.
