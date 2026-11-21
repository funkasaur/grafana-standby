#!/usr/bin/env python3
"""Example rules file: generic host-health rules for node-exporter and smartctl-exporter.

    export GRAFANA_URL=http://localhost:3000 GRAFANA_TOKEN_FILE=~/.config/grafana-token
    export PROM_DS_UID=<your Prometheus datasource uid> FOLDER_UID=<an existing alert folder uid>
    python3 examples/host_rules.py --print      # show the JSON
    python3 examples/host_rules.py --dry-run    # read Grafana, print the writes
    python3 examples/host_rules.py              # apply

Find the datasource uid under Connections > Data sources (it's in the URL), the folder uid in the folder's URL.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from grafana_rules import cli, rule  # noqa: E402

DS = os.environ.get("PROM_DS_UID", "PROMETHEUS_UID")
FOLDER = os.environ.get("FOLDER_UID", "FOLDER_UID")
GROUP = "Host health (code)"
FS = 'fstype=~"ext4|xfs|btrfs|zfs"'
USED = f"100 * (1 - node_filesystem_avail_bytes{{{FS}}} / node_filesystem_size_bytes{{{FS}}})"
common = dict(folder_uid=FOLDER, group=GROUP, datasource_uid=DS)

RULES = [
    rule("host-disk-space-low", "HostDiskSpaceLow", f"{USED} > 85", for_="15m",
         description="{{ $labels.instance }} {{ $labels.mountpoint }} is {{ humanize $values.A.Value }}% full.",
         **common),
    rule("host-disk-filling-fast", "HostDiskFillingFast",
         f"({USED} > 70) and on(instance, mountpoint) "
         f"(predict_linear(node_filesystem_avail_bytes{{{FS}}}[6h], 24 * 3600) < 0)", for_="30m",
         description="{{ $labels.instance }} {{ $labels.mountpoint }} will fill within 24 h at the last 6 hours' rate.",
         **common),
    rule("host-node-exporter-down", "HostExporterDown", 'up{job="node"} == 0', for_="5m", severity="critical",
         threshold=-1, comparison="gt", no_data="Alerting",
         description="Prometheus cannot scrape node-exporter on {{ $labels.instance }}.", **common),
    rule("host-smart-critical", "HostSmartCritical",
         "(smartctl_device_critical_warning > 0) or ((smartctl_device_smart_status == 0) + 1)", for_="0s",
         severity="critical", description="{{ $labels.instance }} {{ $labels.device }} reports a SMART failure.",
         **common),
]

if __name__ == "__main__":
    sys.exit(cli(RULES, folder_uid=FOLDER, group=GROUP, interval_s=60))
