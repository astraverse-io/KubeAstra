"use client";

import { useState, useEffect } from "react";
import { AlertTriangle, Play, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import type { TabId } from "./Sidebar";
import { SlideToConfirm } from "./SlideToConfirm";

interface Props {
  tab: TabId;
  onResult: (r: Record<string, unknown>) => void;
  onLoading: (v: boolean) => void;
  onError: (e: string | null) => void;
}

// ── Shared primitives ─────────────────────────────────────────────────────────

function Label({ children }: { children: React.ReactNode }) {
  return <label style={{ display: "block", fontSize: "0.75rem", fontWeight: 500, color: "var(--ink-3)", marginBottom: "0.375rem" }}>{children}</label>;
}

function Input({
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <input
      name={placeholder || "tool-input"}
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="sym-input"
      style={{ width: "100%", fontSize: "0.875rem" }}
    />
  );
}

function TextArea({
  value,
  onChange,
  placeholder,
  rows = 8,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  rows?: number;
}) {
  return (
    <textarea
      name={placeholder || "tool-textarea"}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      rows={rows}
      className="sym-input"
      style={{ width: "100%", fontSize: "0.875rem", fontFamily: "var(--mono)", resize: "vertical" }}
    />
  );
}

function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      name="tool-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="sym-input"
      style={{ width: "100%", fontSize: "0.875rem" }}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function Toggle({
  label,
  value,
  onChange,
}: {
  label: string;
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" }}>
      <div
        onClick={() => onChange(!value)}
        style={{
          width: "2.25rem", height: "1.25rem", borderRadius: "9999px", transition: "background-color 0.15s",
          backgroundColor: value ? "var(--brand)" : "var(--paper-3)", position: "relative"
        }}
      >
        <span
          style={{
            position: "absolute", top: "0.125rem", left: "0.125rem", width: "1rem", height: "1rem",
            borderRadius: "50%", backgroundColor: "var(--paper)", transition: "transform 0.15s",
            transform: value ? "translateX(1rem)" : "translateX(0)"
          }}
        />
      </div>
      <span style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>{label}</span>
    </label>
  );
}

function SubmitButton({
  loading,
  disabled,
  label = "Run",
}: {
  loading: boolean;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="submit"
      disabled={loading || disabled}
      className="sym-btn-primary"
      style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.875rem", padding: "0.5rem 1rem" }}
    >
      {loading ? (
        <RefreshCw size={14} style={{ animation: "spin 1s linear infinite" }} />
      ) : (
        <Play size={14} />
      )}
      {loading ? "Running..." : label}
    </button>
  );
}

// ── Tab forms ─────────────────────────────────────────────────────────────────

function AnalyzeTab({ onResult, onLoading, onError }: Props) {
  const [tool, setTool] = useState("analyze");
  const [errorText, setErrorText] = useState("");
  const [toolType, setToolType] = useState("kubernetes");
  const [env, setEnv] = useState("production");
  const [category, setCategory] = useState("");
  const [namespace, setNamespace] = useState("");
  const [resourceName, setResourceName] = useState("");
  const [eventsText, setEventsText] = useState("");
  const [errors, setErrors] = useState("");
  const [loading, setLoading] = useState(false);
  const [categories, setCategories] = useState<Record<string, string>>({});

  useEffect(() => {
    api.categories().then((d) => setCategories(d as Record<string, string>)).catch(() => {});
  }, []);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    onLoading(true);
    onError(null);
    try {
      let result: unknown;
      if (tool === "analyze") result = await api.analyze({ error_text: errorText, tool: toolType, environment: env });
      else if (tool === "fix") result = await api.fix({ error_text: errorText || undefined, category: category || undefined, tool: toolType, namespace: namespace || "<namespace>", resource_name: resourceName || "<name>" });
      else if (tool === "runbook") result = await api.runbook({ error_text: errorText || undefined, category: category || undefined, tool: toolType });
      else if (tool === "report") result = await api.report({ events_text: eventsText });
      else result = await api.summary({ errors: errors.split("\n").filter(Boolean), tool: toolType });
      onResult(result as Record<string, unknown>);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setLoading(false);
      onLoading(false);
    }
  };

  return (
    <form onSubmit={run} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <Label>Tool</Label>
        <Select
          value={tool}
          onChange={setTool}
          options={[
            { value: "analyze", label: "Analyze Error (AI + RAG)" },
            { value: "fix", label: "Get Fix Commands" },
            { value: "runbook", label: "Generate Runbook" },
            { value: "report", label: "Cluster Report (paste events)" },
            { value: "summary", label: "Error Summary (batch)" },
          ]}
        />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "0.75rem" }}>
        <div>
          <Label>Tool type</Label>
          <Select
            value={toolType}
            onChange={setToolType}
            options={[
              { value: "kubernetes", label: "Kubernetes" },
              { value: "ansible", label: "Ansible" },
              { value: "helm", label: "Helm" },
            ]}
          />
        </div>
        {tool === "analyze" && (
          <div>
            <Label>Environment</Label>
            <Select
              value={env}
              onChange={setEnv}
              options={[
                { value: "production", label: "Production" },
                { value: "staging", label: "Staging" },
                { value: "dev", label: "Dev" },
              ]}
            />
          </div>
        )}
      </div>

      {(tool === "fix" || tool === "runbook") && (
        <div>
          <Label>Category (or leave blank to auto-detect)</Label>
          <select
            name="error-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="sym-input"
            style={{ width: "100%" }}
          >
            <option value="">-- auto-detect from error text --</option>
            {Object.entries(categories).map(([k, v]) => (
              <option key={k} value={k}>{k} — {v}</option>
            ))}
          </select>
        </div>
      )}

      {tool === "fix" && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "0.75rem" }}>
          <div>
            <Label>Namespace</Label>
            <Input value={namespace} onChange={setNamespace} placeholder="my-namespace" />
          </div>
          <div>
            <Label>Resource name</Label>
            <Input value={resourceName} onChange={setResourceName} placeholder="my-pod-xyz" />
          </div>
        </div>
      )}

      {tool === "report" ? (
        <div>
          <Label>Paste kubectl events output</Label>
          <TextArea
            value={eventsText}
            onChange={setEventsText}
            placeholder={"kubectl get events --all-namespaces --sort-by='.lastTimestamp'"}
            rows={10}
          />
        </div>
      ) : tool === "summary" ? (
        <div>
          <Label>Errors (one per line)</Label>
          <TextArea
            value={errors}
            onChange={setErrors}
            placeholder={"Error 1...\nError 2...\nError 3..."}
            rows={8}
          />
        </div>
      ) : (
        <div>
          <Label>Error text</Label>
          <TextArea
            value={errorText}
            onChange={setErrorText}
            placeholder="Paste your Kubernetes or Ansible error here..."
            rows={10}
          />
        </div>
      )}

      <SubmitButton loading={loading} disabled={!errorText && !eventsText && !errors && !category} />
    </form>
  );
}

function InvestigateTab({ onResult, onLoading, onError }: Props) {
  const [tool, setTool] = useState("investigate");
  const [namespace, setNamespace] = useState("");
  const [podName, setPodName] = useState("");
  const [labelSelector, setLabelSelector] = useState("");
  const [workloadName, setWorkloadName] = useState("");
  const [tail, setTail] = useState("200");
  const [previous, setPrevious] = useState(false);
  const [useAi, setUseAi] = useState(true);
  const [loading, setLoading] = useState(false);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    onLoading(true);
    onError(null);
    try {
      let result: unknown;
      if (tool === "investigate") result = await api.investigate({ namespace, pod_name: podName, tail: parseInt(tail), use_ai: useAi });
      else if (tool === "pods") result = await api.pods({ namespace, label_selector: labelSelector || undefined });
      else if (tool === "describe") result = await api.describe({ namespace, pod_name: podName });
      else if (tool === "logs") result = await api.logs({ namespace, pod_name: podName, previous, tail: parseInt(tail) });
      else if (tool === "events") result = await api.events({ namespace });
      else result = await api.find({ name: workloadName });
      onResult(result as Record<string, unknown>);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setLoading(false);
      onLoading(false);
    }
  };

  return (
    <form onSubmit={run} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <Label>Tool</Label>
        <Select
          value={tool}
          onChange={setTool}
          options={[
            { value: "investigate", label: "Investigate Pod (full triage + AI)" },
            { value: "pods", label: "List Pods" },
            { value: "describe", label: "Describe Pod" },
            { value: "logs", label: "Get Pod Logs" },
            { value: "events", label: "Get Events" },
            { value: "find", label: "Find Workload" },
          ]}
        />
      </div>

      {tool === "find" ? (
        <div>
          <Label>Workload name</Label>
          <Input value={workloadName} onChange={setWorkloadName} placeholder="my-service" />
        </div>
      ) : (
        <div>
          <Label>Namespace</Label>
          <Input value={namespace} onChange={setNamespace} placeholder="prod" />
        </div>
      )}

      {["investigate", "describe", "logs"].includes(tool) && (
        <div>
          <Label>Pod name</Label>
          <Input value={podName} onChange={setPodName} placeholder="my-app-7d4f9b-xyz" />
        </div>
      )}

      {tool === "pods" && (
        <div>
          <Label>Label selector (optional)</Label>
          <Input value={labelSelector} onChange={setLabelSelector} placeholder="app=my-app" />
        </div>
      )}

      {["investigate", "logs"].includes(tool) && (
        <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
          <div style={{ flex: 1 }}>
            <Label>Tail lines</Label>
            <Input value={tail} onChange={setTail} placeholder="200" type="number" />
          </div>
          {tool === "logs" && (
            <div style={{ marginTop: "1.25rem" }}>
              <Toggle label="Previous container" value={previous} onChange={setPrevious} />
            </div>
          )}
          {tool === "investigate" && (
            <div style={{ marginTop: "1.25rem" }}>
              <Toggle label="AI analysis" value={useAi} onChange={setUseAi} />
            </div>
          )}
        </div>
      )}

      <SubmitButton loading={loading} disabled={!namespace && !workloadName} />
    </form>
  );
}

function ClusterTab({ onResult, onLoading, onError }: Props) {
  const [tool, setTool] = useState("deployment");
  const [namespace, setNamespace] = useState("");
  const [deploymentName, setDeploymentName] = useState("");
  const [serviceName, setServiceName] = useState("");
  const [loading, setLoading] = useState(false);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    onLoading(true);
    onError(null);
    try {
      let result: unknown;
      if (tool === "deployment") result = await api.deployment({ namespace, deployment_name: deploymentName });
      else if (tool === "service") result = await api.service({ namespace, service_name: serviceName });
      else if (tool === "endpoints") result = await api.endpoints({ namespace, service_name: serviceName });
      else result = await api.rolloutStatus({ namespace, deployment_name: deploymentName });
      onResult(result as Record<string, unknown>);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setLoading(false);
      onLoading(false);
    }
  };

  return (
    <form onSubmit={run} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <Label>Tool</Label>
        <Select
          value={tool}
          onChange={setTool}
          options={[
            { value: "deployment", label: "Get Deployment" },
            { value: "rollout", label: "Rollout Status" },
            { value: "service", label: "Get Service" },
            { value: "endpoints", label: "Get Endpoints" },
          ]}
        />
      </div>
      <div>
        <Label>Namespace</Label>
        <Input value={namespace} onChange={setNamespace} placeholder="prod" />
      </div>
      {["deployment", "rollout"].includes(tool) ? (
        <div>
          <Label>Deployment name</Label>
          <Input value={deploymentName} onChange={setDeploymentName} placeholder="my-deployment" />
        </div>
      ) : (
        <div>
          <Label>Service name</Label>
          <Input value={serviceName} onChange={setServiceName} placeholder="my-service" />
        </div>
      )}
      <SubmitButton loading={loading} disabled={!namespace} />
    </form>
  );
}

function MulticlusterTab({ onResult, onLoading, onError }: Props) {
  const [tool, setTool] = useState("list");
  const [contextName, setContextName] = useState("");
  const [sshConn, setSshConn] = useState("");
  const [sshPassword, setSshPassword] = useState("");
  const [loading, setLoading] = useState(false);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    onLoading(true);
    onError(null);
    try {
      let result: unknown;
      if (tool === "list") result = await api.contexts();
      else if (tool === "current") result = await api.currentContext();
      else if (tool === "switch") result = await api.switchContext(contextName);
      else result = await api.addContext({
        ssh_connection: sshConn,
        password: sshPassword || undefined,
        context_name: contextName || undefined,
      });
      onResult(result as Record<string, unknown>);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setLoading(false);
      onLoading(false);
    }
  };

  return (
    <form onSubmit={run} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <Label>Tool</Label>
        <Select
          value={tool}
          onChange={setTool}
          options={[
            { value: "list", label: "List Contexts" },
            { value: "current", label: "Current Context" },
            { value: "switch", label: "Switch Context" },
            { value: "add", label: "Add Context via SSH" },
          ]}
        />
      </div>
      {tool === "switch" && (
        <div>
          <Label>Context name</Label>
          <Input value={contextName} onChange={setContextName} placeholder="my-cluster" />
        </div>
      )}
      {tool === "add" && (
        <>
          <div>
            <Label>SSH connection (user@hostname)</Label>
            <Input value={sshConn} onChange={setSshConn} placeholder="ansible@k8s-master.example.com" />
          </div>
          <div>
            <Label>SSH password (optional, blank = key-based)</Label>
            <Input value={sshPassword} onChange={setSshPassword} type="password" placeholder="Leave blank for key-based auth" />
          </div>
        </>
      )}
      <SubmitButton loading={loading} />
    </form>
  );
}

function RecoveryTab({ onResult, onLoading, onError }: Props) {
  const [tool, setTool] = useState("restart");
  const [namespace, setNamespace] = useState("");
  const [podName, setPodName] = useState("");
  const [deploymentName, setDeploymentName] = useState("");
  const [command, setCommand] = useState("");
  const [replicas, setReplicas] = useState("1");
  const [patch, setPatch] = useState("");
  const [resourceType, setResourceType] = useState("deployment");
  const [resourceName, setResourceName] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [loading, setLoading] = useState(false);

  const executeAction = async (isConfirmed: boolean = confirm) => {
    setLoading(true);
    onLoading(true);
    onError(null);
    try {
      let result: unknown;
      if (tool === "restart") result = await api.restart({ namespace, deployment_name: deploymentName, confirm: isConfirmed });
      else if (tool === "scale") result = await api.scale({ namespace, deployment_name: deploymentName, replicas: parseInt(replicas), confirm: isConfirmed });
      else if (tool === "delete") result = await api.deletePod({ namespace, pod_name: podName, confirm: isConfirmed });
      else if (tool === "exec") result = await api.exec({ namespace, pod_name: podName, command, confirm: isConfirmed });
      else result = await api.patch({ namespace, resource_type: resourceType, resource_name: resourceName, patch, confirm: isConfirmed });
      onResult(result as Record<string, unknown>);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setLoading(false);
      onLoading(false);
    }
  };

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    await executeAction(confirm);
  };

  return (
    <form onSubmit={run} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {/* Warning banner */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: "0.75rem", borderRadius: "0.5rem", border: "1px solid var(--amber-bd)", backgroundColor: "var(--amber-bg)", padding: "0.75rem 1rem" }}>
        <AlertTriangle size={16} color="var(--amber)" style={{ marginTop: "0.125rem", flexShrink: 0 }} />
        <div>
          <p style={{ fontSize: "0.75rem", fontWeight: 500, color: "var(--amber)", margin: 0 }}>Write Operations</p>
          <p style={{ fontSize: "0.75rem", color: "var(--ink-2)", margin: "0.125rem 0 0 0" }}>
            These tools modify cluster state. You must toggle Confirm before running.
            Requires <code style={{ fontFamily: "var(--mono)" }}>ENABLE_RECOVERY_OPERATIONS=true</code> in the backend .env.
          </p>
        </div>
      </div>

      <div>
        <Label>Operation</Label>
        <Select
          value={tool}
          onChange={setTool}
          options={[
            { value: "restart", label: "Rollout Restart (deployment)" },
            { value: "scale", label: "Scale Deployment" },
            { value: "delete", label: "Delete Pod" },
            { value: "exec", label: "Exec Command in Pod" },
            { value: "patch", label: "Apply JSON Patch" },
          ]}
        />
      </div>

      <div>
        <Label>Namespace</Label>
        <Input value={namespace} onChange={setNamespace} placeholder="prod" />
      </div>

      {["restart", "scale"].includes(tool) && (
        <div>
          <Label>Deployment name</Label>
          <Input value={deploymentName} onChange={setDeploymentName} placeholder="my-deployment" />
        </div>
      )}

      {["delete", "exec"].includes(tool) && (
        <div>
          <Label>Pod name</Label>
          <Input value={podName} onChange={setPodName} placeholder="my-pod-7d4f9b-xyz" />
        </div>
      )}

      {tool === "scale" && (
        <div>
          <Label>Replicas</Label>
          <Input value={replicas} onChange={setReplicas} type="number" placeholder="3" />
        </div>
      )}

      {tool === "exec" && (
        <div>
          <Label>Command</Label>
          <Input value={command} onChange={setCommand} placeholder="ls -lh /var/lib/postgresql/" />
        </div>
      )}

      {tool === "patch" && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "0.75rem" }}>
            <div>
              <Label>Resource type</Label>
              <Select
                value={resourceType}
                onChange={setResourceType}
                options={[
                  { value: "deployment", label: "deployment" },
                  { value: "statefulset", label: "statefulset" },
                  { value: "pod", label: "pod" },
                  { value: "service", label: "service" },
                  { value: "configmap", label: "configmap" },
                ]}
              />
            </div>
            <div>
              <Label>Resource name</Label>
              <Input value={resourceName} onChange={setResourceName} placeholder="my-deployment" />
            </div>
          </div>
          <div>
            <Label>JSON patch</Label>
            <TextArea
              value={patch}
              onChange={setPatch}
              placeholder={'{"spec":{"template":{"spec":{"containers":[{"name":"app","resources":{"limits":{"memory":"1Gi"}}}]}}}}'}
              rows={5}
            />
          </div>
        </>
      )}

      <div style={{ paddingTop: "1rem", borderTop: "1px solid var(--rule)" }}>
        <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", marginBottom: "0.5rem" }}>
          Confirm write operation (Slide or Button fallback)
        </p>
        <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
          <div style={{ flex: 1, minWidth: "200px" }}>
            <SlideToConfirm
              disabled={!namespace}
              label="Slide to confirm and run"
              confirmedLabel="Running..."
              onConfirm={() => {
                setConfirm(true);
                executeAction(true);
              }}
            />
          </div>
          <SubmitButton
            loading={loading}
            disabled={!namespace}
            label="Run fallback"
          />
        </div>
      </div>
    </form>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────

export default function ToolForm(props: Props) {
  const { tab } = props;
  if (tab === "analyze") return <AnalyzeTab {...props} />;
  if (tab === "investigate") return <InvestigateTab {...props} />;
  if (tab === "cluster") return <ClusterTab {...props} />;
  if (tab === "multicluster") return <MulticlusterTab {...props} />;
  if (tab === "recovery") return <RecoveryTab {...props} />;
  return null;
}
