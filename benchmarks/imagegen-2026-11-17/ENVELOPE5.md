# Image size limits: 5-run check (2026-11-17)

Walter, RTX 3070 Laptop GPU (8192 MiB), ComfyUI portable with `--lowvram`, SDXL Turbo fp16, 8 steps.
Idle VRAM before runs: 416 MiB. Times are generator time after one warm-up run; VRAM is the nvidia-smi peak sampled every 0.25 s.

| Case | Output | OK | Min ms | Median ms | Max ms | Peak VRAM MiB |
|---|---|---|---|---|---|---|
| draft | 1024x1024 | 5/5 | 2547 | 2563 | 2594 | 7010 |
| draft-above | 1088x1088 | 5/5 | 3062 | 3063 | 3266 | 7234 |
| quality | 1536x1536 | 5/5 | 8452 | 9657 | 11750 | 6978 |
| quality-above | 1600x1600 | 5/5 | 13281 | 15516 | 17061 | 6980 |

Result: the defaults stay as they are (draft 1024x1024, quality pass 1536x1536). One step above each limit also worked, but it was slower, quality-pass times climbed run over run (cause not established; possibly heat on a laptop GPU), and peak VRAM left about 1 GB free.
