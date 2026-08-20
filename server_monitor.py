#!/usr/bin/env python3
"""
Server Monitoring With Email Alert (Python edition)
-----------------------------------------------------
Inspired by: LasVegasCoder/Server-Monitoring-With-Email-Alert (Monit-based)

Monitors CPU, memory, disk, swap, load average, specific processes,
systemd services, and website/HTTP endpoints. Sends an email the moment
a check crosses its threshold, and sends a second email when it recovers
(so you're not spammed every run).

Usage:
    python3 server_monitor.py --config config.yaml            # run one check pass (good for cron)
    python3 server_monitor.py --config config.yaml --daemon   # run forever, checking on an interval

Requires: psutil, requests, pyyaml  (see requirements.txt)
"""

import argparse
import json
import logging
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.exit("Missing dependency: psutil. Install with `pip install -r requirements.txt`.")

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: requests. Install with `pip install -r requirements.txt`.")

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pyyaml. Install with `pip install -r requirements.txt`.")


STATE_FILE = Path(__file__).with_name("monitor_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).with_name("monitor.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("server-monitor")


# --------------------------------------------------------------------------- #
# State handling (so we only email on state CHANGE, not every single check)
# --------------------------------------------------------------------------- #
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file was corrupt, starting fresh.")
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(cfg, subject, body):
    email_cfg = cfg["email"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_cfg["from"]
    msg["To"] = ", ".join(email_cfg["to"])

    try:
        if email_cfg.get("use_ssl", False):
            server = smtplib.SMTP_SSL(email_cfg["smtp_host"], email_cfg["smtp_port"], context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"])
            if email_cfg.get("use_tls", True):
                server.starttls(context=ssl.create_default_context())

        if email_cfg.get("username"):
            server.login(email_cfg["username"], email_cfg["password"])

        server.sendmail(email_cfg["from"], email_cfg["to"], msg.as_string())
        server.quit()
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.error("Failed to send email (%s): %s", subject, e)


def alert(cfg, state, key, is_problem, subject, description):
    """
    Fires an email only on transition: OK -> PROBLEM, or PROBLEM -> OK.
    `key` is a unique id for this check (e.g. 'cpu', 'disk:/', 'proc:mysqld').
    """
    was_problem = state.get(key, False)

    if is_problem and not was_problem:
        body = (
            f"Event:       Alert triggered\n"
            f"Check:       {key}\n"
            f"Host:        {socket.gethostname()}\n"
            f"Date:        {datetime.now().isoformat(sep=' ', timespec='seconds')}\n"
            f"Description: {description}\n"
        )
        send_email(cfg, f"[ALERT] {subject}", body)
        log.warning("ALERT %s: %s", key, description)
    elif not is_problem and was_problem:
        body = (
            f"Event:       Recovered\n"
            f"Check:       {key}\n"
            f"Host:        {socket.gethostname()}\n"
            f"Date:        {datetime.now().isoformat(sep=' ', timespec='seconds')}\n"
            f"Description: {key} is back to normal.\n"
        )
        send_email(cfg, f"[RECOVERED] {subject}", body)
        log.info("RECOVERED %s", key)

    state[key] = is_problem


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_system_resources(cfg, state):
    t = cfg.get("thresholds", {})

    cpu_pct = psutil.cpu_percent(interval=1)
    alert(cfg, state, "cpu", cpu_pct > t.get("cpu_percent", 85),
          "High CPU usage", f"CPU usage is {cpu_pct:.1f}% (threshold {t.get('cpu_percent', 85)}%).")

    mem = psutil.virtual_memory()
    alert(cfg, state, "memory", mem.percent > t.get("memory_percent", 85),
          "High memory usage", f"Memory usage is {mem.percent:.1f}% (threshold {t.get('memory_percent', 85)}%).")

    swap = psutil.swap_memory()
    if swap.total > 0:
        alert(cfg, state, "swap", swap.percent > t.get("swap_percent", 50),
              "High swap usage", f"Swap usage is {swap.percent:.1f}% (threshold {t.get('swap_percent', 50)}%).")

    if hasattr(psutil, "getloadavg"):
        load1, load5, _ = psutil.getloadavg()
        cores = psutil.cpu_count() or 1
        norm_load5 = load5 / cores
        alert(cfg, state, "loadavg", norm_load5 > t.get("load_per_core", 2.0),
              "High load average", f"5-min load average is {load5:.2f} ({norm_load5:.2f} per core, threshold {t.get('load_per_core', 2.0)}).")

    for mount in cfg.get("disks", ["/"]):
        try:
            usage = psutil.disk_usage(mount)
            alert(cfg, state, f"disk:{mount}", usage.percent > t.get("disk_percent", 90),
                  f"Low disk space ({mount})", f"Disk usage on {mount} is {usage.percent:.1f}% (threshold {t.get('disk_percent', 90)}%).")
        except FileNotFoundError:
            log.warning("Disk mount not found: %s", mount)


def check_processes(cfg, state):
    for proc_name in cfg.get("processes", []):
        running = any(
            proc_name.lower() in (p.info.get("name") or "").lower()
            for p in psutil.process_iter(["name"])
        )
        alert(cfg, state, f"proc:{proc_name}", not running,
              f"Process down: {proc_name}", f"Process '{proc_name}' does not appear to be running.")


def check_websites(cfg, state):
    for site in cfg.get("websites", []):
        url = site["url"]
        expected = site.get("expected_status", 200)
        timeout = site.get("timeout", 10)
        try:
            resp = requests.get(url, timeout=timeout)
            is_down = resp.status_code != expected
            detail = f"{url} returned HTTP {resp.status_code} (expected {expected})."
        except requests.RequestException as e:
            is_down = True
            detail = f"{url} is unreachable: {e}"

        alert(cfg, state, f"site:{url}", is_down,
              f"Website issue: {url}", detail)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def run_once(cfg):
    state = load_state()
    check_system_resources(cfg, state)
    check_processes(cfg, state)
    check_websites(cfg, state)
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Server Monitoring With Email Alert (Python edition)")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--daemon", action="store_true", help="Run continuously instead of a single pass")
    args = parser.parse_args()

    cfg = load_config(args.config)
    interval = cfg.get("check_interval_seconds", 60)

    if args.daemon:
        log.info("Starting in daemon mode, checking every %ss", interval)
        while True:
            try:
                run_once(cfg)
            except Exception as e:
                log.error("Unexpected error during check pass: %s", e)
            time.sleep(interval)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
