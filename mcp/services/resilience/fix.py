"""Turn findings into fixes — deterministic, and only ones that are safe to make blind.

Each rule has one builder. A builder returns a :class:`~.types.Fix`:

- ``field``  — change one value (PDB ``minAvailable``, HPA ``minReplicas``,
  grace period, rollout ``maxUnavailable``). The new value keeps the old one's
  form: a percentage stays a percentage, an int stays an int, so the GitOps
  span edit (which keeps quoting) never turns ``"100%"`` into a quoted ``"2"``.
- ``new_object`` — a whole object to add (a PodDisruptionBudget).
- ``block`` — a block to add (topology spread, a preStop hook).
- ``advisory`` — everything that needs a value only a human knows (CPU
  requests, probe paths, image tags) or that isn't safe blind (raising a
  singleton's replicas). Never applied by ``--fix``.

Two routing rules apply to every fix:

- **Helm-rendered objects are advisory.** The manifest is generated; the fix
  belongs in the chart's values or templates, and KubeAstra never proposes a PR
  against rendered output.
- **A PR is offered only for a ``field`` fix whose field is already in the
  source** (static mode), because the GitOps editor replaces an existing value
  in place and cannot insert one. Live mode can't see the source, so it offers
  the change and lets ``POST /api/gitops/preview`` report a missing field.

:func:`render_fixes` turns a report into files: one merged strategic-merge
patch per object, new objects, a ``kustomization.yaml`` that lists them, and a
README with the advisory items and ``kubectl`` commands.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Callable, Optional

import yaml

from .model import Obj, Workload
from .rules import Ctx, Hit
from .types import Finding, Fix, ResilienceReport

NAMED_LISTS = ("containers", "initContainers", "env")
MARKER = ".kubeastra-fixes"

Builder = Callable[[Hit, Ctx], Optional[Fix]]


# ── helpers ───────────────────────────────────────────────────────────────────

def _target(o: Obj) -> dict[str, Any]:
    return {
        "kind": o.kind,
        "name": o.name,
        "namespace": o.namespace,
        "apiVersion": str(o.doc.get("apiVersion", "")),
        "file": o.ref.file,
        "line": o.ref.line,
    }


def _nest(path: tuple, value: Any) -> Any:
    """``("spec","template","spec","containers","app","lifecycle")`` → nested
    patch body. A key in :data:`NAMED_LISTS` is followed by the item's name and
    becomes a one-item list (strategic merge matches list items by ``name``)."""
    if not path:
        return value
    head, *rest = path
    if head in NAMED_LISTS and rest:
        name, *rest = rest
        return {head: [{"name": name, **_nest(tuple(rest), value)}]} if rest else {head: [{"name": name}]}
    return {head: _nest(tuple(rest), value)}


def _has_path(doc: Any, path: tuple) -> bool:
    node = doc
    it = iter(path)
    for key in it:
        if isinstance(node, dict) and key in NAMED_LISTS:
            node = node.get(key)
            name = next(it, None)
            node = next((i for i in (node or []) if isinstance(i, dict) and i.get("name") == name), None)
        elif isinstance(node, dict):
            if key not in node:
                return False
            node = node[key]
        else:
            return False
        if node is None:
            return False
    return True


def _patch_doc(o: Obj, body: dict) -> dict:
    meta = {"name": o.name}
    if o.namespace:
        meta["namespace"] = o.namespace
    return {"apiVersion": o.doc.get("apiVersion"), "kind": o.kind, "metadata": meta, **body}


def _dump(doc: Any) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _pod_selector(w: Workload) -> dict:
    sel = w.obj.spec.get("selector")
    if isinstance(sel, dict) and (sel.get("matchLabels") or sel.get("matchExpressions")):
        return copy.deepcopy(sel)
    return {"matchLabels": dict(w.pod_labels)}


def _workload(hit: Hit, ctx: Ctx) -> Optional[Workload]:
    obj = hit.objs[0]
    return next((w for w in ctx.inv.workloads if w.obj is obj), None)


def _field(o: Obj, path: tuple, value: Any, note: str, ctx: Ctx, reason: str) -> Fix:
    fix = Fix(
        kind="field",
        target=_target(o),
        field_path=path,
        new_value=value,
        yaml=_dump(_patch_doc(o, _nest(path, value))),
        note=note,
    )
    if not ctx.static or _has_path(o.doc, path):
        fix.gitops = {
            "kind": o.kind,
            "name": o.name,
            "namespace": o.namespace or None,
            "api_version": str(o.doc.get("apiVersion", "")) or None,
            "field_path": list(path),
            "new_value": value,
            "reason": reason,
        }
    return fix


def _block(o: Obj, body: dict, note: str) -> Fix:
    return Fix(kind="block", target=_target(o), yaml=_dump(_patch_doc(o, body)), note=note)


def _advisory(o: Obj, note: str, suggestion: Optional[dict] = None) -> Fix:
    return Fix(kind="advisory", target=_target(o), yaml=_dump(_patch_doc(o, suggestion)) if suggestion else "", note=note)


# ── builders, one per rule ────────────────────────────────────────────────────

def _res001(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    pdb = hit.objs[-1]
    total, key, form = hit.data["total"], hit.data["field"], hit.data["form"]
    if total < 2:
        return _advisory(
            pdb,
            f"With {total} replica, any PDB that protects the pod blocks every drain. Run at least 2 replicas "
            "(or raise the HPA minReplicas to 2), then `minAvailable: 1` lets one pod move at a time.",
        )
    if key == "minAvailable":
        value: Any = total - 1 if form == "int" else f"{math.floor((total - 1) * 100 / total)}%"
        note = f"minAvailable: {value} keeps {total - 1} of {total} pods up and lets a drain evict one at a time."
    else:  # maxUnavailable 0 / 0%
        value = 1 if form == "int" else f"{max(1, 100 // total)}%"
        note = f"maxUnavailable: {value} lets a drain evict one pod at a time."
    return _field(pdb, ("spec", key), value, note, ctx, f"RES001: PodDisruptionBudget blocks every eviction ({hit.message})")


def _res002(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    w = _workload(hit, ctx)
    if w is None:
        return None
    meta = {"name": f"{w.obj.name}-pdb"}
    if w.obj.namespace:
        meta["namespace"] = w.obj.namespace
    pdb = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta,
           "spec": {"maxUnavailable": 1, "selector": _pod_selector(w)}}
    return Fix(
        kind="new_object",
        target=_target(w.obj),
        yaml=_dump(pdb),
        note=(
            f"Adds a PDB for {w.kind} {w.obj.name}. It only protects anything once there are 2+ replicas: set "
            "`replicas: 2` (or an HPA minReplicas of 2) too — KubeAstra doesn't change replicas for you, because "
            "not every singleton is safe to run twice."
        ),
    )


def _res003(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    return _advisory(
        hit.objs[0],
        "Add the missing resource request(s): " + "; ".join(hit.data.get("missing", [])) + ". Base the value on "
        "observed usage (`kubectl top pod`, your metrics), not a guess — the HPA's target utilization is a "
        "percentage of it.",
    )


def _res004(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    hpa = hit.objs[-1]
    max_r = hpa.spec.get("maxReplicas")
    if isinstance(max_r, int) and max_r < 2:
        return _advisory(hpa, "maxReplicas is below 2, so the HPA can't keep 2 pods. Raise maxReplicas first.")
    return _field(hpa, ("spec", "minReplicas"), 2, "minReplicas: 2 keeps a second pod running at low traffic.", ctx,
                  "RES004: HPA can scale down to a single pod")


def _res005(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    w = _workload(hit, ctx)
    if w is None:
        return None
    keys = ["kubernetes.io/hostname"]
    if hit.data.get("zones", 0) > 1:
        keys.insert(0, "topology.kubernetes.io/zone")
    selector = _pod_selector(w)
    constraints = [
        {"maxSkew": 1, "topologyKey": k, "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": copy.deepcopy(selector)}
        for k in keys
    ]
    return _block(
        w.obj,
        {"spec": {"template": {"spec": {"topologySpreadConstraints": constraints}}}},
        "Spreads replicas across " + (" and ".join(k.split("/")[-1] + "s" for k in keys)) + ". `ScheduleAnyway` "
        "never leaves a pod Pending; switch to `DoNotSchedule` when you want a hard guarantee.",
    )


def _res006(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    w = _workload(hit, ctx)
    if w is None:
        return None
    suggestion = None
    containers = []
    for c in w.containers:
        if c.get("readinessProbe"):
            continue
        port = next((p.get("containerPort") for p in (c.get("ports") or []) if isinstance(p, dict)), None)
        if port:
            containers.append({"name": c.get("name"), "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 5}})
    if containers:
        suggestion = {"spec": {"template": {"spec": {"containers": containers}}}}
    return _advisory(
        w.obj,
        "Add a readinessProbe that checks the app can actually serve (usually an HTTP health endpoint). "
        "The suggested tcpSocket probe is only a starting point — it passes as soon as the port is open.",
        suggestion,
    )


def _res007(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    return _advisory(
        hit.objs[0],
        "Make the livenessProbe check only that the process is alive (a cheap endpoint, or a higher "
        "failureThreshold), so a slow dependency takes the pod out of rotation instead of restarting it.",
    )


def _res008(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    w = _workload(hit, ctx)
    if w is None:
        return None
    grace = w.pod_spec.get("terminationGracePeriodSeconds", 30)
    if isinstance(grace, int) and grace <= 5:
        return _advisory(w.obj, f"terminationGracePeriodSeconds is {grace}; raise it above the preStop sleep before adding one.")
    serving = [c for c in w.containers if c.get("ports")] or w.containers
    body = {"spec": {"template": {"spec": {"containers": [
        {"name": c.get("name"), "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}}} for c in serving
    ]}}}}
    return _block(
        w.obj,
        body,
        "A 5s preStop sleep lets the Service stop routing to the pod before it gets SIGTERM. Images without a "
        "`sleep` binary (distroless): on Kubernetes 1.30+ use `preStop: {sleep: {seconds: 5}}` instead.",
    )


def _res009(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    w = _workload(hit, ctx)
    if w is None:
        return None
    value = int(hit.data["sleep"]) + 10
    return _field(
        w.obj,
        ("spec", "template", "spec", "terminationGracePeriodSeconds"),
        value,
        f"terminationGracePeriodSeconds: {value} leaves 10s for the app to shut down after its {hit.data['sleep']}s preStop sleep.",
        ctx,
        f"RES009: grace period ends before the preStop sleep in '{hit.data['container']}'",
    )


def _res010(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    return _advisory(hit.objs[0], "Set requests from observed usage and a memory limit at or a little above the "
                                  "request: " + hit.message + ".")


def _res011(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    return _advisory(hit.objs[0], "Pin the image to a version tag, or better a digest (`image@sha256:…`).")


def _res012(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    w = _workload(hit, ctx)
    if w is None:
        return None
    n, form = hit.data["replicas"], hit.data["form"]
    spec = w.obj.spec
    if w.kind == "Deployment":
        rolling = (spec.get("strategy") or {}).get("rollingUpdate") or {}
        surge = rolling.get("maxSurge", "25%")
        if str(surge).strip() in ("0", "0%") and n < 2:
            return _advisory(w.obj, "maxSurge is 0, so maxUnavailable can't go lower. Allow a surge (maxSurge: 1) and "
                                    "set maxUnavailable: 0, or run 2+ replicas.")
        value: Any = "25%" if form == "pct" else (max(1, math.ceil(n / 4)) if n >= 2 else 0)
        path = ("spec", "strategy", "rollingUpdate", "maxUnavailable")
    else:  # StatefulSet: maxUnavailable must stay >= 1, and percent rounding varies by version
        if n < 2 or form == "pct":
            return _advisory(w.obj, "A StatefulSet rollout with this replica count always takes a pod down; run 2+ "
                                    "replicas and set an integer maxUnavailable below the replica count.")
        value = max(1, math.ceil(n / 4))
        path = ("spec", "updateStrategy", "rollingUpdate", "maxUnavailable")
    return _field(w.obj, path, value, f"maxUnavailable: {value} keeps most of the {n} replicas serving during a rollout.",
                  ctx, "RES012: a rollout can take every replica down at once")


def _res013(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    return _advisory(hit.objs[0], "Point the selector at the pods' labels, or delete the Service if nothing uses it.")


def _res014(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    return _advisory(hit.objs[0], "Point the selector at the workload's pod labels, or delete the leftover PDB.")


BUILDERS: dict[str, Builder] = {
    "RES001": _res001, "RES002": _res002, "RES003": _res003, "RES004": _res004, "RES005": _res005,
    "RES006": _res006, "RES007": _res007, "RES008": _res008, "RES009": _res009, "RES010": _res010,
    "RES011": _res011, "RES012": _res012, "RES013": _res013, "RES014": _res014,
}


def build_fix(hit: Hit, ctx: Ctx) -> Optional[Fix]:
    """The fix for one hit, with the Helm rule applied: a fix for a
    Helm-rendered object becomes advisory (its patch kept as the suggestion)."""
    fix = BUILDERS[hit.rule](hit, ctx)
    if fix is None:
        return None
    owner = next((o for o in hit.objs if o.kind == fix.target.get("kind") and o.name == fix.target.get("name")), hit.objs[0])
    if fix.kind != "advisory" and owner.helm_owned:
        chart = owner.labels.get("helm.sh/chart", "its chart")
        fix.kind = "advisory"
        fix.gitops = None
        fix.note = (f"Rendered by Helm ({chart}): make this change in the chart's values or templates — "
                    "KubeAstra doesn't edit generated manifests. " + fix.note)
    return fix


# ── files for `kubeastra resilience --fix DIR` ────────────────────────────────

_MERGE_KEYS = {"containers": "name", "initContainers": "name", "env": "name", "topologySpreadConstraints": "topologyKey"}


def _merge(a: Any, b: Any, key: str = "") -> Any:
    """Deep-merge two patch bodies the way strategic merge would read them:
    maps recursively, named lists by their merge key."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge(out[k], v, k) if k in out else copy.deepcopy(v)
        return out
    if isinstance(a, list) and isinstance(b, list) and key in _MERGE_KEYS:
        mk = _MERGE_KEYS[key]
        out = [copy.deepcopy(i) for i in a]
        for item in b:
            idx = next((n for n, i in enumerate(out) if isinstance(i, dict) and isinstance(item, dict) and i.get(mk) == item.get(mk)), None)
            if idx is None:
                out.append(copy.deepcopy(item))
            else:
                out[idx] = _merge(out[idx], item)
        return out
    return copy.deepcopy(b)


def _slug(t: dict) -> str:
    return "-".join(p for p in (t["kind"].lower(), t.get("namespace") or "", t["name"]) if p)


def _kubectl_target(t: dict) -> str:
    return f"{t['kind'].lower()} {t['name']}" + (f" -n {t['namespace']}" if t.get("namespace") else "")


def render_fixes(report: ResilienceReport) -> dict[str, str]:
    """``{relative path: content}`` for every unsuppressed finding's fix. Pure —
    the CLI decides where (and whether) to write."""
    findings: list[Finding] = [f for f in report.findings if f.fix is not None and not f.suppressed]
    patches: dict[str, tuple[dict, dict, list[str]]] = {}
    new_objects: dict[str, tuple[str, list[str]]] = {}
    advisory: list[Finding] = []
    for f in findings:
        fix = f.fix
        if fix.kind in ("field", "block"):
            doc = yaml.safe_load(fix.yaml)
            path = f"patches/{_slug(fix.target)}.yaml"
            if path in patches:
                merged, target, rules = patches[path]
                patches[path] = (_merge(merged, doc), target, rules + [f.rule])
            else:
                patches[path] = (doc, fix.target, [f.rule])
        elif fix.kind == "new_object":
            obj = yaml.safe_load(fix.yaml)
            path = f"new/{_slug({'kind': obj['kind'], 'namespace': obj['metadata'].get('namespace', ''), 'name': obj['metadata']['name']})}.yaml"
            new_objects[path] = (fix.yaml, [f.rule])
        else:
            advisory.append(f)

    files: dict[str, str] = {MARKER: "Written by `kubeastra resilience --fix`. Safe to delete.\n"}
    for path, (doc, _t, rules) in sorted(patches.items()):
        files[path] = f"# kubeastra resilience: {', '.join(rules)}\n" + _dump(doc)
    for path, (text, rules) in sorted(new_objects.items()):
        files[path] = f"# kubeastra resilience: {', '.join(rules)}\n" + text
    if patches or new_objects:
        kust = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                "resources": sorted(new_objects), "patches": [{"path": p} for p in sorted(patches)]}
        files["kustomization.yaml"] = (
            "# Add your manifests (or your base) to `resources:` and build with `kubectl kustomize`.\n" + _dump(kust)
        )
    files["README.md"] = _readme(report, patches, new_objects, advisory)
    return files


def _readme(report, patches, new_objects, advisory) -> str:
    lines = [
        "# Resilience fixes",
        "",
        f"Generated by `kubeastra resilience` for a {report.source_mode} scan "
        f"(score {report.score if report.score is not None else 'n/a'}, grade {report.grade}). "
        "Review every change before you apply it.",
        "",
    ]
    if patches:
        lines += ["## Patches", "",
                  "Strategic-merge patches, one per object. Use them from a Kustomize overlay "
                  "(`kustomization.yaml` lists them), or apply one to a cluster you manage directly:", ""]
        for path, (_doc, t, rules) in sorted(patches.items()):
            lines.append(f"- `{path}` ({', '.join(rules)}): `kubectl patch {_kubectl_target(t)} --type strategic --patch-file {path}`")
        lines.append("")
    if new_objects:
        lines += ["## New objects", ""]
        for path, (_text, rules) in sorted(new_objects.items()):
            lines.append(f"- `{path}` ({', '.join(rules)}): `kubectl apply -f {path}`")
        lines.append("")
    notes = [f for f in report.findings if f.fix is not None and not f.suppressed and f.fix.kind != "advisory"]
    if notes:
        lines += ["## What each change does", ""]
        lines += [f"- **{f.rule}** {f.fix.target['kind']} {f.fix.target['name']}: {f.fix.note}" for f in notes]
        lines.append("")
    if advisory:
        lines += ["## Needs you", "", "These need a value only you know, or aren't safe to change blind. Nothing below is applied.", ""]
        for f in advisory:
            t = f.fix.target
            lines.append(f"- **{f.rule}** {t['kind']} {t['name']}: {f.fix.note}")
            if f.fix.yaml:
                lines += ["", "  Suggested starting point:", "", "  ```yaml"]
                lines += [f"  {line}" for line in f.fix.yaml.rstrip().splitlines()]
                lines += ["  ```", ""]
        lines.append("")
    if not (patches or new_objects or advisory):
        lines += ["Nothing to fix.", ""]
    return "\n".join(lines)
