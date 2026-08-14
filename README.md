# AI Lean Generate

`ai-lean-generate` is a GitHub composite action that asks Claude Code or Codex to
add focused Lean files to a project and then independently compiles them. The
agent cannot modify committed project files.

## What it does

0. Posts a progress comment on the source pull request and keeps editing it
   through the run, including a live view of the agent's own task list.
1. Installs the toolchain from `lean-toolchain` and runs `lake build`.
2. Scans committed sources for `sorry`/`admit`: with `deps-sorry-policy: warn`
   pre-existing placeholders are annotated and reported to the agent as
   baseline context; with `reject` they fail the run before the agent starts.
3. Collects the changed Lean source plus configured context.
4. Gives Claude Code or Codex a constrained task to add one or more Lean files.
5. Lets the agent run only a credential-scrubbing Lean wrapper.
6. Confirms that `HEAD` and all tracked files are unchanged.
7. Marks untracked additions as intent-to-add, discovers generated files with
   `git diff --diff-filter=A`, and rejects non-Lean additions.
8. Rejects unsafe escape hatches in every generated Lean source.
9. Runs `lake env lean` on every generated file and reruns `lake build`.
10. On verification failure with generated files present, optionally asks the
    agent once (yes/no) whether the partial result is worth publishing, and
    exposes the answer as the `publish-on-failure` output.
11. Uploads the generated files and diagnostics as `ai-lean-generate`.

## Claude Code with a GitHub environment

GitHub environments are selected on the caller's **job**, not inside a
composite action. If an environment named `main` contains an API-key secret
named `CLAUDE_CODE_KEY`, use:

```yaml
name: AI Lean Generate

on:
  pull_request:
    paths:
      - "**/*.lean"
      - "lakefile.toml"
      - "lean-toolchain"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  lean-ai:
    runs-on: ubuntu-latest
    environment: main
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2
      - uses: eyalk11/ai-lean-generate@main
        with:
          provider: claude-code
          source-paths: |
            **/*.lean
          context-files: |
            lakefile.toml
            lean-toolchain
          deps-sorry-policy: reject
        env:
          ANTHROPIC_API_KEY: ${{ secrets.CLAUDE_CODE_KEY }}
```

`CLAUDE_CODE_KEY` is the repository/environment secret's name. It is mapped to
`ANTHROPIC_API_KEY` because that is the variable consumed by the upstream
Claude Code action. For a long-lived Claude Code OAuth credential instead:

```yaml
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

The environment may require approval before the job starts, depending on its
GitHub protection rules.

## Codex

```yaml
      - uses: eyalk11/ai-lean-generate@main
        with:
          provider: codex
          imports: |
            MyProject
          task: Generate examples that exercise the declarations changed by this PR.
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

## Configuration

All composite-action inputs are strings. Write booleans as `"true"` or
`"false"`.

| Input | Default | Meaning |
|---|---|---|
| `provider` | `claude-code` | Required mode: `claude-code` or `codex`; every other value fails |
| `model` | Claude: `claude-sonnet-4-6`; Codex: upstream default | Optional explicit model ID |
| `source-paths` | `*.lean`, `**/*.lean` | Newline-separated Git pathspecs used for diff collection and placeholder scanning |
| `context-files` | empty | Newline-separated globs for additional read-only prompt context |
| `imports` | empty | Newline-separated modules the generated file must import |
| `task` | Generate meaningful compile-time checks | Extra generation instructions |
| `agent-max-turns` | `20` | Maximum Claude Code turns; ignored by Codex |
| `output-file` | empty | Legacy optional single target; empty lets the agent choose filenames |
| `target-files` | empty | Optional requested project files; empty lets the agent choose filenames |
| `verification-command` | empty | Additional shell verification run after mandatory checks with provider and GitHub credentials removed |
| `setup-lean` | `"true"` | Run `leanprover/lean-action@v1` |
| `build-project` | `"true"` | Run the initial `lake build` when Lean setup is enabled |
| `use-mathlib-cache` | `auto` | Passed to `leanprover/lean-action` |
| `upload-artifact` | `"true"` | Upload generated source, prompt, and diagnostics |
| `artifact-name` | `ai-lean-generate` | Artifact name; use a run-specific value when a separate publishing job downloads it |
| `base-sha` | PR base or `HEAD^` | Explicit base revision for the Lean diff |
| `head-sha` | PR head or `HEAD` | Explicit head revision for the Lean diff |
| `max-context-bytes` | `200000` | Legacy context cap used only if `max-input-tokens` is empty |
| `max-input-tokens` | `50000` | Approximate prompt-context cap, estimated conservatively at four UTF-8 bytes per token |
| `max-output-tokens` | `128000` | Per-response output-token cap, passed to the agent as `CLAUDE_CODE_MAX_OUTPUT_TOKENS`; the CLI's own default of 32000 fails long runs with `response exceeded the 32000 output token maximum` |
| `max-repair-attempts` | `2` | Reserved for compatibility; coding agents repair within their own turns |
| `deps-sorry-policy` | `warn` | `warn` reports pre-existing placeholders (annotations, plus prompt context for out-of-dependency ones); `reject` fails the run on any of them |
| `sorry-allowed-files` | `**/*_deps.lean` | Newline-separated dependency-file globs; under `warn` the agent prompt offers them as the sanctioned home for unavoidable placeholders |
| `pr-number` | empty | Source pull request that receives the live progress comment; empty disables progress reporting |
| `github-token` | empty | Token used only to post and edit that comment; needs `pull-requests: write` and is never exposed to the agent |
| `progress-comment` | `"true"` | Post one comment when the run starts and edit it in place through every phase, mirroring the agent's own task list |
| `ask-publish-on-failure` | `"true"` | On verification failure with generated files, query the agent once whether the partial result is worth a PR; strict final-word yes/no, anything unclear is no; claude-code only |

Committed sources are scanned for `sorry`/`admit` before the agent runs. With
`deps-sorry-policy: warn` (the default) every finding becomes a warning
annotation, findings outside `sorry-allowed-files` are additionally written
into the agent prompt as project baseline it must not extend, and the run
continues — like a failing project build, pre-existing placeholders are the
context the agent needs, not a reason to refuse to run it. With `reject`, any
finding fails the run before the agent starts. Either way, the verifier
rejects `sorry`/`admit` outside `sorry-allowed-files` in every file the agent
adds or edits.

Suggested dependency naming:

```text
Lean/theorem_3_11_deps.lean
Lean/theorem_3_11.lean
```

## Additional Lean setup steps

Add arbitrary setup steps to the caller workflow before `ai-lean-generate`. If
those steps install the toolchain and build the project, set `setup-lean:
"false"` so the composite action does not repeat that work:

```yaml
      - uses: actions/checkout@v4
      - name: Project-specific setup
        run: ./scripts/prepare-lean-project.sh
      - uses: eyalk11/ai-lean-generate@main
        with:
          provider: claude-code
          setup-lean: "false"
        env:
          ANTHROPIC_API_KEY: ${{ secrets.CLAUDE_CODE_KEY }}
```

This deliberately keeps arbitrary shell commands in the visible caller
workflow instead of accepting an opaque command string as an action input.

## Credentials and isolation

The caller passes exactly one provider credential:

| Provider | Caller environment variable |
|---|---|
| Claude Code API key | `ANTHROPIC_API_KEY` |
| Claude Code OAuth | `CLAUDE_CODE_OAUTH_TOKEN` |
| Codex | `OPENAI_API_KEY` |

The agent steps explicitly clear `GITHUB_TOKEN` and `GH_TOKEN`. The generated
Lean wrapper clears provider credentials, GitHub tokens, Actions runtime
tokens, and OIDC request credentials before invoking Lean. The action also
removes the checkout token persisted in the repository's Git HTTP configuration
before the agent starts. Claude Code gets
only read/edit tools plus that wrapper; Codex uses a workspace-write sandbox
with sudo disabled. After the agent finishes, an independent verifier rejects
any tracked-file or commit change.

The optional `verification-command` runs last, after each generated file and
`lake build` pass. It receives the same scrubbed environment and
cannot reuse the checkout authorization header:

```yaml
        with:
          verification-command: |
            lake test
            ./scripts/check-generated-proof.sh
```

Repository secrets are not provided to workflows triggered from untrusted
forks. Use an environment approval rule, restrict the job to trusted branches,
or skip agent jobs for forked pull requests.

## Failure and branch protection

The job fails when:

- the provider is not `claude-code` or `codex`;
- credentials are absent or invalid;
- the initial project build fails;
- committed sources contain `sorry` or `admit` and `deps-sorry-policy` is
  `reject`;
- the agent changes tracked files or `HEAD`;
- generated code uses a forbidden construct;
- the agent adds a non-Lean project file;
- a requested target file is missing;
- any generated file or final `lake build` does not compile.

The check still fails in all of these cases. But when verification failed and
generated files exist (a compile failure, not a safety rejection — safety
rejections never expose generated files), `ask-publish-on-failure: "true"`
additionally asks the agent once, with read-only tools, whether the partial
result is worth a reviewer's time. The strict final-word YES/NO lands in the
`publish-on-failure` output; the reusable PR workflow uses it to open a pull
request clearly marked as an unverified partial result, and anything unclear
counts as no.

Repository administrators can bypass a required check only if the branch rules
or ruleset permits bypass. GitHub can be configured to forbid administrator
bypass.

## Live progress on the source pull request

A run takes as long as Lean setup, a long agent session, and independent
verification take together, and until it finished the pull request said nothing
at all — a running job and a job that was never triggered looked identical.

Give the action `pr-number` and a `github-token` with `pull-requests: write` and
it posts one comment as soon as the work starts, then edits that same comment
through every phase. Editing rather than appending is deliberate: reviewers get
one notification, and what the comment says is always current instead of a
column of stale updates.

```yaml
      - uses: eyalk11/ai-lean-generate@main
        with:
          pr-number: "123"
          github-token: ${{ github.token }}
```

The comment carries the phase table (prepare, agent, verification, publish),
the provider, model, head revision and turn budget, the agent's token use and
cost once known, and a link to the run. While the agent is working it also
mirrors the agent's own `TodoWrite` list, checkboxes and all, so a reviewer can
watch which proof it is on. The task-list instruction is added to the agent
prompt only when reporting is enabled, and `TodoWrite` is in the agent's tool
allow-list for the same reason.

Live mirroring works because Claude Code appends its session transcript as it
runs, under the sandbox `HOME` inside `RUNNER_TEMP`. A watcher process outside
the sandbox tails that file. The base action's execution log is written only
after Claude exits, so it serves as the end-of-run fallback rather than the
live source. Codex has no equivalent task list; it gets the phase table only.

The token never reaches the agent. The sandbox runs with `--clearenv`, the
agent steps blank `GITHUB_TOKEN` and `GH_TOKEN`, and only the progress steps
receive `AI_LEAN_PROGRESS_TOKEN`. Every failure in the reporting path is a
warning annotation: a GitHub API problem cannot fail a generation run.

Later jobs — publishing, failure reporting — update the same comment through
the small `progress` action, finding it by a hidden marker that carries the
workflow run id, and restoring the accumulated state from a hidden block in the
comment body:

```yaml
      - uses: eyalk11/ai-lean-generate/progress@main
        with:
          github-token: ${{ github.token }}
          pr-number: "123"
          phase: publish
          status: ok
          detail: ${{ steps.publish.outputs.pull-request-url }}
```

The reusable workflow wires all of this up already; set `progress-comment:
false` to turn it off.

## Publish verified files

Publishing is a separate action so the coding agent and verifier never receive
write-capable GitHub credentials:

```yaml
      - name: Generate and verify
        id: lean
        uses: eyalk11/ai-lean-generate@main
        # provider inputs and credential environment omitted

      - name: Publish verified additions
        if: steps.lean.outcome == 'success'
        uses: eyalk11/ai-lean-generate/publish@main
        with:
          github-token: ${{ github.token }}
          generated-files: ${{ steps.lean.outputs.generated-files }}
          base-branch: feature-branch
          source-pr: "123"
```

The publish action independently confirms that every supplied or discovered
path is safe, exists, is an added `.lean` file in Git diff, and that no tracked
file was changed. It stages only those verified files, disables Git hooks for
commit and push, pushes a new branch, and opens a pull request. Run it in a
fresh job and fresh checkout after downloading the verifier artifact; never
give the generation job write permissions.

## Complete reusable PR workflow

Other repositories can call the complete isolated workflow as one caller job.
The reusable workflow infers the PR number for `pull_request` callers; manual
callers pass `pr-number`.

```yaml
jobs:
  lean:
    permissions:
      contents: write
      pull-requests: write
    uses: eyalk11/ai-lean-generate/.github/workflows/lean-pr.yml@main
    with:
      pr-number: ${{ inputs.pr_number }}
      environment-name: main
      setup-command: |
        lake update
        lake exe cache get
        lake build
      source-paths: |
        Lean/*.lean
        proofs/*.md
      imports: MyProject
      verification-command: lake build
```

`setup-command` contains all caller-specific preparation commands and runs only
in the read-only generation job. The caller appears as one job, but the
reusable workflow internally isolates PR inspection, AI generation, trusted
publishing, and failure reporting on separate runners.

By default (`cache: true`) the generation job caches `.lake` and `~/.elan`
with `actions/cache`, keyed on `lean-toolchain` and `lake-manifest.json`
(`cache-paths` / `cache-key-files` override both), so repeated runs skip most
of the toolchain download, `lake exe cache get`, and project build. The cache
is saved after setup but before the agent runs, so it never contains
artifacts of generated modules and a failed run still warms the next one. A
key-file change starts a fresh cache; older caches prefix-match and seed an
incremental build. For zero-copy warm state instead of a cache, point
`runner` at a self-hosted runner — GitHub-hosted runners are ephemeral, so
disk state never survives between runs there.

For cross-repository use, the calling repository must allow the public reusable
workflow and grant the shown token permissions. Prefer a release tag or commit
SHA over `@main`. Repository or organization secrets may be passed explicitly;
an `environment-name` attaches the caller repository's environment only to the
generation job.

## Local checks

```bash
python -m unittest discover -s tests -v
lake build
```
