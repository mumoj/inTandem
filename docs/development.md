# Developing inTandem

Install the development dependencies (including the optional `claude` extra so the Claude Code
backend is type-checked and tested), then run the checks:

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run pyright                    # strict type checking
uv run pytest -m "not e2e"        # fast unit tests (no external binaries needed)
uv run pytest                     # unit tests + OpenCode E2E when OpenCode is on PATH
```

## Test suites

### Unit tests (`uv run pytest -m "not e2e"`)

All unit tests run offline with no external processes:

- **Supervisor, bridge, CLI, MCP, worktrees, storage, notifier**: tested against `FakeOpenCode`
  (`tests/fakes.py`), ASGI transport, `respx`, and real temporary Git repositories.
- **OpenCode 1.x (`tests/test_client.py`, `tests/test_translate.py`)**: HTTP mapping via `respx`
  and event translation to `BackendEvent`.
- **OpenCode 2.x (`tests/test_opencode2.py`)**: HTTP mapping via `respx`, plus event translation
  replaying real SSE streams recorded from OpenCode `2.3.2` (`tests/fixtures/opencode2`).
- **Auto-installer (`tests/test_install.py`)**: platform/CPU/libc selection, `SHA256SUMS`
  verification, atomic extraction, and `INTANDEM_NO_INSTALL`.
- **Claude Code (`tests/test_claude.py`)**: drives `ClaudeCodeBackend` with an in-memory
  `FakeSDKClient` using the real `claude-agent-sdk` message and permission dataclasses, covering
  streaming turns, `PreToolUse` protected-file guards, `AskUserQuestion`, aborts/connect races,
  dashboard diff/command metadata, git diffs, and full-stack hub/bridge integration.

### OpenCode E2E tests (`tests/test_e2e.py`, `tests/test_hub.py`)

The OpenCode E2E tests start a real `opencode serve` with the scripted OpenAI-compatible model in
`tests/scripted_llm.py`. They run automatically whenever OpenCode 1.x or 2.x is on `PATH`. To
target a specific binary (for example, testing both 1.x and 2.x side by side):

```bash
INTANDEM_TEST_OPENCODE=/path/to/opencode uv run pytest -m e2e
```

The scripted model adapts its tool calls to the tools the server advertises (2.x renamed `bash`
to `shell` and `filePath` to `path`), so the same suite covers both majors: protected edits,
questions/forms, corrections, a second spec, shell edits caught by the hash guard, and supervisor
restart recovery.

### Claude Code E2E smoke test (`tests/test_claude_e2e.py`)

Opt-in because it runs against your own Claude subscription (`claude auth login`):

```bash
INTANDEM_E2E_CLAUDE=1 uv run pytest tests/test_claude_e2e.py -v
```

This verifies a real turn creating a file and the `PreToolUse` guard blocking and rejecting an
edit to a protected file.

## Release

GitHub Actions publishes releases to PyPI. Update `version` in `pyproject.toml` and
`__version__` in `src/intandem/__init__.py`, then create and push a matching tag:

```bash
git tag v0.1.3 && git push origin v0.1.3
```

`.github/workflows/publish.yml` builds with `uv` and publishes through PyPI's GitHub Action
using Trusted Publishing. The action attaches a publish attestation to each distribution. PyPI
does not allow a version to be published twice, so publish a new version number to correct a
release.
