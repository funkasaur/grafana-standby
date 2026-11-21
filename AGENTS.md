# Agent instructions for grafana-standby

## Scope

Standard-library Python 3.10+ tools for Grafana-managed alert-rule provisioning and warm
standby.

## Repository layout

- `grafana_standby.py` performs standby sync, health checks and failover.
- `grafana_rules.py` defines the idempotent rules-as-code CLI and helpers.
- `examples/` contains example configuration and rule definitions.
- `systemd/` contains check and sync services and timers.
- `tests/` contains offline `unittest` coverage.

## Run and test

- Run `python3 -B -m unittest discover -s tests -t . -v` from the root.
- Follow the README's standby install procedure to create the config and state directories
  and install the timers.
- Use `grafana_standby.py status` to inspect mode and `grafana_standby.py --dry-run sync` to
  preview writes. The preview reads both Grafanas.
- Preserve the state file across upgrades and configure systemd write access for the state
  and textfile directories.

## Conventions

- Keep the primary Grafana client read-only.
- Keep the standby-only folder outside synchronization.
- Copy primary alert rules paused. Failover enables only copied rules that are active on the
  primary.
- Check evaluation freshness per rule as well as the database health endpoint.
- Sync deletes tracked objects it created. Preserve manually managed and untracked objects.
- Match datasource UIDs on both Grafanas. Standby contact points and datasources are
  provisioned separately.
- Keep example configs free of live credentials.

## Rule provisioning

- Use the rule CLI's print mode to inspect generated rules without contacting Grafana.
- Group interval updates replace the group. Keep hand-written rules in their own group and
  use prune only for deletion you intend.
- Earlier API writes can survive a later group-write failure. Fix the error and rerun the
  same rules file.
