# Persistent model context settings

## Scope and acceptance

The operator needs an installed, tool-capable small model with more context without
increasing the memory demand of every existing model. The approved first slice is
Qwen3.5 4B at 32,768 tokens, retaining the existing 16,384-token request default.
Use the normal installed launcher and the existing router environment interface;
do not change credentials, tool grants, node services or other installed models.

Acceptance: private settings survive a normal launcher restart; missing or invalid
files preserve existing defaults; explicit environment settings win; the selected
model appears in inventory and completes a bounded selected-tool run at 32K.
A 32K allocation and a small successful run do not prove full-window recall,
maximum-length throughput, or H9 qualification.

## Operator settings

The Mac launcher reads optional `model-context.json` in the DaveLLM data directory
(`~/Library/Application Support/DaveLLM` by default). The file contains no secrets:

```json
{
  "max_context": 32768,
  "default_context": 16384,
  "models": {"qwen3.5:4b": 32768}
}
```

The values map to `DAVE_CHAT_NUM_CTX`, `DAVE_MODEL_CONTEXT_DEFAULT` and
`DAVE_MODEL_CONTEXT_WINDOWS`, respectively. Each existing nonempty environment
value takes precedence over its file value. Both plain chat and tool runs use
the model window capped by the maximum; the model's own trained limit still
applies. Context includes instructions, tool descriptions, conversation and reply.

All three keys are required; unknown keys are rejected. The maximum is an integer
from 8,192 through 262,144; default and model windows are integers from 4,096 through
that maximum. Model IDs must be nonempty strings. The file must be owned by the
current user, not writable by group/others, no larger than 16 KiB and not a symlink.
Invalid files are skipped with a fixed warning, never their contents.

Restart using the existing installed launcher only when requests are idle. This
file does not download models, unload runners, modify Ollama defaults or start
inference. Verify the actual context using Ollama's running-model inventory after
a bounded test. Increasing the maximum allocates additional memory and must be
matched to available RAM/VRAM, not just the advertised model window.

Rollback: move this optional file out of the data directory and restart through
the normal launcher. Environment overrides, if present, continue to win. Keep
existing models installed when trying a new one.

## Verified first configuration

On 2026-10-07, Qwen3.5 4B (Q4_K_M) was installed on Walter and selected in the
authenticated browser. A normal installed-launcher restart read back a 32,768
maximum, 16,384 fallback and the Qwen 32K override. The private file is owner-only.

A bounded selected `calc.eval` run completed in two model steps with one tool
call, zero errors and final answer `108`. The prompt specified the exact arithmetic
arguments; this is a smoke check, not an unprompted tool-quality benchmark.
Ollama's loaded-model inventory reported context length 32,768 and identical total
and GPU allocation of 4,098,894,068 bytes. This establishes 32K allocation and a
small working tool flow, not tested retrieval across a full 32K conversation.
