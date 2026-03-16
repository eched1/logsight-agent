#!/usr/bin/env python3
"""LogSight Agent — lightweight log shipper for customer machines.

Tails log files, parses common formats, and ships to LogSight API in batches.
Single file, no external dependencies beyond Python 3.8+ stdlib.

Usage:
    python3 logsight-agent.py --config /etc/logsight/agent.yaml
    python3 logsight-agent.py --endpoint https://logsight-api.home.arpa \
        --username ops --password secret --source-id abc123 \
        --watch /var/log/syslog:syslog_bsd --watch /var/log/app.log:json
"""

import argparse
import datetime
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

__version__ = "0.1.0"
LOG = logging.getLogger("logsight-agent")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL = 5  # seconds
DEFAULT_ENDPOINT = "https://logsight-api.home.arpa"

# ---------------------------------------------------------------------------
# Parsers — extract structured fields from raw log lines
# ---------------------------------------------------------------------------

_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_BSD_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<service>[^\[:]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$"
)
_RFC5424_RE = re.compile(
    r"^<\d+>\d?\s*(?P<ts>\S+)\s+(?P<host>\S+)\s+(?P<app>\S+)\s+(?P<pid>\S+)\s+(?P<msgid>\S+)\s+"
    r"(?P<sd>(?:\[.*?\])*|-)\s*(?P<msg>.*)$"
)
_LEVEL_MAP = {
    "emerg": "CRITICAL", "alert": "CRITICAL", "crit": "CRITICAL",
    "err": "ERROR", "error": "ERROR",
    "warn": "WARNING", "warning": "WARNING",
    "notice": "INFO", "info": "INFO",
    "debug": "DEBUG",
}
_YEAR = datetime.datetime.now().year


def parse_syslog_bsd(line: str) -> dict:
    m = _BSD_RE.match(line)
    if not m:
        return {"message": line, "level": "INFO"}
    g = m.groupdict()
    ts = datetime.datetime(
        _YEAR, _MONTH_MAP.get(g["mon"], 1), int(g["day"]),
        int(g["H"]), int(g["M"]), int(g["S"]),
    ).isoformat()
    return {
        "timestamp": ts,
        "host": g["host"],
        "service": g["service"].strip(),
        "message": g["msg"],
        "level": _guess_level(g["msg"]),
        "raw": line,
    }


def parse_syslog_5424(line: str) -> dict:
    m = _RFC5424_RE.match(line)
    if not m:
        return {"message": line, "level": "INFO"}
    g = m.groupdict()
    return {
        "timestamp": g["ts"],
        "host": g["host"],
        "service": g["app"],
        "message": g["msg"],
        "level": _guess_level(g["msg"]),
        "raw": line,
    }


def parse_json(line: str) -> dict:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {"message": line, "level": "INFO"}
    return {
        "timestamp": obj.get("timestamp") or obj.get("ts") or obj.get("@timestamp"),
        "level": str(obj.get("level") or obj.get("severity") or "INFO").upper(),
        "message": obj.get("message") or obj.get("msg") or line,
        "host": obj.get("host") or obj.get("hostname"),
        "service": obj.get("service") or obj.get("app") or obj.get("logger"),
        "raw": line,
        "metadata_json": {k: v for k, v in obj.items()
                         if k not in ("timestamp", "ts", "@timestamp", "level",
                                      "severity", "message", "msg", "host",
                                      "hostname", "service", "app", "logger")},
    }


def parse_plain(line: str) -> dict:
    return {
        "message": line,
        "level": _guess_level(line),
        "raw": line,
    }


def _guess_level(msg: str) -> str:
    msg_lower = msg.lower()
    for keyword, level in _LEVEL_MAP.items():
        if keyword in msg_lower:
            return level
    return "INFO"


PARSERS = {
    "syslog_bsd": parse_syslog_bsd,
    "syslog_5424": parse_syslog_5424,
    "json": parse_json,
    "plain": parse_plain,
    "auto": None,  # try all parsers
}


def auto_parse(line: str) -> dict:
    """Try parsers in order: JSON → RFC5424 → BSD → plain."""
    if line.lstrip().startswith("{"):
        result = parse_json(line)
        if result.get("raw") != result.get("message"):
            return result
    if line.startswith("<"):
        result = parse_syslog_5424(line)
        if result.get("host"):
            return result
    result = parse_syslog_bsd(line)
    if result.get("host"):
        return result
    return parse_plain(line)


# ---------------------------------------------------------------------------
# File tailer — reads new lines from log files
# ---------------------------------------------------------------------------

class FileTailer:
    """Tail a file, tracking position across rotations."""

    def __init__(self, path: str, parser_name: str = "auto"):
        self.path = path
        self.parser = PARSERS.get(parser_name) or auto_parse
        self._fh = None
        self._inode = None
        self._pos = 0

    def open(self, from_end: bool = True):
        try:
            self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
            stat = os.fstat(self._fh.fileno())
            self._inode = stat.st_ino
            if from_end:
                self._fh.seek(0, 2)
                self._pos = self._fh.tell()
        except FileNotFoundError:
            LOG.warning("File not found: %s (will retry)", self.path)

    def read_lines(self) -> list[dict]:
        if self._fh is None:
            self.open(from_end=True)
            if self._fh is None:
                return []

        # Check for file rotation (inode changed)
        try:
            stat = os.stat(self.path)
            if stat.st_ino != self._inode:
                LOG.info("File rotated: %s", self.path)
                self._fh.close()
                self.open(from_end=False)
                if self._fh is None:
                    return []
        except FileNotFoundError:
            return []

        lines = []
        for raw_line in self._fh:
            raw_line = raw_line.rstrip("\n\r")
            if not raw_line:
                continue
            parsed = self.parser(raw_line)
            parsed.setdefault("host", os.uname().nodename)
            lines.append(parsed)

        self._pos = self._fh.tell()
        return lines

    def close(self):
        if self._fh:
            self._fh.close()


# ---------------------------------------------------------------------------
# API client — ships batches to LogSight
# ---------------------------------------------------------------------------

class LogSightClient:
    """Minimal HTTP client for LogSight API (stdlib only)."""

    def __init__(self, endpoint: str, username: str, password: str, source_id: str,
                 verify_ssl: bool = True):
        self.endpoint = endpoint.rstrip("/")
        self.username = username
        self.password = password
        self.source_id = source_id
        self.verify_ssl = verify_ssl
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    def _login(self):
        data = json.dumps({"email": self.username, "username": self.username, "password": self.password}).encode()
        req = urllib.request.Request(
            f"{self.endpoint}/api/v1/auth/login",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            import ssl
            ctx = ssl.create_default_context()
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, context=ctx, timeout=10)
            body = json.loads(resp.read())
            self._token = body["access_token"]
            self._token_expiry = time.time() + 1500  # refresh before 30min
            LOG.info("Authenticated to LogSight as %s", self.username)
        except Exception as e:
            LOG.error("Login failed: %s", e)
            raise

    def _ensure_token(self):
        if not self._token or time.time() > self._token_expiry:
            self._login()

    def ship(self, logs: list[dict]) -> bool:
        if not logs:
            return True
        self._ensure_token()
        data = json.dumps({"logs": logs}).encode()
        req = urllib.request.Request(
            f"{self.endpoint}/api/v1/logs/ingest/{self.source_id}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            import ssl
            ctx = ssl.create_default_context()
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, context=ctx, timeout=15)
            body = json.loads(resp.read())
            LOG.debug("Shipped %d logs → ingested: %s", len(logs), body.get("ingested"))
            return True
        except urllib.error.HTTPError as e:
            LOG.error("Ship failed (%d): %s", e.code, e.read().decode()[:200])
            if e.code == 401:
                self._token = None  # force re-login
            return False
        except Exception as e:
            LOG.error("Ship failed: %s", e)
            return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def load_yaml_config(path: str) -> dict:
    """Minimal YAML-like config parser (no PyYAML dependency)."""
    config = {}
    current_list = None
    current_item = {}

    with open(path) as f:
        for line in f:
            line = line.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue

            # List item
            if line.startswith("  - "):
                if current_item and current_list is not None:
                    config.setdefault(current_list, []).append(current_item)
                current_item = {}
                kv = line[4:].split(":", 1)
                if len(kv) == 2:
                    current_item[kv[0].strip()] = kv[1].strip()
                continue

            # Nested key under list item
            if line.startswith("    ") and current_list:
                kv = line.strip().split(":", 1)
                if len(kv) == 2:
                    current_item[kv[0].strip()] = kv[1].strip()
                continue

            # Top-level key
            kv = line.split(":", 1)
            if len(kv) == 2:
                key = kv[0].strip()
                val = kv[1].strip()
                if val:
                    config[key] = val
                else:
                    current_list = key
                    if current_item and current_list:
                        config.setdefault(current_list, []).append(current_item)
                    current_item = {}

    if current_item and current_list:
        config.setdefault(current_list, []).append(current_item)

    return config


def main():
    parser = argparse.ArgumentParser(description="LogSight Agent")
    parser.add_argument("--config", help="Path to YAML config file")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--source-id")
    parser.add_argument("--watch", action="append", help="path:format (e.g. /var/log/syslog:syslog_bsd)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--flush-interval", type=int, default=DEFAULT_FLUSH_INTERVAL)
    parser.add_argument("--no-verify-ssl", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load config file or CLI args
    if args.config:
        cfg = load_yaml_config(args.config)
        endpoint = cfg.get("endpoint", args.endpoint)
        username = cfg.get("username", args.username)
        password = cfg.get("password", args.password)
        source_id = cfg.get("source_id", args.source_id)
        watches = cfg.get("watch", [])
        batch_size = int(cfg.get("batch_size", args.batch_size))
        flush_interval = int(cfg.get("flush_interval", args.flush_interval))
        verify_ssl = cfg.get("verify_ssl", "true").lower() != "false"
    else:
        endpoint = args.endpoint
        username = args.username
        password = args.password
        source_id = args.source_id
        watches = []
        if args.watch:
            for w in args.watch:
                parts = w.rsplit(":", 1)
                watches.append({"path": parts[0], "format": parts[1] if len(parts) > 1 else "auto"})
        batch_size = args.batch_size
        flush_interval = args.flush_interval
        verify_ssl = not args.no_verify_ssl

    if not all([username, password, source_id]):
        LOG.error("Missing required: --username, --password, --source-id (or config file)")
        sys.exit(1)

    if not watches:
        LOG.error("No log files to watch. Use --watch or config file.")
        sys.exit(1)

    # Initialize
    client = LogSightClient(endpoint, username, password, source_id, verify_ssl)
    tailers = []
    for w in watches:
        path = w.get("path", w) if isinstance(w, dict) else w
        fmt = w.get("format", "auto") if isinstance(w, dict) else "auto"
        # Expand globs
        from glob import glob
        matched = sorted(glob(path))
        if not matched:
            LOG.warning("No files match: %s", path)
            matched = [path]  # will retry
        for fpath in matched:
            LOG.info("Watching: %s (format: %s)", fpath, fmt)
            tailer = FileTailer(fpath, fmt)
            tailer.open(from_end=True)
            tailers.append(tailer)

    # Graceful shutdown
    running = True
    def _shutdown(sig, frame):
        nonlocal running
        LOG.info("Shutting down (signal %d)...", sig)
        running = False
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    LOG.info("LogSight Agent v%s started — %d file(s), batch=%d, flush=%ds",
             __version__, len(tailers), batch_size, flush_interval)

    # Main loop
    buffer = []
    last_flush = time.time()
    shipped_total = 0

    while running:
        for tailer in tailers:
            lines = tailer.read_lines()
            buffer.extend(lines)

        # Flush when buffer full or interval elapsed
        now = time.time()
        if len(buffer) >= batch_size or (buffer and now - last_flush >= flush_interval):
            batch = buffer[:batch_size]
            if client.ship(batch):
                shipped_total += len(batch)
                buffer = buffer[batch_size:]
            last_flush = now

        time.sleep(0.5)

    # Final flush
    if buffer:
        client.ship(buffer)
        shipped_total += len(buffer)

    for t in tailers:
        t.close()
    LOG.info("Agent stopped. Total shipped: %d logs", shipped_total)


if __name__ == "__main__":
    main()
