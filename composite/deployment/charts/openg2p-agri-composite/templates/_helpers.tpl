{{- define "agriComposite.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "common.names.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{/*
Name of the ConfigMap holding the use cases.
*/}}
{{- define "agriComposite.useCasesConfigMap" -}}
{{- if .Values.existingUseCasesConfigMap -}}
{{ tpl .Values.existingUseCasesConfigMap $ }}
{{- else -}}
{{ .Release.Name }}-use-cases
{{- end -}}
{{- end -}}

{{/*
Registries as the service reads them: JSON {controller: {url, partner_id}}.
*/}}
{{- define "agriComposite.registriesJson" -}}
{{- $out := dict -}}
{{- range $id, $r := .Values.registries }}
{{- $_ := set $out $id (dict "url" (tpl (toString $r.url) $) "partner_id" (default "" $r.partnerId) "receiver_id" (default "" $r.receiverId)) -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/*
env: list from .Values.envVars (templated literals) and .Values.envVarsFrom.
*/}}
{{- define "agriComposite.envVars" -}}
{{- range $key, $value := .Values.envVars }}
- name: {{ $key }}
  value: {{ tpl (printf "%v" $value) $ | quote }}
{{- end }}
- name: AGRI_COMPOSITE_REGISTRIES
  value: {{ include "agriComposite.registriesJson" . | quote }}
{{- range $key, $spec := .Values.envVarsFrom }}
- name: {{ $key }}
  valueFrom:
{{ tpl (toYaml $spec) $ | indent 4 }}
{{- end }}
{{- end -}}
