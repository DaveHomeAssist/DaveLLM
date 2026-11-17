# DaveLLM ImageGen Harness Benchmark Report

> The twelve result images (T01.png to T12.png) are kept on Walter beside this report and are not committed, to keep binaries out of git history.

- Date: 2026-11-01 (America/New_York)
- Execution host: Walter
- Visible client: DaveLLM Electron app
- Generator: ComfyUI 0.43.0, SDXL Turbo, RTX 3070 Laptop GPU
- Observed configuration: 768x768, 4 steps, one image per request, raw prompt, empty negative prompt

## Executive result

**129/240 (53.8%) — Weak for constraint-sensitive production work.**

The harness is fast and produces attractive first drafts for broad photographic,
watercolor, food, historical, and surreal concepts. It is unreliable for exact
text, exact counts, topology, spatial relationships, exclusion rules, requested
aspect ratios, and technical product fidelity. The model is being asked to solve
every class of request with the same square, four-step workflow and no validation.

## Detailed scores

Scores are Subject / Composition / Style / Artifact control / Special constraints.

| ID | Score | Breakdown | Pointed finding |
|---|---:|---|---|
| T01 | 11/20 | 3 / 2 / 3 / 2 / 1 | Good FOH mood, but square output, no clear two-screen layout, and anatomy/cabling artifacts. |
| T02 | 10/20 | 2 / 2 / 3 / 3 / 0 | Polished metal product, but two stacked units, many invented connectors, and pseudo-brand text. |
| T03 | 4/20 | 1 / 1 / 1 / 1 / 0 | Exact-copy task failed completely: wrong object, multiple badges, hand added, and unreadable text. |
| T04 | 6/20 | 2 / 0 / 2 / 2 / 0 | Recognized adapters, but produced many objects, multiple rows, cables, overlap, and pseudo-text. |
| T05 | 11/20 | 3 / 1 / 3 / 3 / 1 | Strong flat camera mark, but it fills the frame and adds a radial background instead of isolation. |
| T06 | 17/20 | 4 / 3 / 4 / 3 / 3 | Best general illustration: convincing rain, reflections, palette, workers, truck, and painted texture. |
| T07 | 7/20 | 1 / 0 / 3 / 3 / 0 | Isometric rendering works, but the scene becomes a large office with many desks and monitors. |
| T08 | 12/20 | 3 / 2 / 3 / 3 / 1 | Credible red backstage scene, but lacks the requested rack inspection, flashlight, and stage-spill contrast. |
| T09 | 17/20 | 4 / 4 / 3 / 3 / 3 | Strong macro composition and ingredients; texture is over-sharpened and less natural than requested. |
| T10 | 14/20 | 3 / 3 / 3 / 3 / 2 | Convincing period machinery and palette, but two people appear and exact projector details drift. |
| T11 | 15/20 | 4 / 3 / 4 / 3 / 1 | Excellent jellyfish concept and reflections; street is not empty and building signs become pseudo-text. |
| T12 | 5/20 | 2 / 0 / 2 / 1 / 0 | Diagram styling appears, but six-node linear topology and readable labels fail completely. |

## What the harness currently does well

- Fast visible completion: approximately 5-7 seconds per 768x768 draft once warm.
- Strong broad semantic matching for common photographic and illustration subjects.
- Good color, lighting, and atmosphere for loosely constrained creative prompts.
- Stable UI behavior across 12 sequential generations; every request returned an image and seed.

## Highest-priority implementation plans

### P0 — Add a structured generation contract

**Problem:** The UI sends prose into one fixed square workflow. Aspect ratio,
negative constraints, quality intent, and constraint type are not first-class.

**Plan:**

1. Extend the `/image/generate` request with `use_case`, `aspect_ratio`,
   `negative_prompt`, `quality`, and optional `seed`.
2. Map approved aspect ratios to bounded dimensions (square, landscape, portrait).
3. Add simple controls beside Generate and display the effective settings with the result.
4. Preserve current defaults for backward compatibility.
5. Add API and renderer tests for validation, request mapping, and result metadata.

**Acceptance:** T01 returns landscape; T05 can request isolated/transparent-ready
composition; each result shows model, dimensions, steps, seed, and duration.

### P0 — Compile prompts instead of forwarding raw prose

**Problem:** Negative constraints and exact-count requirements are diluted inside
long positive prompts; the current negative CLIP input is empty.

**Plan:**

1. Add a deterministic prompt compiler that separates subject, style,
   composition, must-have constraints, and avoid items.
2. Route avoid items into the negative prompt (extra objects, text, logos,
   watermark, duplicate subjects, malformed hands where relevant).
3. Repeat exact count and spatial constraints in a compact final constraint block.
4. Return both original and compiled prompts in result metadata for auditability.
5. Unit-test compilation with T01-T12 fixtures.

**Acceptance:** No hidden creative additions; compiled prompts are deterministic;
T02, T04, T05, and T07 materially improve on a fixed-seed A/B run.

### P0 — Stop pretending raster diffusion is a typography/diagram engine

**Problem:** T03 and T12 are structurally unsuitable for the current workflow.

**Plan:**

1. Detect exact-copy and diagram/topology requests before generation.
2. For badges and marketing assets, generate the background/object without text,
   then add verified text with a deterministic renderer.
3. For signal-chain diagrams, render nodes, connectors, and labels as SVG/HTML,
   using ImageGen only for optional decorative imagery.
4. Show the chosen path in the UI instead of silently using SDXL Turbo.
5. Add OCR/string-equality and graph-order assertions to the benchmark.

**Acceptance:** T03 contains all three exact lines and no others; T12 contains six
ordered nodes, one continuous left-to-right path, and readable labels.

### P1 — Introduce quality/model routing

**Problem:** Four-step SDXL Turbo is a draft workflow, but all prompts are treated
as final-quality requests.

**Plan:**

1. Keep Turbo/4-step as **Draft**.
2. Add a separately qualified **Final** workflow with more steps and an appropriate
   checkpoint/refiner available on Walter's 8 GB VRAM.
3. Route technical products, faces/hands, history, and dense compositions to Final.
4. Measure latency and VRAM; fail closed to Draft with a visible warning if Final
   is unavailable.
5. Re-run the same seeds and publish score/latency deltas.

**Acceptance:** Final mode improves artifact-control scores without GPU OOM or an
unbounded wait; draft mode stays under the current latency envelope.

### P1 — Add post-generation verification and bounded retry

**Problem:** The harness returns the first image without checking whether it met
the request.

**Plan:**

1. Add lightweight validators by task type: OCR for exact text, alpha/background
   checks for isolation, object/count checks where confidence is sufficient, and
   dimension/aspect checks for every run.
2. Score hard constraints separately from visual preference.
3. Permit at most one targeted retry that changes only the failed constraint.
4. Surface validation failures and both seeds; never claim success silently.
5. Add timeout and cancellation tests.

**Acceptance:** Hard failures are visible; retries are bounded and auditable; the
harness never reports exact-text or exact-count success without verification.

### P1 — Persist reproducible result manifests

**Problem:** The UI shows a seed, but the run lacks a durable manifest tying the
prompt, workflow, output, timings, and validation together. Local-time ordering
also became ambiguous during the daylight-saving rollback in this run.

**Plan:**

1. Give every request a UTC run ID and monotonic sequence number.
2. Save a JSON manifest beside each image: original/compiled prompts, negative
   prompt, model/checkpoint, workflow version, dimensions, steps, seed, UTC start
   and finish, duration, output filename, and validator results.
3. Use the run ID or ComfyUI sequence, never local modification time, to pair files.
4. Add an export action for a result plus manifest.

**Acceptance:** Every displayed image maps to exactly one manifest across DST,
restarts, and repeated seeds.

## Recommended delivery order

1. Structured request contract and durable manifests.
2. Prompt compiler plus negative prompt support.
3. Deterministic typography/diagram routing.
4. Draft/Final workflow qualification on Walter.
5. Validators, bounded retry, and automated benchmark command.

Do not tune the current prompt strings to hide these failures. The dominant gaps
are workflow capability and verification, not a lack of adjectives.
