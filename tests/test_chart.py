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
    claim = render("report.enabled=true", "report.persistence.storageClassName=fast").split("kind: PersistentVolumeClaim")[1]
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
        ["helm", "template", "pdg", str(CHART), "--set", "report.enabled=true", "--set", "report.persistence.pv.enabled=true"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0 and "report.persistence.pv.source is required" in result.stderr


def test_image_contains_the_report_script():
    dockerfile = (CHART.parent.parent / "Dockerfile").read_text()
    assert "COPY scripts/usage_report.py ./scripts/usage_report.py" in dockerfile
    assert (CHART.parent.parent / "scripts" / "usage_report.py").is_file()
