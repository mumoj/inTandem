# Developing inTandem

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run pyright                    # strict
uv run pytest                     # unit tests, plus e2e when OpenCode 1.x is on PATH
```

The end-to-end tests run a real `opencode serve` against a scripted OpenAI-compatible model
in `tests/scripted_llm.py`. Only the model's choices are scripted; OpenCode's permission
system and event stream are the real thing. They cover a protected edit stopped and rejected,
a question answered, a correction applied, a second spec, a shell edit caught by the hash
guard, and a supervisor restart mid-spec. The MCP tools are tested against a mocked bridge in
`tests/test_mcp.py`, and implementer worktrees against a real git repository in
`tests/test_worktrees.py`.

## Releasing

Releases go to PyPI from GitHub Actions, with no token stored anywhere. Bump `version` in
`pyproject.toml` and `__version__` in `src/intandem/__init__.py`, then push a matching tag:

```bash
git tag v0.1.1 && git push origin v0.1.1
```

`.github/workflows/publish.yml` builds the package and publishes it. A version can only be
published once, so a bad release needs a new version number.
