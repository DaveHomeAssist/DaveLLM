# DaveLLM image check (live)

Started 2026-11-17T08:23:57Z UTC. Generator: {'available': True, 'comfyui_version': '0.43.0', 'device': 'cuda:0 NVIDIA GeForce RTX 3070 Laptop GPU : cudaMallocAsync', 'checkpoint': 'sd_xl_turbo_1.0_fp16.safetensors'}.

**50/50 checks passed.**

| Check | Result | Detail |
|---|---|---|
| generator reachable | PASS | comfyui 0.43.0 on cuda:0 NVIDIA GeForce RTX 3070 Laptop GPU : cudaMallocAsync |
| C1 draft generated | PASS | HTTP 200 |
| C1 PNG is 768x768 | PASS | got (768, 768) |
| C1 manifest has provenance | PASS | id img-20261117T082404Z-60e1eb09 |
| C1 saved file matches | PASS |  |
| C2 draft generated | PASS | HTTP 200 |
| C2 PNG is 768x768 | PASS | got (768, 768) |
| C2 manifest has provenance | PASS | id img-20261117T082407Z-4e20a108 |
| C2 saved file matches | PASS |  |
| C3 draft generated | PASS | HTTP 200 |
| C3 PNG is 1024x576 | PASS | got (1024, 576) |
| C3 manifest has provenance | PASS | id img-20261117T082410Z-ad292c29 |
| C3 saved file matches | PASS |  |
| same seed and settings reproduce the draft byte-for-byte | PASS | identical |
| save=false leaves no library entry | PASS |  |
| quality pass generated | PASS | HTTP 200 |
| quality pass keeps seed and links its draft | PASS |  |
| quality pass is 1.5x the draft | PASS | got (1152, 1152) |
| over-limit request pauses for approval and generates nothing | PASS | proposed {'width': 1024, 'height': 1024, 'steps': 4} |
| material rewrite needs review by default | PASS |  |
| formatting-only change generates without review | PASS | HTTP 200 |
| suggest mode never sends a rewrite | PASS |  |
| U6 aspect presets snap to 64 and stay within the draft limit | PASS | 20 presets |
| U6 16:9 max preset 1344x768 generated | PASS | HTTP 200 |
| U6 output size matches the preset | PASS | got (1344, 768) |
| U7 style preset is appended to the model prompt and recorded | PASS | HTTP 200 |
| U7 presets reset to defaults | PASS |  |
| U1 gallery thumbnail rendered by ComfyUI (long side 256) | PASS | (256, 256) |
| U1 gallery search and seed filter | PASS | search 1, seed 1 |
| U2 remix recipe from the manifest reproduces the image | PASS | HTTP 200 |
| U3 four variations finished | PASS | 4/4 in 9889 ms |
| U3 variations use consecutive seeds and differ | PASS |  |
| U8 never more than one GPU job running | PASS | max running 1 |
| U4 image to image from a library image | PASS | HTTP 200 |
| U4 records source and strength | PASS |  |
| U4 image to image from an upload | PASS | HTTP 200 |
| U5 inpaint generated | PASS | HTTP 200 |
| U5 inpaint manifest records the mask | PASS |  |
| U8 queue reports positions 0/1/2 | PASS | [0, 1, 2] |
| U8 cancel stops a running job and drops a queued one | PASS | ['cancelled', 'done', 'cancelled'] |
| U8 progress reported in steps | PASS | {'value': 4, 'max': 4} |
| U9 export embeds DaveLLM tEXt and drops ComfyUI's workflow chunks | PASS | stored file had ['prompt'] |
| U9 strip-metadata export has no text chunks | PASS |  |
| U9 zip export of selected images | PASS | 6 entries |
| U10 favorite kept past retention, non-favorite pruned | PASS |  |
| U10 favorite and collection filters | PASS |  |
| per-image delete removes file and manifest | PASS |  |
| retention prunes images past 90 days | PASS |  |
| purge-all needs the exact confirmation and empties the library | PASS | deleted 12 |
| nothing left on disk after purge | PASS |  |

| Case | Stage | Size | Generator ms | Round trip ms |
|---|---|---|---:|---:|
| C1 | draft | 768x768 | 6469 | 6532 |
| C2 | draft | 768x768 | 3077 | 3141 |
| C3 | draft | 1024x576 | 3061 | 3108 |
| C1 | quality | 1152x1152 | 3610 | 3704 |
| U6-16:9 | draft | 1344x768 | 3108 | 3203 |
| U7-style | draft | 768x768 | 3608 | 3672 |
| U3-seed300 | draft | 768x768 | 3125 | queued |
| U3-seed301 | draft | 768x768 | 2078 | queued |
| U3-seed302 | draft | 768x768 | 2047 | queued |
| U3-seed303 | draft | 768x768 | 2280 | queued |
| U4-img2img | draft | 768x768 | 3625 | 3734 |
| U4-upload | draft | 768x768 | 3625 | 3766 |
| U5-inpaint | draft | 768x768 | 3750 | 3922 |

## Notes (2026-11-17, Walter)

- Live run of `scripts/check_image_generation.py` against ComfyUI 0.43.0 on the RTX 3070 Laptop GPU (8192 MiB), SDXL Turbo fp16, `--lowvram`.
- The UI walkthrough (20/20 checks, `ui_upgrades.cjs`) ran right after against the same ComfyUI.
- Peak GPU memory across both runs, sampled every 0.5 s with nvidia-smi: 7234 MiB of 8192 (217 samples). This matches the 1088x1088 peak in ENVELOPE5; the largest image in these runs was the 1344x768 16:9 preset.
- C1's first draft (6469 ms) includes model load. Variation times are generator time only; they ran back to back through the queue.
- Inpaint uses SDXL Turbo, a regular checkpoint, with `SetLatentNoiseMask`. At strength 1.0 with a rectangular mask, it replaced the masked area cleanly in this single run. At 0.9 in the UI run, the new object blended with part of the original. One sample each, so this is not a measured quality comparison.
