# inTandem

inTandem is an MCP server and CLI for pairing coding agents: one agent (such as Claude Code or Codex) writes specifications and reviews results, while another (running on [OpenCode](https://opencode.ai) or Claude Code) writes the code.

When the planner submits a specification, inTandem starts or reuses a supervised implementer session and monitors it. If the implementer asks for permission, asks a question, stalls while reasoning without calling tools, or stops responding, inTandem surfaces that to the planner and to a local web dashboard. You can also approve, deny, or inspect sessions from the command line (including over SSH). Once the implementer finishes, inTandem returns its result and changed files to the planner for verification and optional merging.

<p align="center">
  <img src="https://raw.githubusercontent.com/mumoj/inTandem/main/docs/overview.svg" width="900"
       alt="Overview diagram: the planner sends a specification to inTandem, which drives an implementer on OpenCode or Claude Code. inTandem returns results and diffs to the planner for review, and exposes controls via a web dashboard or CLI.">
</p>

## What it does

- Runs alongside Claude Code or Codex as an MCP server, letting the planning model delegate implementation to OpenCode (1.x or 2.x) or Claude Code.
- Tracks each specification in its own session and only accepts result files matching the active spec ID that were written after the latest prompt, alongside the git diff for verification.
- Protects configured file globs from edits using backend permission rules and periodic content-hash checks in case an edit bypasses tool permissions.
- Detects and reports permission requests, questions, reasoning stalls, and unresponsive tool calls to both the planner and the dashboard.
- Supports running multiple implementers in parallel by giving each one its own Git branch and worktree until its changes are reviewed and merged or discarded.

## Installation

inTandem runs on Linux, macOS, and WSL (native Windows is not supported yet; on Windows, install inside [WSL](https://learn.microsoft.com/windows/wsl/install)). It requires Python 3.12–3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install intandem        # installs `inTandem` (and downloads OpenCode if missing)
opencode auth login             # authenticate with a model provider
```

To use Claude Code as the implementer instead of OpenCode:

```bash
uv tool install 'intandem[claude]'
claude auth login
```

## Connecting a planner

Register inTandem as an MCP server with your planner:

```bash
claude mcp add inTandem --scope user -- inTandem mcp      # Claude Code
codex mcp add inTandem -- inTandem mcp                    # Codex
```

The MCP server starts on demand when the planner connects and shuts down when it exits, so you do not need to run a background daemon.

## Running a task

Ask your planner to delegate work through inTandem, for example:

> Have an implementation model write the tests for citations.py through inTandem.

The planner writes a specification, submits it, waits for the implementer to finish, and reviews the resulting files. Its first reply includes a local **dashboard link** where you can follow progress and handle permission or question prompts.

To configure defaults such as the implementer model, harness, or protected file globs across tasks, pass environment variables when registering the MCP server:

```bash
claude mcp add inTandem --scope user \
  -e INTANDEM_MODEL=openrouter/qwen/qwen3-coder \
  -e 'INTANDEM_PROTECTED=["instruction.md"]' \
  -- inTandem mcp
```

Use `-e INTANDEM_HARNESS=claude` to make Claude Code the default implementer. See the [usage guide](https://github.com/mumoj/inTandem/blob/main/docs/usage.md) for all configuration options.

## Command-line usage

The CLI provides the same inspection and approval commands as the dashboard, which is useful over SSH:

```bash
inTandem status      # show current spec and activity
inTandem pending     # list pending permissions and questions
inTandem allow       # or: inTandem deny --message "leave that file alone"
inTandem cost        # show token usage and cost per spec
```

## Architecture

<p align="center">
  <img src="https://raw.githubusercontent.com/mumoj/inTandem/main/docs/architecture.svg" width="1000"
       alt="Architecture diagram: the planner, CLI, and browser talk to the HTTP bridge in the inTandem hub. The bridge calls the supervisor, which drives the selected backend (OpenCode or Claude Code), while background tasks pump events, run periodic checks, and watch backend health.">
</p>

## Documentation

| | |
| --- | --- |
| [Usage guide](https://github.com/mumoj/inTandem/blob/main/docs/usage.md) | Harnesses, parallel implementers, CLI commands, exit codes, alerts, and configuration |
| [Architecture](https://github.com/mumoj/inTandem/blob/main/docs/architecture.md) | Component overview, invariants, and design notes |
| [Development](https://github.com/mumoj/inTandem/blob/main/docs/development.md) | Running tests and cutting releases |
| [Planner skill](https://github.com/mumoj/inTandem/blob/main/skills/intandem-planner/SKILL.md) | Planner workflow packaged as an Agent Skill |
