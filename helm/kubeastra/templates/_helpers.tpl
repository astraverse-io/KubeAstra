{{/*
Expand the name of the chart.
*/}}
{{- define "kubeastra.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Truncate at 63 chars because some Kubernetes name fields are limited.
*/}}
{{- define "kubeastra.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label.
*/}}
{{- define "kubeastra.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to all resources.
*/}}
{{- define "kubeastra.labels" -}}
helm.sh/chart: {{ include "kubeastra.chart" . }}
{{ include "kubeastra.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels — used in matchLabels and pod template labels.
*/}}
{{- define "kubeastra.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubeastra.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Backend-specific selector labels.
*/}}
{{- define "kubeastra.backend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubeastra.name" . }}-backend
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: backend
{{- end }}

{{/*
Frontend-specific selector labels.
*/}}
{{- define "kubeastra.frontend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubeastra.name" . }}-frontend
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: frontend
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "kubeastra.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "kubeastra.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Backend service name — used by the frontend to build the API URL.
*/}}
{{- define "kubeastra.backendServiceName" -}}
{{- printf "%s-backend" (include "kubeastra.fullname" .) }}
{{- end }}

{{/*
Frontend API URL — auto-resolved to in-cluster backend service unless overridden.
*/}}
{{- define "kubeastra.frontendApiUrl" -}}
{{- if .Values.frontend.apiUrl }}
{{- .Values.frontend.apiUrl }}
{{- else }}
{{- printf "http://%s:%d" (include "kubeastra.backendServiceName" .) (.Values.backend.service.port | int) }}
{{- end }}
{{- end }}

{{/*
Qdrant service name (used by the backend/MCP to build QDRANT_URL).
*/}}
{{- define "kubeastra.qdrantServiceName" -}}
{{- printf "%s-qdrant" (include "kubeastra.fullname" .) }}
{{- end }}

{{/*
Qdrant in-cluster URL — auto-resolves to the chart-managed service when
qdrant.enabled=true; otherwise honors a caller-supplied qdrant.externalUrl.
*/}}
{{- define "kubeastra.qdrantUrl" -}}
{{- if .Values.qdrant.enabled }}
{{- printf "http://%s:6333" (include "kubeastra.qdrantServiceName" .) }}
{{- else if .Values.qdrant.externalUrl }}
{{- .Values.qdrant.externalUrl }}
{{- else }}
{{- "http://localhost:6333" }}
{{- end }}
{{- end }}

{{/*
Qdrant selector labels.
*/}}
{{- define "kubeastra.qdrant.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubeastra.name" . }}-qdrant
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: qdrant
{{- end }}

{{/*
Resolve the loadBalancerIP for a Service based on .Release.Namespace, so a
single install can target dev or prod without separate values files:

    helm install ... --namespace k8s-ai-test   -> dev IP
    helm install ... --namespace kubeastra    -> prod IP

Lookup order, first non-empty wins:
  1. Per-service explicit override (.Values.<component>.service.loadBalancerIP)
  2. Namespace -> IP map at .Values.networking.loadBalancerIPByNamespace
  3. Empty string (let the cloud LB controller assign one ephemerally)

Call as:
  {{- $ip := include "kubeastra.loadBalancerIP" (dict "ctx" . "perService" .Values.frontend.service.loadBalancerIP) }}
  {{- with $ip }}
  loadBalancerIP: {{ . }}
  {{- end }}
*/}}
{{- define "kubeastra.loadBalancerIP" -}}
{{- $ctx := .ctx -}}
{{- $perService := .perService | default "" -}}
{{- $byNs := default (dict) (default (dict) $ctx.Values.networking).loadBalancerIPByNamespace -}}
{{- $fromMap := index $byNs $ctx.Release.Namespace | default "" -}}
{{- if $perService -}}
{{- $perService -}}
{{- else if $fromMap -}}
{{- $fromMap -}}
{{- end -}}
{{- end }}

{{/*
Resolve ALERT_WEBHOOK_TOKEN for the current install based on
.Release.Namespace, so a single chart can target dev or prod from one
values file (no env-specific overrides at the CLI):

    helm install ... --namespace k8s-ai-test   -> dev token
    helm install ... --namespace kubeastra    -> prod token

Lookup order, first non-empty wins:
  1. .Values.secrets.alertWebhookToken            (single-env / legacy)
  2. .Values.secrets.alertWebhookTokensByNamespace[<ns>]   (per-env map)
  3. Convenience aliases that match the two named environments:
       k8s-ai-test -> alertWebhookToken_Dev
       kubeastra  -> alertWebhookToken_Prod

The third path lets a values-secrets.yaml carry the two tokens under
human-readable field names without forcing the user to build a map.
Returns the empty string when none are set (-> webhook stays open;
the Secret template skips the key entirely).
*/}}
{{- define "kubeastra.alertWebhookToken" -}}
{{- $ns := .Release.Namespace -}}
{{- $explicit := .Values.secrets.alertWebhookToken | default "" -}}
{{- $byNs := default (dict) .Values.secrets.alertWebhookTokensByNamespace -}}
{{- $fromMap := index $byNs $ns | default "" -}}
{{- $alias := "" -}}
{{- if eq $ns "k8s-ai-test" -}}
{{- $alias = .Values.secrets.alertWebhookToken_Dev | default "" -}}
{{- else if eq $ns "kubeastra" -}}
{{- $alias = .Values.secrets.alertWebhookToken_Prod | default "" -}}
{{- end -}}
{{- if $explicit -}}{{- $explicit -}}
{{- else if $fromMap -}}{{- $fromMap -}}
{{- else if $alias -}}{{- $alias -}}
{{- end -}}
{{- end }}

{{/*
True when .Release.Namespace is in networking.loadBalancerIPByNamespace AND
the user hasn't already picked a service type. Used by backend-service.yaml
to auto-promote backend from ClusterIP to LoadBalancer when the install
clearly targets one of the known envs — so `helm install -n k8s-ai-test`
just works without an extra --set.

Set backend.service.type explicitly to opt out (e.g. type: ClusterIP forces
internal-only even in a known namespace).
*/}}
{{- define "kubeastra.backendServiceType" -}}
{{- $ns := .Release.Namespace -}}
{{- /* Any non-empty value (including ClusterIP) means the user explicitly
       picked it; honor that. Empty -> infer from the IP map. */ -}}
{{- $explicit := .Values.backend.service.type | default "" -}}
{{- $byNs := default (dict) (default (dict) .Values.networking).loadBalancerIPByNamespace -}}
{{- $hasReservedIp := index $byNs $ns -}}
{{- if $explicit -}}
{{- $explicit -}}
{{- else if $hasReservedIp -}}
LoadBalancer
{{- else -}}
ClusterIP
{{- end -}}
{{- end }}

{{/*
Merged backend service annotations. When the namespace is in
loadBalancerIPByNamespace and the user hasn't set a load-balancer-type
annotation explicitly, default to GCP Internal — matches the frontend
service convention. User annotations always win on key collision.
*/}}
{{- define "kubeastra.backendServiceAnnotations" -}}
{{- $ns := .Release.Namespace -}}
{{- $byNs := default (dict) (default (dict) .Values.networking).loadBalancerIPByNamespace -}}
{{- $hasReservedIp := index $byNs $ns -}}
{{- $user := default (dict) .Values.backend.service.annotations -}}
{{- if and $hasReservedIp (not (hasKey $user "networking.gke.io/load-balancer-type")) -}}
{{- $defaults := dict "networking.gke.io/load-balancer-type" "Internal" -}}
{{- toYaml (merge $user $defaults) -}}
{{- else -}}
{{- toYaml $user -}}
{{- end -}}
{{- end }}
