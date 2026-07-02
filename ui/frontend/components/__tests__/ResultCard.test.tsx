import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ResultCard from "../ResultCard";
import { RootCauseCard } from "../RootCauseCard";
import { SlideToConfirm } from "../SlideToConfirm";

describe("ResultCard Kubernetes renderers", () => {
  it("renders Helm release inventory without exposing the envelope JSON", () => {
    render(
      <ResultCard
        tool="list_helm_releases"
        result={{
          verdict: "n/a",
          evidence: {
            type: "inventory",
            items: [
              {
                available: true,
                namespace: "monitoring",
                status_filter: null,
                release_count: 2,
                releases: [
                  {
                    name: "grafana-operator",
                    namespace: "monitoring",
                    revision: "1",
                    status: "deployed",
                    chart: "grafana-operator-v5.20.0",
                    app_version: "v5.20.0",
                  },
                  {
                    name: "kube-prometheus",
                    namespace: "monitoring",
                    revision: "1",
                    status: "deployed",
                    chart: "kube-prometheus-9.5.7",
                    app_version: "0.75.1",
                  },
                ],
              },
            ],
            filter_criteria: { namespace: "monitoring" },
          },
          raw_excerpt: "{\"available\": true, \"namespace\": \"monitoring\"}",
        }}
      />,
    );

    expect(screen.getByText("Helm Releases")).toBeInTheDocument();
    expect(screen.getByText(/Found 2 Helm releases/)).toBeInTheDocument();
    expect(screen.getByText("grafana-operator")).toBeInTheDocument();
    expect(screen.getByText("kube-prometheus")).toBeInTheDocument();
    expect(screen.getByText("grafana-operator-v5.20.0")).toBeInTheDocument();
    expect(screen.queryByText(/raw_excerpt/)).not.toBeInTheDocument();
    expect(screen.queryByText(/"available"/)).not.toBeInTheDocument();
  });

  it("renders get_helm_release sections without dumping envelope JSON", () => {
    render(
      <ResultCard
        tool="get_helm_release"
        result={{
          verdict: "n/a",
          evidence: {
            type: "inventory",
            items: [
              {
                release: "jenkins-legacy",
                namespace: "ci",
                revision: null,
                found: true,
                sections: {
                  status: { status: "deployed", chart: "jenkins", chart_version: "5.8.92", app_version: "2.452.1", revision: 7 },
                  history: [
                    { revision: 6, status: "superseded", chart: "jenkins-5.8.90", updated: "2026-05-01" },
                    { revision: 7, status: "deployed", chart: "jenkins-5.8.92", updated: "2026-06-01" },
                  ],
                  values: "controller:\n  tag: 2.452.1\n  adminPassword: ***redacted***\n",
                },
                errors: {},
              },
            ],
          },
          raw_excerpt: "{\"release\": \"jenkins-legacy\"}",
        }}
      />,
    );
    expect(screen.getByText("Helm Release")).toBeInTheDocument();
    expect(screen.getByText("jenkins-legacy")).toBeInTheDocument();
    expect(screen.getAllByText("deployed").length).toBeGreaterThan(0);  // status + history badges
    expect(screen.getByText("Values")).toBeInTheDocument();
    expect(screen.getByText(/tag: 2\.452\.1/)).toBeInTheDocument();
    expect(screen.queryByText(/sections_requested/)).not.toBeInTheDocument();
    expect(screen.queryByText(/raw_excerpt/)).not.toBeInTheDocument();
  });

  it("renders Helm unavailable for get_helm_release instead of saying release not found", () => {
    render(
      <ResultCard
        tool="get_helm_release"
        result={{
          evidence: {
            type: "inventory",
            items: [
              {
                release: "jenkins-legacy",
                namespace: "ci",
                found: false,
                available: false,
                reason: "helm_unavailable",
                remediation_hint: "Install Helm in the backend image/container.",
                error: "helm: command not found",
              },
            ],
          },
        }}
      />,
    );

    expect(screen.getByText("Helm Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Install Helm in the backend image/container.")).toBeInTheDocument();
    expect(screen.queryByText(/was not found/)).not.toBeInTheDocument();
  });

  it("renders diff_helm_revisions as a diff block, not JSON", () => {
    render(
      <ResultCard
        tool="diff_helm_revisions"
        result={{
          verdict: "n/a",
          evidence: {
            type: "inventory",
            items: [
              {
                release: "jenkins-legacy",
                namespace: "ci",
                section: "values",
                from_revision: 6,
                to_revision: 7,
                changed: true,
                redaction_may_hide_secret_only_changes: true,
                diff: "--- revision 6\n+++ revision 7\n-  tag: 2.450.0\n+  tag: 2.452.1\n",
                truncated: false,
              },
            ],
          },
        }}
      />,
    );
    expect(screen.getByText("Helm Revision Diff")).toBeInTheDocument();
    expect(screen.getByText(/revision 6 →/)).toBeInTheDocument();
    expect(screen.getByText(/tag: 2\.452\.1/)).toBeInTheDocument();
    expect(screen.getByText(/Compared after redaction/)).toBeInTheDocument();
    expect(screen.queryByText(/from_revision/)).not.toBeInTheDocument();
  });

  it("renders diff_helm_revisions 'no changes' state", () => {
    render(
      <ResultCard
        tool="diff_helm_revisions"
        result={{
          evidence: { type: "inventory", items: [
            { release: "r", namespace: "ns", section: "values", from_revision: 1, to_revision: 2,
              changed: false, redaction_may_hide_secret_only_changes: true, diff: "", truncated: false },
          ] },
        }}
      />,
    );
    expect(screen.getByText(/No non-secret changes/)).toBeInTheDocument();
  });

  it("renders investigate_helm_release with health and scoped pod info", () => {
    render(
      <ResultCard
        tool="investigate_helm_release"
        result={{
          verdict: "n/a",
          evidence: {
            type: "inventory",
            items: [
              {
                release: "jenkins-legacy",
                namespace: "ci",
                found: true,
                release_healthy: false,
                status: { status: "deployed", chart: "jenkins", chart_version: "5.8.92", revision: 7 },
                recent_revisions: [{ revision: 7, status: "deployed", chart: "jenkins-5.8.92" }],
                prior_failed_revisions: [{ revision: 6, status: "failed" }],
                workloads: [{ kind: "Deployment", name: "jenkins" }],
                resource_count: 3,
                pod_health: { scoped: true, pod_count: 2, unhealthy_count: 1,
                  unhealthy: [{ name: "jenkins-1", status: "CrashLoopBackOff", restarts: 9 }] },
                recent_warnings: { scoped: true, count: 1,
                  warnings: [{ reason: "BackOff", message: "Back-off restarting", kind: "Pod", name: "jenkins-1" }] },
              },
            ],
          },
          raw_excerpt: "{\"release_healthy\": false}",
        }}
      />,
    );
    expect(screen.getByText("Helm Release Investigation")).toBeInTheDocument();
    expect(screen.getByText("unhealthy")).toBeInTheDocument();
    expect(screen.getByText("jenkins-1")).toBeInTheDocument();
    expect(screen.getByText(/Back-off restarting/)).toBeInTheDocument();
    expect(screen.getByText(/Prior failed revisions/)).toBeInTheDocument();
    expect(screen.queryByText(/resource_count/)).not.toBeInTheDocument();
    expect(screen.queryByText(/raw_excerpt/)).not.toBeInTheDocument();
  });

  it("renders Helm unavailable for investigate_helm_release instead of not found", () => {
    render(
      <ResultCard
        tool="investigate_helm_release"
        result={{
          evidence: {
            type: "inventory",
            items: [
              {
                release: "jenkins-legacy",
                namespace: "ci",
                found: false,
                available: false,
                reason: "helm_unavailable",
                remediation_hint: "Install Helm on that target server.",
                error: "sh: 1: helm: not found",
              },
            ],
          },
        }}
      />,
    );

    expect(screen.getByText("Helm Release Investigation")).toBeInTheDocument();
    expect(screen.getByText("Install Helm on that target server.")).toBeInTheDocument();
    expect(screen.queryByText(/was not found/)).not.toBeInTheDocument();
  });

  it("renders filtered CrashLoopBackOff pod inventory as a concise table", () => {
    render(
      <ResultCard
        tool="get_pods"
        result={{
          namespace: "*",
          status_filter: "CrashLoopBackOff",
          pod_count: 2,
          pods: [
            { namespace: "infrastructure", name: "my-kafka-0", status: "CrashLoopBackOff", ready: "0/2", restarts: 42 },
            { namespace: "infrastructure", name: "my-kafka-1", status: "CrashLoopBackOff", ready: "0/2", restarts: 17 },
          ],
        }}
      />,
    );

    expect(screen.getByText("Found 2 pods in CrashLoopBackOff status across all namespaces.")).toBeInTheDocument();
    expect(screen.getByText("my-kafka-0")).toBeInTheDocument();
    expect(screen.getByText("my-kafka-1")).toBeInTheDocument();
    expect(screen.getByText("tool: get_pods")).toBeInTheDocument();
  });

  it("renders node allocation summary without exposing label noise", () => {
    render(
      <ResultCard
        tool="investigate_node"
        result={{
          name: "node-a",
          status: "Ready",
          labels: { "flannel.alpha.coreos.com/backend-data": "noisy" },
          capacity: { cpu: "16", memory_gib: 31.1 },
          allocatable: { cpu: "16", memory_gib: 30.9 },
          allocated: {
            cpu_requests_cores: 0.3,
            cpu_requests_percent_of_allocatable: 1.88,
            cpu_limits_cores: 0.15,
            cpu_limits_percent_of_allocatable: 0.94,
            memory_requests_gib: 0.262,
            memory_requests_percent_of_allocatable: 0.84,
            memory_limits_gib: 0.188,
            memory_limits_percent_of_allocatable: 0.61,
            non_terminated_pods: 6,
          },
          pods: [{ namespace: "monitoring", name: "node-exporter", cpu_requests_millicores: 100, cpu_limits_millicores: 150 }],
        }}
      />,
    );

    expect(screen.getByText("Node CPU Allocation")).toBeInTheDocument();
    expect(screen.getByText("0.3 cores (1.88%)")).toBeInTheDocument();
    expect(screen.getByText("0.15 cores (0.94%)")).toBeInTheDocument();
    expect(screen.getByText("Pods Contributing Resources (1)")).toBeInTheDocument();
    expect(screen.queryByText(/flannel\.alpha/)).not.toBeInTheDocument();
  });

  it("renders investigated pod evidence as root-cause content instead of raw JSON", () => {
    render(
      <ResultCard
        tool="investigate_pod"
        result={{
          pod_name: "my-kafka-0",
          namespace: "infrastructure",
          evidence_summary: {
            suspected_root_cause: "Kafka cannot connect to the ZooKeeper service.",
            evidence: [
              "KAFKA_ZOOKEEPER_CONNECT=zookeeper-kube-upd-cs:2181",
              { message: "ZooKeeper service is missing", service_exists: false },
            ],
            suggested_fix: "Restore the missing ZooKeeper service or update KAFKA_ZOOKEEPER_CONNECT.",
          },
          container_log_findings: [
            {
              container: "kafka-broker",
              reason: "CrashLoopBackOff",
              restart_count: 8,
              logs_previous: {
                excerpt: "Check if Zookeeper is healthy",
              },
            },
            {
              container: "prometheus-jmx-exporter",
              reason: "CrashLoopBackOff",
              restart_count: 6,
              logs_previous: {
                excerpt: "Error: Unable to access jarfile /opt/jmx_exporter/jmx_prometheus_javaagent.jar",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("Verified Evidence")).toBeInTheDocument();
    expect(screen.getByText("Kafka cannot connect to the ZooKeeper service.")).toBeInTheDocument();
    expect(screen.getByText(/ZooKeeper service is missing/)).toBeInTheDocument();
    expect(screen.getByText("Container Findings")).toBeInTheDocument();
    expect(screen.getByText("kafka-broker")).toBeInTheDocument();
    expect(screen.getByText(/Check if Zookeeper is healthy/)).toBeInTheDocument();
    expect(screen.getByText("prometheus-jmx-exporter")).toBeInTheDocument();
    expect(screen.getByText(/Unable to access jarfile/)).toBeInTheDocument();
    expect(screen.getByText(/Restore the missing ZooKeeper service/)).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
    expect(screen.queryByText(/"evidence_summary"/)).not.toBeInTheDocument();
  });

  it("renders root-cause evidence details from structured objects", () => {
    render(
      <RootCauseCard
        result={{
          evidence_summary: {
            suspected_root_cause: "Kafka cannot connect to the ZooKeeper service.",
            evidence: [
              "kafka: KAFKA_ZOOKEEPER_CONNECT=zookeeper-kube-upd-cs:2181",
              {
                message: "Kafka exits during the Confluent preflight step",
                checkpoint: "Check if Zookeeper is healthy",
              },
            ],
            dependency_checks: [
              {
                target: "zookeeper-kube-upd-cs:2181",
                service_exists: false,
                endpoints_exist: false,
                ready_addresses: 0,
              },
            ],
            suggested_fix: "Restore the missing ZooKeeper service.",
          },
        }}
      />,
    );

    expect(screen.getByText("Kafka cannot connect to the ZooKeeper service.")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Evidence Details"));

    expect(screen.getByText("# Verified root cause")).toBeInTheDocument();
    expect(screen.getByText("- kafka: KAFKA_ZOOKEEPER_CONNECT=zookeeper-kube-upd-cs:2181")).toBeInTheDocument();
    expect(screen.getByText(/Kafka exits during the Confluent preflight step/)).toBeInTheDocument();
    expect(screen.getByText(/service_exists=false/)).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("prefers versioned root_cause_summary for deterministic incident card", () => {
    render(
      <RootCauseCard
        result={{
          root_cause_summary: {
            schema_version: "root_cause_summary.v1",
            target: { pod_name: "my-kafka-0", mode: "CrashLoopBackOff", container: "kafka-broker" },
            resource_kind: "pod",
            resource_name: "my-kafka-0",
            namespace: "infrastructure",
            root_cause: "Kafka cannot connect to the ZooKeeper service.",
            severity: "critical",
            confidence: 0.95,
            evidence: [
              {
                type: "dependency_check",
                service: "zookeeper-kube-upd-cs",
                service_exists: false,
              },
            ],
            secondary_findings: [
              {
                container: "prometheus-jmx-exporter",
                reason: "CrashLoopBackOff",
                evidence: "Unable to access jarfile.",
              },
            ],
            related_resources: [
              {
                kind: "service",
                name: "zookeeper-kube-upd-cs",
                namespace: "infrastructure",
                relationship: "dependency",
              },
            ],
            suggested_fix: "Restore the missing ZooKeeper service.",
            data_completeness: "complete",
            source_tool: "investigate_pod",
          },
        }}
      />,
    );

    expect(screen.getByText("Kafka cannot connect to the ZooKeeper service.")).toBeInTheDocument();
    expect(screen.getByText(/critical/i)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Evidence Details"));

    expect(screen.getByText("# Target")).toBeInTheDocument();
    expect(screen.getByText(/name=my-kafka-0/)).toBeInTheDocument();
    expect(screen.getByText("# Related resources")).toBeInTheDocument();
    expect(screen.getAllByText(/zookeeper-kube-upd-cs/).length).toBeGreaterThan(0);
    expect(screen.getByText(/source_tool=investigate_pod/)).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("falls back to legacy root cause when root_cause_summary is malformed", () => {
    render(
      <RootCauseCard
        result={{
          root_cause_summary: {
            schema_version: "root_cause_summary.v1",
            root_cause: "",
          },
          evidence_summary: {
            suspected_root_cause: "Legacy deterministic root remains available.",
            suggested_fix: "Use the legacy fix.",
          },
        }}
      />,
    );

    expect(screen.getByText("Legacy deterministic root remains available.")).toBeInTheDocument();
  });

  it("shows review execute CTA for deterministic root cause when executable action is available", () => {
    render(
      <RootCauseCard
        result={{
          evidence_summary: {
            suspected_root_cause: "Kafka cannot connect to the ZooKeeper service.",
            suggested_fix: "Apply the corrected manifest.",
          },
        }}
        onReviewExecute={() => undefined}
      />,
    );

    expect(screen.getByText("Review & Execute Fix")).toBeInTheDocument();
  });

  it("renders slide confirmation without NaN opacity before measurement", () => {
    render(<SlideToConfirm onConfirm={() => undefined} />);

    const label = screen.getByText("Slide to confirm");
    expect(label).not.toHaveStyle({ opacity: "NaN" });
    expect(label).toHaveStyle({ opacity: "1" });
  });

  it("renders namespace inventory while keeping ConfigMap data hidden", () => {
    render(
      <ResultCard
        tool="list_namespace_resources"
        result={{
          namespace: "apps",
          summary: { pods: 1, services: 1, deployments: 1, configmaps: 1 },
          pods: [{ name: "web-0", status: "Running", ready: true, restarts: 0 }],
          services: [{ name: "web", type: "ClusterIP", ports: [{ name: "http", port: 80, target_port: 8080, protocol: "TCP" }] }],
          deployments: [{ name: "web", replicas: 3, ready: 2 }],
          configmaps: [{ name: "web-config", data: "do-not-render" }],
        }}
      />,
    );

    expect(screen.getByText("Deployments")).toBeInTheDocument();
    expect(screen.getByText("Pods")).toBeInTheDocument();
    expect(screen.getByText("Services")).toBeInTheDocument();
    expect(screen.getByText("ConfigMaps")).toBeInTheDocument();
    expect(screen.getByText("web-config")).toBeInTheDocument();
    expect(screen.queryByText("do-not-render")).not.toBeInTheDocument();
  });

  it("renders grounded RAG sources instead of raw rag_decision JSON", () => {
    render(
      <ResultCard
        tool="rag_grounded"
        result={{
          rag_decision: {
            mode: "grounded",
            top_score: 0.789,
            top_collection: "deployment_repo",
            citations: [
              {
                title: "playbooks/ops/deploy_rabbit.yaml",
                section: "ops > Deploy RabbitMQ Standard (default)",
                url: "https://github.com/example/deployment/blob/develop/ansible/playbooks/ops/deploy_rabbit.yaml",
                similarity: 0.789,
                collection: "deployment_repo",
              },
            ],
            grounded_chunks: [
              {
                title: "playbooks/ops/deploy_rabbit.yaml",
                score: 0.789,
                content: "- name: Deploy RabbitMQ Standard (default)\n  import_playbook: deploy_rabbit_old.yaml",
              },
            ],
          },
        }}
      />,
    );

    expect(screen.getByText("Knowledge Sources")).toBeInTheDocument();
    expect(screen.getAllByText("playbooks/ops/deploy_rabbit.yaml").length).toBeGreaterThan(0);
    expect(screen.getByText("ops > Deploy RabbitMQ Standard (default)")).toBeInTheDocument();
    expect(screen.getByText(/import_playbook: deploy_rabbit_old.yaml/)).toBeInTheDocument();
    expect(screen.getByText("Retrieval Summary")).toBeInTheDocument();
    expect(screen.queryByText(/"rag_decision"/)).not.toBeInTheDocument();
    expect(screen.queryByText(/"grounded_chunks"/)).not.toBeInTheDocument();
  });

  it("renders service routing details and EndpointSlice readiness", () => {
    const { rerender } = render(
      <ResultCard
        tool="get_service"
        result={{
          name: "web",
          namespace: "apps",
          type: "LoadBalancer",
          cluster_ip: "10.0.0.1",
          selector: { app: "web" },
          ports: [{ name: "http", port: 80, target_port: 8080, protocol: "TCP", node_port: 30080 }],
          session_affinity: "ClientIP",
          external_traffic_policy: "Local",
          internal_traffic_policy: "Cluster",
          ip_families: ["IPv4"],
        }}
      />,
    );

    expect(screen.getByText("Service Routing")).toBeInTheDocument();
    expect(screen.getByText("http 80->8080/TCP node:30080")).toBeInTheDocument();
    expect(screen.getByText("app=web")).toBeInTheDocument();

    rerender(
      <ResultCard
        tool="get_endpoints"
        result={{
          ready_count: 1,
          not_ready_count: 1,
          diagnostic_hint: "1 endpoint is terminating",
          endpoint_slices: {
            endpoint_count: 2,
            ready_count: 1,
            serving_count: 2,
            terminating_count: 1,
            endpoints: [
              {
                addresses: ["10.1.1.10"],
                node_name: "node-a",
                conditions: { ready: true, serving: true, terminating: false },
                target_ref: { kind: "Pod", name: "web-0" },
              },
            ],
          },
        }}
      />,
    );

    expect(screen.getByText("Endpoint Health")).toBeInTheDocument();
    const endpointLine = screen.getByText("10.1.1.10").closest("div");
    expect(endpointLine).not.toBeNull();
    expect(within(endpointLine as HTMLElement).getByText(/ready=true/)).toBeInTheDocument();
    expect(within(endpointLine as HTMLElement).getByText(/terminating=false/)).toBeInTheDocument();
  });
});
