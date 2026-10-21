"""tool-guardian-setup refuses to add or import tool-guardian into its own config, because the router
would start itself as a backend. It used to decide that by substring over the command and every
argument, so a server whose PATH argument merely contained "tool-guardian" (a filesystem server rooted
in a clone of this repo) was refused as "tool-guardian itself". Fails on main before the fix."""
import pytest

import tg_setup


@pytest.mark.parametrize("spec", [
    {"command": "tool-guardian", "args": []},
    {"command": "/home/user/.venv/bin/tool-guardian", "args": ["--config", "x.json"]},
    {"command": "C:\\Tools\\tool-guardian.exe", "args": []},
    {"command": "uvx", "args": ["tool-guardian", "--config", "x.json"]},
    {"command": "uvx", "args": ["tool-guardian@0.7.0"]},
    {"command": "pipx", "args": ["run", "tool-guardian==0.7.0"]},
    {"command": "python", "args": ["/home/user/src/tool-guardian/tool_guardian.py"]},
    {"command": "python", "args": ["-m", "tool_guardian"]},
    {"command": "npx", "args": ["-y", "dsh-tool-guardian"]},
    {"command": "TOOL-GUARDIAN", "args": []},
])
def test_tool_guardian_itself_is_recognised(spec):
    assert tg_setup.is_self(spec)


@pytest.mark.parametrize("spec", [
    {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/tool-guardian"]},
    {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/tool-guardian/docs"]},
    {"command": "uvx", "args": ["mcp-server-git", "--repository", "C:\\src\\tool-guardian"]},
    {"command": "python", "args": ["/home/user/tool-guardian-notes/server.py"]},
    {"command": "node", "args": ["server.js"], "env": {"ROOT": "/srv/tool_guardian"}},
])
def test_a_server_that_only_mentions_the_name_in_a_path_is_not_self(spec):
    assert not tg_setup.is_self(spec)
