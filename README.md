# inTandem

**A harness for pairing coding agents: one model plans, another builds, and the planner reviews the result.**

Your planner (Claude Code or Codex) writes a spec. inTandem hands it to the model you choose in
[OpenCode](https://opencode.ai), watches the work, and tells the planner exactly why it stopped.
It checks what the implementer did instead of taking its word, and you can step in from a browser
or your phone.

inTandem runs as an MCP server on top of these tools, so it works with them rather than replacing them.

<p align="center">
  <img src="https://raw.githubusercontent.com/mumoj/inTandem/main/docs/overview.svg" width="900"
       alt="A planner sends a spec to inTandem, which prompts the implementer model in OpenCode. It edits your repo, and inTandem returns a verified result. You can allow or deny requests from a dashboard or phone.">
</p>

## Why use it

- **Choose who does what.** Pick the planner, then pick the implementer from the models available in OpenCode.
- **Trust the result.** A result only counts if it's fresh, and you review the real diff.
- **Protect what matters.** Mark files as off limits. inTandem blocks edits and catches any that slip through.
- **Never get stuck.** You get an alert when the implementer needs you, stalls or goes quiet.
- **Work in parallel.** Run several implementers at once, each on its own branch.

## Install

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and OpenCode 1.x.

```bash
npm i -g opencode-ai@1          # OpenCode 2 isn't supported yet
opencode auth login             # connect a model provider
uv tool install intandem        # puts `inTandem` on your PATH
```

## Connect your planner

Register inTandem once:

```bash
claude mcp add inTandem --scope user -- inTandem mcp      # Claude Code
codex mcp add inTandem -- inTandem mcp                    # Codex
```

There's nothing else to start. inTandem runs when your planner connects and stops when it exits.

## Run your first task

Ask your planner to delegate something:

> Have a cheaper model write the tests for citations.py through inTandem.

It writes the spec, waits for the work and reviews the result. Its first reply includes a
**dashboard link**, where you can watch live and approve or deny requests.

To set a default model or protect files for every task, add them when you register:

```bash
claude mcp add inTandem --scope user \
  -e INTANDEM_MODEL=openrouter/qwen/qwen3-coder \
  -e 'INTANDEM_PROTECTED=["instruction.md"]' \
  -- inTandem mcp
```

## Step in from a terminal

The same controls work from the command line, including over SSH from your phone:

```bash
inTandem status      # what it's doing right now
inTandem pending     # what it's waiting on
inTandem allow       # or: inTandem deny --message "leave that file alone"
inTandem cost        # what the implementer has spent
```

## Learn more

| | |
| --- | --- |
| [Usage guide](https://github.com/mumoj/inTandem/blob/main/docs/usage.md) | Parallel implementers, the CLI, exit codes, alerts and configuration |
| [How it works](https://github.com/mumoj/inTandem/blob/main/docs/architecture.md) | Architecture, what it enforces and design notes |
| [Development](https://github.com/mumoj/inTandem/blob/main/docs/development.md) | Running the tests and releasing |
| [Planner skill](https://github.com/mumoj/inTandem/blob/main/skills/intandem-planner/SKILL.md) | The planner loop as an Agent Skill |
