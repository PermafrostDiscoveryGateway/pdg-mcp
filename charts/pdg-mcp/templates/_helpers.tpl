{{- define "pdg-mcp.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pdg-mcp.labels" -}}
app.kubernetes.io/name: pdg-mcp
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "pdg-mcp.selector" -}}
app.kubernetes.io/name: pdg-mcp
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "pdg-mcp.usageClaimName" -}}
{{- default (printf "%s-usage" (include "pdg-mcp.fullname" .)) .Values.report.persistence.existingClaim -}}
{{- end -}}

{{- define "pdg-mcp.activeUsersClaimName" -}}
{{- default (printf "%s-active-users" (include "pdg-mcp.fullname" .)) .Values.activeUsers.persistence.existingClaim -}}
{{- end -}}

{{- define "pdg-mcp.activeUsersPersistent" -}}
{{- if and .Values.activeUsers.enabled .Values.activeUsers.persistence.enabled -}}true{{- end -}}
{{- end -}}
