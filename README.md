# Server Monitoring With Email Alert

Two ways to monitor a server and get emailed when something goes wrong,

- **`monitrc`** — a config file for [Monit](https://mmonit.com/monit/), a
  lightweight Linux daemon. Same approach as the original repo. Best if
  you're on a Linux server and want a battle-tested tool with a web UI.
- **`server_monitor.py`** — a standalone Python script that does the same
  job (CPU, memory, disk, swap, load average, process checks, website
  uptime) with no extra daemon to install. Works on Linux, macOS, or
  Windows, and is easy to read/extend.

Pick whichever fits your setup — they're independent, you don't need both.

---

## Option A: Monit (`monitrc`)

1. Install monit
   ```bash
   sudo apt install monit      # Debian/Ubuntu
   sudo yum install monit      # RHEL/CentOS
   ```
2. Edit `monitrc` and replace every `<PLACEHOLDER>`:
   - `<SMTP_USERNAME>` / `<SMTP_PASSWORD>` — your email account's SMTP creds
     (for Gmail, use an [app password](https://support.google.com/accounts/answer/185833))
   - `<YOUR_ALERT_EMAIL>` — where alerts should be sent
   - `<WEB_UI_PASSWORD>` — password for the optional local web dashboard
   - `<YOUR_HOSTNAME_OR_LABEL>` — a name for the system block
   - Adjust the `mysqld`/`apache2` checks or remove them if you don't run those services
3. Copy it into place and start monit:
   ```bash
   sudo cp monitrc /etc/monit/monitrc
   sudo monit -t                     # validate syntax
   sudo systemctl restart monit
   sudo systemctl enable monit
   ```
4. Check status any time with `sudo monit status`, or visit
   `http://localhost:2812` for the web UI.

---

## Option B: Python script (`server_monitor.py`)

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Edit `config.yaml`:
   - Fill in your SMTP settings under `email:` (same app-password note as above)
   - Adjust `thresholds:` to taste
   - List the disks, processes, and websites you want watched
3. Run it:
   ```bash
   # single pass — good for a cron job
   python3 server_monitor.py --config config.yaml

   # or run continuously, checking every check_interval_seconds
   python3 server_monitor.py --config config.yaml --daemon
   ```
4. For scheduled checks instead of a daemon, add a cron entry:
   ```
   */5 * * * * cd /path/to/server-monitor && /usr/bin/python3 server_monitor.py --config config.yaml
   ```

### How alerting works

The script only emails you on a **state change** — once when a check first
crosses its threshold ("ALERT"), and once when it goes back to normal
("RECOVERED"). This mirrors what Monit does, so you don't get the same
email every single check interval. State is tracked in `monitor_state.json`
(created automatically next to the script); logs go to `monitor.log`.

### What it checks

| Check | Config key |
|---|---|
| CPU usage | `thresholds.cpu_percent` |
| Memory usage | `thresholds.memory_percent` |
| Swap usage | `thresholds.swap_percent` |
| Load average (per core) | `thresholds.load_per_core` |
| Disk usage per mount | `disks` + `thresholds.disk_percent` |
| Process running? | `processes` |
| Website/HTTP status | `websites` |

To monitor a `systemd` service instead of a raw process name, you can
extend `check_processes()` to shell out to `systemctl is-active <service>`.

---

## Security notes

- Never commit real SMTP credentials to git — keep `config.yaml` and
  `monitrc` (once filled in) out of version control, or use environment
  variables / a secrets manager instead.
- Use an app password or a dedicated SMTP relay account, not your primary
  email password.
- If you enable Monit's web UI, keep it bound to `localhost` (as in the
  provided config) unless you've put proper auth/TLS in front of it.
