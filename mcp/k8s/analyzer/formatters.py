"""Formatter helpers for health analyzer results."""

from typing import Any, Dict
from .models import AnalyzerResult


def format_analyzer_summary(result: AnalyzerResult) -> str:
    """Format an AnalyzerResult object into a clear human-readable summary string."""
    scope = result.scope
    scope_type = scope.get("type", "cluster")
    ns = scope.get("namespace")
    scope_desc = f"namespace '{ns}'" if ns else scope_type

    if result.finding_count == 0:
        return f"### Kubernetes Health Analysis ({scope_desc})\n\nNo critical findings or health warnings detected."

    summary = result.summary
    lines = [
        f"### Kubernetes Health Analysis ({scope_desc})",
        f"**Summary**: {summary.get('critical', 0)} Critical, {summary.get('warning', 0)} Warning, {summary.get('info', 0)} Info\n",
    ]

    for f in result.findings:
        sev_icon = "🔴" if f.severity == "critical" else "⚠️" if f.severity == "warning" else "ℹ️"
        lines.append(f"#### {sev_icon} [{f.severity.upper()}] {f.title}")
        lines.append(f"**Summary**: {f.summary}")
        
        if f.probable_cause:
            lines.append(f"**Probable Cause**: {f.probable_cause}")

        if f.evidence:
            lines.append("**Evidence**:")
            for ev in f.evidence:
                ev_str = f"  - `{ev.source}`: "
                if ev.field and ev.value:
                    ev_str += f"{ev.field} = {ev.value}"
                elif ev.message:
                    ev_str += ev.message
                lines.append(ev_str)

        if f.recommended_next_steps:
            lines.append("**Recommended Next Steps**:")
            for step in f.recommended_next_steps:
                lines.append(f"  - {step}")

        if f.suggested_read_only_commands:
            lines.append("**Suggested Diagnostic Commands**:")
            for cmd in f.suggested_read_only_commands:
                lines.append(f"  - `{cmd}`")

        lines.append("")  # blank separator

    return "\n".join(lines)
