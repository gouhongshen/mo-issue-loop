# MatrixOne Challenger Runner

Runner infrastructure for independent black-box testing of MatrixOne issue fixes.

## Execution Model

- The local issue loop manages assignments and the Challenger agent.
- Ephemeral GitHub Actions runners host the exact MatrixOne build under test.
- The Challenger uses private connectivity to execute SQL tests and collect evidence.
- Restricted lifecycle controls support node termination, restart, and diagnostics.
- Each test run records source identity and preserves its results before cleanup.

Keep model credentials, private issue-loop configuration, and production data out of
this repository and runner artifacts.

## Lifecycle

One `workflow_dispatch` job hosts serial tasks for at most 340 minutes, measured
from its first step, within GitHub's 360-minute job limit. The local SQLite pool
persists dispatch intent before calling GitHub, reconciles unknown outcomes, and
holds a process lock for each task. No requests means no new VM. An existing VM
retains its caches across idle periods until its lifetime ends. Insufficient time
or disk headroom drains it before another request is admitted. Failed startups
retry with 5-minute to 1-hour backoff; a changed workflow revision can retry sooner.

A task reserves build + test + 15 minutes, plus 5 minutes of runner cleanup
headroom. A 30-second heartbeat renews ownership, not its absolute deadline;
orphaned tasks expire after 3 minutes. The task UUID and random lease token fence
stale commands. The runner cannot return to IDLE until containers and task data
are removed. Only compiler/module caches survive between tasks.

## Isolation

- The Mac's Tailscale installation, routes, DNS and device identity are unchanged.
- The trusted main-branch workflow authenticates through Tailscale OIDC with an
  exact subject and workflow claim, and only `auth_keys` scope for its own tag.
- Before Tailscale starts, host firewall rules reject outbound tailnet connections
  and allow only the configured controller IPv4 to the tunnel-only SSH port.
  Tailscale netfilter management is disabled on this VM so it cannot override these
  rules. Existing tailnet policy is not rewritten.
- SSH pins each host key from the authenticated run's endpoint artifact. Its key
  cannot obtain a shell, run arbitrary commands or forward arbitrary ports.
- The broker has separate controller and task credentials. No model credentials
  reach GitHub. Test SQL ports are only published to loopback and SSH forwarding.
- Candidate code builds in unprivileged, capability-free containers with 4 CPU,
  12 GiB RAM and no Docker socket or host credentials. Container egress to private
  networks, metadata endpoints and the tailnet is blocked.
- Public artifacts contain endpoint identity or image digests, never test logs,
  source snapshots, task tokens or model history. Test evidence returns locally.
- Only the repository owner may dispatch the workflow on main. Fork/PR events do
  not trigger it. All third-party Actions are pinned to commits.

## Configuration

Repository variables: `TS_CLIENT_ID`, `TS_AUDIENCE`, `TS_TAG`, `CONTROLLER_IP`,
and `RUNNER_IMAGE`. The image must be an immutable
`ghcr.io/gouhongshen/mo-issue-loop/base@sha256:...` reference.

Read the repository's actual OIDC subject prefix before configuring Tailscale:

```sh
gh api repos/gouhongshen/mo-issue-loop/actions/oidc/customization/sub
```

This repository uses immutable subjects. Match its returned `sub_claim_prefix`
plus `:ref:refs/heads/main`, not the old name-only subject. Also require the
`workflow_ref` claim to equal
`gouhongshen/mo-issue-loop/.github/workflows/challenger.yml@refs/heads/main`.
Grant only writable `auth_keys` scoped to `tag:mo-challenger`.
See the [GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc).

Build the image with `gh workflow run build-image.yml`; use the `image-digest`
artifact to set `RUNNER_IMAGE`. The image contains only tools and this public
runtime, never a candidate MO binary. CGO libraries and the exact uploaded source
are rebuilt for each task. Shared Go caches accelerate later builds on that VM;
candidate compiler caches are not published to Actions caches or container images.

The existing issue loop opts in with
`ISSUE_FIX_CHALLENGER_ACTIONS_RUNNER_ROOT=/absolute/path/to/mo-issue-loop`.
This explicitly selects Actions instead of the dedicated SSH host; failure does
not select another backend. The Challenger model and evidence validation remain
local and unchanged.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 scripts/exercise.py --worktree /path/to/matrixone \
  --state /private/path/runner-pool --output /private/path/smoke-evidence
```

The opt-in live test creates two fresh environments on one VM, checks cross-CN
SQL, abruptly terminates/restarts CN1, checks survivor availability, and proves
the second task has no database left by the first. It retains local receipts and
build/start timings. These are infrastructure smoke tests, not evidence that any
particular issue is fixed. Empty, uninitialized Git submodule placeholders have
no source bytes and are excluded from the content hash and archive, just like
absent placeholders. Populated submodules and source symlinks remain explicitly
unsupported until a safe manifest contract is implemented.
