"""Render the Helm chart and check the monitoring pieces. Skipped when helm is not installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

CHART = Path(__file__).resolve().parent.parent / "charts" / "pdg-mcp"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def render(*sets: str) -> str:
    args = ["helm", "template", "pdg", str(CHART), "-n", "pdg"]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def test_podmonitor_is_off_by_default():
    """Clusters without the Prometheus Operator CRDs must still install."""
    assert "kind: PodMonitor" not in render()


def test_podmonitor_targets_the_metrics_container_port():
    out = render("metrics.podMonitor.enabled=true")
    assert "kind: PodMonitor" in out
    assert "port: metrics" in out and "path: /metrics" in out
    assert "- pdg\n" in out  # namespaceSelector names the release namespace
    assert "- name: metrics\n              containerPort: 9100" in out  # the port the monitor points at


def test_podmonitor_labels_and_interval_are_configurable():
    out = render(
        "metrics.podMonitor.enabled=true",
        "metrics.podMonitor.labels.release=rancher-monitoring",
        "metrics.podMonitor.interval=1m",
    )
    assert "release: rancher-monitoring" in out and "interval: 1m" in out


def test_no_podmonitor_when_metrics_are_disabled():
    assert "kind: PodMonitor" not in render("metrics.podMonitor.enabled=true", "metrics.enabled=false")


def test_metrics_port_is_never_on_the_service():
    """The Service and Ingress must keep routing only the MCP port."""
    service = render("metrics.podMonitor.enabled=true").split("kind: Service")[1].split("---")[0]
    assert "metrics" not in service and "9100" not in service


# --- usage archive (CronJob + volume) ------------------------------------------------------------------------
def test_usage_archive_is_off_by_default():
    out = render()
    assert "kind: CronJob" not in out and "kind: PersistentVolume" not in out


def test_cronjob_archives_to_the_mounted_claim():
    out = render("report.enabled=true")
    cron = out.split("kind: CronJob")[1].split("\n---")[0]
    assert "concurrencyPolicy: Forbid" in cron
    assert "--archive-dir=/data" in cron and "--days=3" in cron
    assert "--prometheus-url=http://rancher-monitoring-prometheus.cattle-monitoring-system:9090" in cron
    assert "claimName: pdg-usage" in cron and "mountPath: /data" in cron
    assert "readOnlyRootFilesystem: true" in cron  # same hardening as the app


def test_claim_survives_release_removal_and_pruning():
    claim = render("report.enabled=true").split("kind: PersistentVolumeClaim")[1].split("\n---")[0]
    assert "helm.sh/resource-policy: keep" in claim
    assert "argocd.argoproj.io/sync-options: Prune=false" in claim
    assert "storageClassName" not in claim  # cluster default class unless one is named


def test_named_storage_class_is_used():
    claim = render("report.enabled=true", "report.persistence.storageClassName=fast").split(
        "kind: PersistentVolumeClaim"
    )[1]
    assert "storageClassName: fast" in claim


def test_existing_claim_is_used_and_no_claim_is_created():
    out = render("report.enabled=true", "report.persistence.existingClaim=mine")
    assert "kind: PersistentVolumeClaim" not in out and "claimName: mine" in out


def test_static_pv_is_retained_reserved_and_bound_by_name():
    out = render(
        "report.enabled=true",
        "report.persistence.pv.enabled=true",
        "report.persistence.pv.source.nfs.server=nfs.example.org",
        "report.persistence.pv.source.nfs.path=/exports/usage",
    )
    pv = out.split("kind: PersistentVolume\n")[1].split("\n---")[0]
    assert "persistentVolumeReclaimPolicy: Retain" in pv
    assert "server: nfs.example.org" in pv and "path: /exports/usage" in pv
    assert "claimRef:" in pv and "namespace: pdg" in pv and "name: pdg-usage" in pv
    claim = out.split("kind: PersistentVolumeClaim")[1].split("\n---")[0]
    assert 'storageClassName: ""' in claim and "volumeName: pdg-usage" in claim


def test_static_pv_without_a_source_is_refused():
    result = subprocess.run(
        [
            "helm",
            "template",
            "pdg",
            str(CHART),
            "--set",
            "report.enabled=true",
            "--set",
            "report.persistence.pv.enabled=true",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "report.persistence.pv.source is required" in result.stderr


def test_image_contains_the_report_script():
    dockerfile = (CHART.parent.parent / "Dockerfile").read_text()
    assert "COPY scripts/usage_report.py ./scripts/usage_report.py" in dockerfile
    assert (CHART.parent.parent / "scripts" / "usage_report.py").is_file()


# --- Grafana dashboard ConfigMap -----------------------------------------------------------------------------
DASHBOARD_JSON = CHART / "dashboards" / "pdg-mcp.json"


def test_dashboard_configmap_is_off_by_default():
    assert "kind: ConfigMap" not in render()


def test_dashboard_configmap_goes_to_the_sidecar_namespace_with_its_label():
    cm = render("grafana.dashboard.enabled=true").split("kind: ConfigMap")[1].split("\n---")[0]
    assert "namespace: cattle-dashboards" in cm  # not the release namespace
    assert 'grafana_dashboard: "1"' in cm
    assert "name: pdg-dashboard" in cm and "pdg-mcp.json: |-" in cm


def test_dashboard_namespace_falls_back_to_the_release_namespace():
    cm = render("grafana.dashboard.enabled=true", "grafana.dashboard.namespace=").split("kind: ConfigMap")[1]
    assert "namespace: pdg\n" in cm


def test_dashboard_labels_and_annotations_are_configurable():
    cm = (
        render(
            "grafana.dashboard.enabled=true",
            "grafana.dashboard.labels.team=pdg",
            "grafana.dashboard.annotations.grafana_folder=PDG",
        )
        .split("kind: ConfigMap")[1]
        .split("\n---")[0]
    )
    assert 'grafana_dashboard: "1"' in cm and "team: pdg" in cm  # extra labels are added to the default
    assert "grafana_folder: PDG" in cm


def test_dashboard_json_survives_the_configmap_unchanged():
    """Guards against YAML escaping or indentation mangling the JSON Grafana will load."""
    import json

    yaml = pytest.importorskip("yaml")
    docs = list(yaml.safe_load_all(render("grafana.dashboard.enabled=true")))
    cm = next(d for d in docs if d and d.get("kind") == "ConfigMap")
    embedded = json.loads(cm["data"]["pdg-mcp.json"])
    assert embedded == json.loads(DASHBOARD_JSON.read_text())
    for token in ("$__range", "$__rate_interval", "${datasource}"):
        assert token in cm["data"]["pdg-mcp.json"]  # Grafana variables, not Helm's to expand


def test_dashboard_is_far_below_configmap_size_limits():
    """ConfigMaps cap at 1 MiB, and a client-side apply copies the object into a 256 KiB annotation."""
    assert DASHBOARD_JSON.stat().st_size < 200_000


def test_local_stack_loads_the_same_dashboard_file_the_chart_ships():
    compose = (CHART.parent.parent / "dev" / "observability" / "docker-compose.yml").read_text()
    assert "../../charts/pdg-mcp/dashboards:/var/lib/grafana/dashboards" in compose
    assert DASHBOARD_JSON.is_file() and not (CHART.parent.parent / "dashboards").exists()  # one copy only


# --- active-user database volume ------------------------------------------------------------------------------
HASH_KEY = ("observability.userHashKeySecret.name=obs", "observability.userHashKeySecret.key=user-hash-key")


def render_fails(*sets: str) -> str:
    args = ["helm", "template", "pdg", str(CHART)]
    for s in sets:
        args += ["--set", s]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    return result.stderr


def deployment(out: str) -> str:
    return out.split("kind: Deployment")[1].split("\n---")[0]


def test_active_users_are_counted_in_memory_by_default():
    out = render()
    dep = deployment(out)
    assert 'ARCTICDATA_MCP_OBSERVABILITY_ACTIVE_USERS_ENABLED\n              value: "true"' in dep
    assert "ACTIVE_USERS_DB" not in dep and "volumeMounts" not in dep and "Recreate" not in dep
    assert "active-users" not in out


def test_active_user_volume_is_mounted_and_the_database_put_on_it():
    out = render("activeUsers.persistence.enabled=true", *HASH_KEY)
    dep = deployment(out)
    assert "value: /data/active-users/active-users.sqlite" in dep
    assert "mountPath: /data/active-users" in dep and "claimName: pdg-active-users" in dep
    assert "type: Recreate" in dep and "fsGroup: 10001" in dep
    pvc = out.split("name: pdg-active-users")[1].split("\n---")[0]
    assert "helm.sh/resource-policy: keep" in pvc and "storage: 100Mi" in pvc


def test_active_user_volume_uses_an_existing_claim():
    out = render("activeUsers.persistence.enabled=true", "activeUsers.persistence.existingClaim=mine", *HASH_KEY)
    assert "claimName: mine" in deployment(out)
    assert "kind: PersistentVolumeClaim" not in out


def test_active_user_volume_needs_the_hash_key_secret():
    assert "needs observability.userHashKeySecret" in render_fails("activeUsers.persistence.enabled=true")


def test_active_user_volume_allows_one_replica_only():
    err = render_fails("activeUsers.persistence.enabled=true", "replicaCount=2", *HASH_KEY)
    assert "replicaCount: 1 only" in err


def test_no_active_user_volume_when_tracking_is_off():
    out = render("activeUsers.enabled=false", "activeUsers.persistence.enabled=true")
    assert "pdg-active-users" not in out and "ACTIVE_USERS_DB" not in out
