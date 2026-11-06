# Developing inTandem

Install the development dependencies, then run the checks:

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run pyright                    # strict type checking
uv run pytest                     # unit tests, plus E2E tests when OpenCode 1.x is on PATH
```

The end-to-end tests start an actual `opencode serve` process with the scripted
OpenAI-compatible model in `tests/scripted_llm.py`. The script controls the model's choices;
OpenCode's permission system and event stream still run normally. The suite covers protected
edits, questions, corrections, a second spec, shell edits detected by the hash guard, and a
supervisor restart during a spec.

MCP tools are tested against a mocked bridge in `tests/test_mcp.py`. Parallel implementer
worktrees are tested against an actual Git repository in `tests/test_worktrees.py`.

## Release

GitHub Actions publishes releases to PyPI. Update `version` in `pyproject.toml` and
`__version__` in `src/intandem/__init__.py`, then create and push a matching tag:

```bash
git tag v0.1.2.1 && git push origin v0.1.2.1
```

`.github/workflows/publish.yml` builds with `uv` and publishes through PyPI's GitHub Action
using Trusted Publishing. The action attaches a publish attestation to each distribution. PyPI
does not allow a version to be published twice, so publish a new version number to correct a
release.
