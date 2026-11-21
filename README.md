# grafana-standby

Two small tools for people whose alerting lives in Grafana (Grafana-managed alert rules, no Alertmanager):

- **`grafana_standby.py`**: keeps a second Grafana as a warm standby. Its copies of the primary's rules stay
  **paused**; when the primary stops evaluating rules, the standby un-pauses them and alerts instead, and pauses
  them again once the primary is back.
- **`grafana_rules.py`**: alert rules as code. Define rules in a Python file, apply them idempotently by uid.

Python 3.10+, standard library only.

## Why

If the box that runs Grafana dies, its alert rules die with it, and nothing tells you. A second Prometheus +
Grafana elsewhere (another VM or a small PC) fixes that, but a plain copy alerts twice while both are up. This
tool keeps the standby quiet until it's needed.

## How the standby works

- **`check`** (every minute, systemd timer): the primary is healthy when `/api/health` reports the database `ok`
  **and** every active rule group has a recently evaluated rule (within `eval_max_age_s`, or 3 group intervals).
  A Grafana that is up but whose scheduler has stalled counts as unhealthy. After `fail_threshold` failed checks
  in a row the standby **activates**: every copied rule that is active on the primary is un-paused (rules paused
  on the primary stay paused). After `recover_threshold` healthy checks it **pauses** them again. While the primary
  is healthy, `check` also copies new and changed rules (paused) every `paused_refresh_s`.
- **`sync`** (nightly timer): copies folders, alert rules (paused), evaluation intervals, notification policies, templates, mute timings
  and dashboards from the primary. It never copies contact points or datasources: create those on the standby
  yourself (same names, so the copied policy tree finds its receivers; sync stops before replacing the policy
  tree if a receiver is missing). Sync deletes removed primary rules, templates, and mute timings only when the
  state file records that this tool created them on the standby. Keep the state file across upgrades. Objects
  that predate this tracking are kept until removed manually. Dashboards retain their existing UID-based tracking.
- **The primary is only ever read.** The primary client has no write method.
- **One standby folder is never touched**: put rules that should always run on the standby (for example "the
  primary is down", "the standby's own Prometheus is down") in it and give its uid as `standby_folder_uid`.
- Notices on failover and failback go to Telegram and/or a webhook (both optional).
- A node-exporter textfile reports mode, primary health and timestamps (`grafana_standby_*`), so the primary can
  alert when the standby itself is unhealthy.

Both Grafanas need the same datasource uids (the copied rules point at them). The simplest setup: the standby's
Prometheus scrapes the same targets as the primary's, and both Grafanas name their Prometheus datasource with the
same uid. Grafana 13 reports a rule group's `lastEvaluation` as the zero time when the group holds a paused rule,
so health is judged per rule.

## Install (on the standby host)

```sh
sudo install -m 755 grafana_standby.py /usr/local/bin/
sudo install -d -m 700 /etc/grafana-standby /var/lib/grafana-standby
sudo install -m 600 examples/config.example.json /etc/grafana-standby/config.json   # then edit it
sudo cp systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo /usr/local/bin/grafana_standby.py --dry-run sync      # see what it would do
sudo systemctl enable --now grafana-standby-check.timer grafana-standby-sync.timer
```

Tokens: a service account on the primary with read access to health, alert rules, folders, policies, templates,
mute timings and dashboards (Viewer plus alerting read); one on the standby with write access to those. The units'
`ReadWritePaths` must contain the state directory and your node-exporter textfile directory; edit them if your
textfile directory isn't `/var/lib/prometheus/node-exporter`.

Config (`/etc/grafana-standby/config.json`, mode 600; see [`examples/config.example.json`](examples/config.example.json)):

| Key | Default | Meaning |
| --- | --- | --- |
| `primary_url`, `primary_token` | (required) | primary Grafana and its read-only token |
| `standby_url`, `standby_token` | (required) | standby Grafana and its token |
| `standby_folder_uid` | (required) | folder of always-on standby rules; never touched |
| `primary_name`, `standby_name` | `primary Grafana` / `standby Grafana` | names used in notices |
| `telegram_bot_token`, `telegram_chat_id` | (off) | Telegram notices |
| `webhook_url`, `webhook_field` | (off) / `text` | POST `{"<field>": message}`; `text` for Slack/Mattermost-style hooks, `content` for Discord |
| `fail_threshold` / `recover_threshold` | `3` / `5` | consecutive checks before failover / failback |
| `eval_max_age_s` | `300` | how old a rule's last evaluation may be |
| `paused_refresh_s` | `900` | how often `check` refreshes the rule copies while healthy |
| `state_path` | `/var/lib/grafana-standby/state.json` | state file |
| `textfile_path` | `/var/lib/prometheus/node-exporter/grafana_standby.prom` | metrics for node-exporter |
| `http_timeout_s` | `10` | per request |

Operate:

```sh
sudo grafana_standby.py status
sudo grafana_standby.py --dry-run sync
sudo grafana_standby.py activate      # force failover (test it once after setup)
sudo grafana_standby.py deactivate    # force failback
journalctl -u grafana-standby-check.service -u grafana-standby-sync.service
```

`--dry-run sync` reads both Grafanas and prints the planned writes without changing Grafana, state, or metrics.
Group interval updates retain current group members. A standby-local rule that shares a copied group will use
that group's interval, so give it a separate group if it needs its own cadence.

## Alert rules as code (`grafana_rules.py`)

```python
from grafana_rules import cli, rule
common = dict(folder_uid="FOLDER_UID", group="Host health (code)", datasource_uid="PROMETHEUS_UID")
RULES = [rule("host-disk-space-low", "HostDiskSpaceLow", "<PromQL> > 85", for_="15m",
              description="{{ $labels.mountpoint }} is nearly full", **common)]
cli(RULES, folder_uid=common["folder_uid"], group=common["group"], interval_s=60)
```

A full example is [`examples/host_rules.py`](examples/host_rules.py) (disk space, disk filling fast, exporter down,
SMART failure). Set `GRAFANA_URL` and `GRAFANA_TOKEN` (or `GRAFANA_TOKEN_FILE`), then run it with `--print`,
`--dry-run`, or nothing to apply. Each rule fires per series while its expression is above the threshold (0 by
default). Rules are created with `X-Disable-Provenance`, so they stay editable in the UI (but the next run
overwrites UI edits).

> **Warning: a group update replaces the whole group.** Setting the evaluation interval uses Grafana's
> rule-group endpoint, which replaces the group: any rule in it that your file doesn't list is **deleted**.
> `grafana_rules.py` reads the group first and stops with an error if it holds rules you didn't list. Pass
> `--prune` (or `prune=True`) only when you really want those deleted. Keep hand-made rules in their own group.
> Rule writes and the final group write are separate API calls. If a call fails, earlier writes remain
> applied, and the group can still have its old interval. Correct the error and rerun the same rules file.

## Tests

```sh
python3 -B -m unittest discover -s tests -t . -v
```

The tests run against local fake Grafana HTTP servers. They were not run against a real Grafana in this
repository's CI; test a failover (`activate` / `deactivate`) on your own pair after installing.

## License

MIT, see [LICENSE](LICENSE).
