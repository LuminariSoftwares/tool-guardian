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

## Things to Know

tool-guardian launches MCP servers you configure with the environment you provide, so treat its configuration file like a secrets file and never paste it into a public issue or report. Archived tool results (spill files) may contain whatever the tools returned. Before sharing diagnostic output publicly:
- Review the configuration for exposed credentials
- Check spill files for sensitive data from tool invocations
- Redact personal information and API keys

If you need to share diagnostic output for a bug report, review it carefully first.
