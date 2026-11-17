# DaveLLM image check (live)

Started 2026-11-17T06:44:42Z UTC. Generator: {'available': True, 'comfyui_version': '0.43.0', 'device': 'cuda:0 NVIDIA GeForce RTX 3070 Laptop GPU : cudaMallocAsync', 'checkpoint': 'sd_xl_turbo_1.0_fp16.safetensors'}.

**26/26 checks passed.**

| Check | Result | Detail |
|---|---|---|
| generator reachable | PASS | comfyui 0.43.0 on cuda:0 NVIDIA GeForce RTX 3070 Laptop GPU : cudaMallocAsync |
| C1 draft generated | PASS | HTTP 200 |
| C1 PNG is 768x768 | PASS | got (768, 768) |
| C1 manifest has provenance | PASS | id img-20261117T064452Z-75f165f2 |
| C1 saved file matches | PASS |  |
| C2 draft generated | PASS | HTTP 200 |
| C2 PNG is 768x768 | PASS | got (768, 768) |
| C2 manifest has provenance | PASS | id img-20261117T064455Z-2647b167 |
| C2 saved file matches | PASS |  |
| C3 draft generated | PASS | HTTP 200 |
| C3 PNG is 1024x576 | PASS | got (1024, 576) |
| C3 manifest has provenance | PASS | id img-20261117T064458Z-e92a9026 |
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
| per-image delete removes file and manifest | PASS |  |
| retention prunes images past 90 days | PASS |  |
| purge-all needs the exact confirmation and empties the library | PASS | deleted 3 |
| nothing left on disk after purge | PASS |  |

| Case | Stage | Size | Generator ms | Round trip ms |
|---|---|---|---:|---:|
| C1 | draft | 768x768 | 9483 | 9531 |
| C2 | draft | 768x768 | 3077 | 3125 |
| C3 | draft | 1024x576 | 3094 | 3141 |
| C1 | quality | 1152x1152 | 3250 | 3328 |

## Envelope probe (top of the validated limits)

Same host and session setup, run with a throwaway data directory on 2026-11-17 (America/New_York, about 1:46 AM). Raw numbers in `ENVELOPE.json`.

| Stage | Size | HTTP | Generator ms |
|---|---|---:|---:|
| draft | 1024x1024 | 200 | 6813 |
| quality | 1536x1536 | 200 | 5656 |
| draft | 1024x576 | 200 | 3609 |

GPU memory read after the runs: about 5.3-6.4 GB of 8 GB in use while ComfyUI (`--lowvram`) was still loaded. These single runs set the default envelope in `davellm_image_policy.load_limits()` (draft up to 1024x1024, quality output up to 1536x1536). They are one sample each, not a load test. The first draft (C1, ~9.5 s) includes the checkpoint load.

Generated PNGs are kept out of git; they stay in the separate check folder on Walter and in the session folder on the box.
