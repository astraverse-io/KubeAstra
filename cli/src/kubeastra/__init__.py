"""KubeAstra CLI — AI-powered Kubernetes troubleshooting from the terminal.

The CLI is a thin HTTP client over the KubeAstra backend. It streams the
same ReAct investigation you'd see in the web UI, rendered as terminal
badges and a colored root-cause panel that matches the design shown on
https://kubeastra.io.

Not a replacement for the web UI or MCP server — an additional entry
point. See ``cli/README.md`` for install + quickstart.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
