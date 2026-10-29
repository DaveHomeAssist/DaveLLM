# DL-CLI-01: Repeatable inference command

## Problem and outcome

The existing project-context CLI operates BRAIN state and has no inference command.
The running router already exposes stateless `POST /eval/chat`. Provide one small
command client that reuses that route and the existing configured node inventory.
The operator can repeat a text request without creating a conversation or enabling
tools. Dave directly authorized this slice on October 28, 2026.

## Flow and smallest slice

1. Read the prompt and explicit node/model selection from command arguments.
2. Use `DAVE_API_KEY`, or the existing macOS launcher Keychain item when unset.
3. Check the selected model using the router's existing inventory route.
4. Send one bounded, stateless completion and print its answer. Offer JSON output
   for callers that need the router's existing metadata.
5. Return a nonzero exit code for invalid input, authentication, unavailable
   inventory, timeout, empty content or a truncated completion. Never retry a
   completion automatically.

## Boundaries and effects

- Python standard library only. No package, service, endpoint or network change.
- This local command accepts loopback router origins only, bypasses environment
  proxies and refuses redirects so credentials stay on the local router path.
- No tools, shell execution by the model, chat history, project context, images,
  streaming, automatic model selection, model download or service lifecycle work.
- Default maximum output is 128 tokens, temperature 0 and client timeout 180 seconds.
  A client timeout does not prove that the router cancelled an in-flight request.
- The BRAIN CLI and backend implementation remain unchanged.
- Install the tested client as a user command without modifying PATH. Preserve any
  existing command at the intended destination instead of overwriting it.

## Acceptance and verification

Offline checks cover request/auth handling, inventory rejection, timeout,
redirect refusal and invalid or incomplete replies. They start no server.
The live acceptance is two serial executions of the same command through the
existing router and the already verified model, capped at 64 output tokens each.
Both must return the requested exact marker and exit 0. Confirm the router remains
healthy afterward. This proves command repeatability, not general model quality,
tool qualification, UI behavior, or restart persistence.

## Dependencies and risks

The router must already be running, the existing credential must be available,
and the explicitly selected model must be installed on a reachable configured
node. Model loading can dominate first-response latency. No fallback to a new
service, another model or sustained Mac inference is authorized by this slice.

## Acceptance observed on October 28, 2026

+ 34 targeted offline tests passed on the Mac in 0.31 seconds.
+ The installed client was byte-identical to source. Both serial executions used
  the existing Keychain item, existing router and explicitly selected installed
  model, with a maximum of 64 output tokens each. They returned the exact requested
  marker and exit 0 in 5.263 and 1.174 seconds.
+ Mac: client and authentication. Selected GPU node: inference. No services or
  network settings changed. Full required CI remains a separate source-delivery gate.

## Source delivery and FIFO repair

The initial PR #104 run on `548cf83` reported 1488 passed, one skipped and one
failed per Python version. Git's initial discovery could consult
`objects/info/alternates` before DaveLLM checked it, causing a FIFO read to reach
the Git timeout. Baseline main at `7e4a1f3` failed the same assertion. These runs
are retained as historical evidence below.

The narrow fix subsequently landed in PR #103 at `14a241d` and is integrated into
this branch by `50c86c5`. It preflights direct alternates before Git starts, removes
`--git-path objects` from discovery and derives the primary object store from the
validated common Git directory. Existing admission checks remain in place.

On October 28, 37 targeted checks passed on the Mac in 13.97 seconds, including
FIFO refusal, ordinary repository access, outside-root refusal and the CLI cases.
Local Apple Git is 2.54.0; the required GitHub Python 3.12, 3.13 and 3.14 jobs cover
the Git 2.56.0 environment that exposed the failure. Dave explicitly authorized
merging PR #104 once these protected checks pass. The PR is the authoritative
source for current check and merge confirmation. No service or network change
is included. The running router and installed client remain unchanged.

- [Current delivery record](https://github.com/DaveHomeAssist/DaveLLM/pull/104)
- [Merged FIFO fix](https://github.com/DaveHomeAssist/DaveLLM/pull/103)
- [Historical PR failure](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/40790985850)
- [Historical baseline failure](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/40774741767)
