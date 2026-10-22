# Security Policy

## Supported Versions

Security updates are provided for:
- The latest release on npm (`dsh-tool-guardian`)
- The latest release on PyPI (`tool-guardian`)

Older versions are not supported. Please update to the latest release to receive security fixes.

## Reporting a Vulnerability

Do not open a public issue to report a security vulnerability.

Use GitHub's private vulnerability reporting at: https://github.com/LuminariSoftwares/tool-guardian/security/advisories/new

When reporting, please include:
- The version you are running (npm and/or PyPI version)
- Your installation path and setup
- Steps to reproduce the vulnerability
- Expected vs. actual behavior
- The security impact (data exposure, session hijacking, etc.)

## Response Timeline

As a volunteer-maintained project, we make a best-effort commitment to:
- Acknowledge receipt of your report within 7 days
- Assess the vulnerability and work on a fix
- Release a patched version as soon as feasible

## Backends Are Trusted Code

tool-guardian starts the MCP servers in its configuration as child processes, with the environment
you give them, and relays tool calls to them. It is a router, not a sandbox: **a server listed in
the config can do anything the user running tool-guardian can do.** There is no allow-list of
backend tools, no argument filtering beyond the server's own JSON Schema, and no attempt to contain
a backend. Only list servers you would run by hand.

Two consequences worth stating plainly:

- **Config files are executable input.** Adding a server is equivalent to running its command.
  Treat `~/.tool-guardian/mcp.json` as you would a shell script that also holds secrets.
- **A config in the current directory is not loaded without consent** (0.8.0). A
  repository you cloned can carry `./mcp.json` or `./.mcp.json`; up to 0.7.0, starting the router in that
  directory launched that repository's servers, with no approval prompt from your client. Those
  paths are now used only when you name one (`--config`, `TOOL_GUARDIAN_CONFIG`) or set
  `TOOL_GUARDIAN_PROJECT_CONFIG=1`, and a skipped one is reported on stderr. `--config` is still a
  path you chose: pointing it at an untrusted file starts untrusted servers.

The archive of shortened results (`~/.tool-guardian/spill/`, directory `0700`, files `0600`), the
call log (`calls.jsonl`, `0600`) and `mcp.json` / `state.json` (`0600`) are owner-readable only. On
Windows, POSIX modes do not apply and these files inherit the directory's ACL.

## What Leaves The Machine

The router makes **no network connections of its own** to serve a request. It speaks stdio to its
client and stdio to the servers it started; your prompts, tool arguments and tool results never
leave the machine because of tool-guardian. (A *backend* server may of course talk to the network —
that is its job, and it is one more reason the point above matters.)

There is exactly one exception, and it is the only outbound request in this project:

| What | An update check: one HTTPS GET of `https://pypi.org/pypi/tool-guardian/json` (Python) or `https://registry.npmjs.org/-/package/dsh-tool-guardian/dist-tags` (the DSH bundle) |
|---|---|
| When | at most **once a day per install, per machine**, and only from `--selftest`, `doctor` or the bundle's report — never during a tool call |
| What is sent | the request itself: the package name in the URL, and your HTTP client's default headers. No identifier, no config, no telemetry, no usage data |
| What is stored | a local timestamp and the version seen, so the next run does not ask again |
| Turning it off | set `GUARDIAN_NO_UPDATE_CHECK=1` (or `NO_UPDATE_NOTIFIER=1`, or `CI` — so it never runs in CI). Failure is silent; the line reads "could not check" |

## Things to Know

tool-guardian launches MCP servers you configure with the environment you provide, so treat its configuration file like a secrets file and never paste it into a public issue or report. Archived tool results (spill files) may contain whatever the tools returned. Before sharing diagnostic output publicly:
- Review the configuration for exposed credentials
- Check spill files for sensitive data from tool invocations
- Redact personal information and API keys

If you need to share diagnostic output for a bug report, review it carefully first.
