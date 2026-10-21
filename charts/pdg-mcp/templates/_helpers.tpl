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
