---
name: deploy-grafana-standby
description: Install and verify the warm-standby Grafana check and sync timers.
---

# Install and verify the warm-standby Grafana check and sync timers

1. Follow the README to install the script and example config on the standby host. Protect the local config and keep the state directory across upgrades.
2. Configure read-only primary access and standby write access. Match datasource UIDs and provision standby contact points before copying policies.
3. Install the systemd units and check write permissions for both the state and node-exporter directories.
4. Preview with `grafana_standby.py --dry-run sync` before enabling the check and sync timers.
5. Use `grafana_standby.py status` and the service journal. Follow the README's controlled failover and failback check.
