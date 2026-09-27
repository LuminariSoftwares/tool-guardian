# Contributing to tool-guardian

## Welcome

This project exists to make local models usable in long coding sessions by keeping tool definitions out of the context window. The most valuable contributions are bug reports with real numbers—your model name, backend, context size, and token counts before and after tool management.

## Ways to Help

- **Bug reports** - Include logs showing the issue with real data
- **Benchmark results** - Share performance numbers from your hardware and model setup
- **Documentation fixes** - Improve README, docstrings, or examples
- **Code improvements** - Bug fixes and feature enhancements with tests

## Development Setup

Create and activate a virtual environment, then install dependencies:

**Windows (cmd):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements-dev.txt
npm install
```

**macOS/Linux:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
npm install
```

## Running the Tests

Run the Python test suite:
```
python -m pytest -q
```

Run the Node.js linting and checks:
```
npm run check
```

Test the DSH smoke tests:
```
node tests/dsh_smoke.mjs
```

Test the Python-to-Node bridge:
```
python modules/tg_bridge.py --selftest
```

All tests must pass before submitting a pull request.

## Pull Request Checklist

Before submitting, ensure:
- All tests pass (pytest and npm commands above)
- Add an entry under `## Unreleased` in CHANGELOG.md
- No secrets, API tokens, or personal file paths in code, tests, or logs
- README is updated if behavior changed

## Coding Style

- Prefer the Python and Node standard libraries
- Avoid new runtime dependencies without discussion
- Fail open: if the router breaks, pass traffic through untouched rather than break the user's session
- Keep original tool definitions recoverable—every transformation must preserve what was changed
- Clear variable names and comments for complex logic

## License

tool-guardian is released under the MIT License. Any contributions you make will be licensed under the same license.
