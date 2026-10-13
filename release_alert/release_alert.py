
import fcntl
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import apprise
import feedparser
import yaml


CONFIG_FILE = "/data/config.yml"
DB_FILE = "/data/feeds.db"
LOCK_FILE = "/data/releasealert.lock"
VERSION = "v0.1"


def log(message):
    print(message, flush=True)


def load_config(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError("Configuration must be a YAML mapping.")

    webhooks = config.get("apprise_webhooks", [])
    feeds = config.get("feeds")

    if not isinstance(webhooks, list):
        raise ValueError("'apprise_webhooks' must be a list.")

    if not isinstance(feeds, list) or not feeds:
        raise ValueError("'feeds' must contain at least one feed.")

    webhook_names = set()

    for webhook in webhooks:
        if not isinstance(webhook, dict):
            raise ValueError("Each webhook must have a name and URL.")

        name = webhook.get("name")
        url = webhook.get("url")

        if not isinstance(name, str) or not name.strip():
            raise ValueError("Every webhook needs a nonempty name.")

        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"Webhook '{name}' has no URL.")

        if name in webhook_names:
            raise ValueError(f"Duplicate webhook name: '{name}'")

        webhook_names.add(name)

    timeout = config.get("request_timeout_seconds", 30)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ValueError("'request_timeout_seconds' must be positive.")

    feed_names = set()

    for feed in feeds:
        if not isinstance(feed, dict):
            raise ValueError("Each feed must be a YAML mapping.")

        name = feed.get("name")
        url = feed.get("feed_url")

        if not isinstance(name, str) or not name.strip():
            raise ValueError("Every feed needs a nonempty name.")

        if name in feed_names:
            raise ValueError(f"Duplicate feed name: '{name}'")
        feed_names.add(name)

        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"Feed '{name}' has no feed_url.")

        for key in ("include_filter", "exclude_filter"):
            patterns = feed.get(key, [])

            if not isinstance(patterns, list):
                raise ValueError(f"'{key}' must be a list in feed '{name}'.")

            for pattern in patterns:
                if not isinstance(pattern, str):
                    raise ValueError(f"Non-string regex in feed '{name}'.")

                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"Invalid regex in feed '{name}': "
                        f"{pattern!r}: {exc}"
                    ) from exc

    return config


@contextmanager
def acquire_lock():
    with open(LOCK_FILE, "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            log("Another release-alert run is active; skipping.")
            yield False
            return

        yield True


def init_db(conn):
    # The database stores only URLs that have already been seen.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_entries (
            entry_url TEXT PRIMARY KEY,
            feed_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def fetch_feed(url, timeout):
    request = Request(
        url,
        headers={"User-Agent": f"ReleaseAlert/{VERSION}"},
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"HTTP status {response.status}")
            body = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Unable to fetch {url}: {exc}") from exc

    parsed = feedparser.parse(body)

    if parsed.bozo:
        raise RuntimeError(
            f"Unable to parse {url}: "
            f"{parsed.get('bozo_exception', 'unknown parsing error')}"
        )

    return parsed


def matches_filters(title, includes, excludes):
    if includes and not any(
        re.search(pattern, title) for pattern in includes
    ):
        return False

    if any(re.search(pattern, title) for pattern in excludes):
        return False

    return True


def is_url_seen(conn, url):
    return conn.execute(
        "SELECT 1 FROM processed_entries WHERE entry_url = ?",
        (url,),
    ).fetchone() is not None


def mark_url_seen(conn, feed_name, url):
    conn.execute("""
        INSERT OR IGNORE INTO processed_entries (entry_url, feed_name)
        VALUES (?, ?)
    """, (url, feed_name))


def escape_markdown_title(title):
    # Escape Markdown characters that could break link text.
    for char in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "|"):
        title = title.replace(char, "\\" + char)
    return title


def send_notifications(webhooks, digest_body):
    failures = []

    if not webhooks:
        log("No webhooks configured; console-only mode.")
        return failures

    for webhook in webhooks:
        name = webhook["name"]

        try:
            notifier = apprise.Apprise()

            if not notifier.add(webhook["url"]):
                raise RuntimeError("Apprise rejected the webhook URL.")

            sent = notifier.notify(
                title="Digest: New Container Release Updates",
                body=digest_body,
                body_format=apprise.NotifyFormat.MARKDOWN,
            )

            if not sent:
                raise RuntimeError("Apprise reported notification failure.")

            log(f"[{name}] Notification sent successfully.")

        except Exception as exc:
            log(f"ERROR [{name}]: Notification failed: {exc}")
            failures.append(name)

    return failures


def main():
    config = load_config(CONFIG_FILE)
    timeout = config.get("request_timeout_seconds", 30)

    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)

    with acquire_lock() as acquired:
        if not acquired:
            return 0

        with sqlite3.connect(DB_FILE, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            init_db(conn)

            # Baseline is inferred from the URL table; no extra state is stored.
            baseline_mode = (
                config.get("baseline_on_first_run", True)
                and conn.execute(
                    "SELECT COUNT(*) FROM processed_entries"
                ).fetchone()[0] == 0
            )

            if baseline_mode:
                log("First-run baseline: existing releases will be recorded "
                    "without notifications.")

            # Fetch all feeds first. If the initial baseline is incomplete,
            # leave the database untouched so the next run can retry it.
            fetched_feeds = []
            feed_failures = []

            for feed in config["feeds"]:
                name = feed["name"]
                log(f"\n--- Checking: {name} ---")

                try:
                    parsed = fetch_feed(feed["feed_url"], timeout)
                    fetched_feeds.append((feed, parsed))
                    log(f"Found {len(parsed.entries)} entries.")
                except Exception as exc:
                    log(f"ERROR [{name}]: {exc}")
                    feed_failures.append(name)

            if baseline_mode and feed_failures:
                log("Baseline aborted because one or more feeds failed. "
                    "No URLs were recorded.")
                return 1

            digest_updates = []

            for feed, parsed in fetched_feeds:
                name = feed["name"]

                for entry in parsed.entries:
                    title = entry.get("title", "No Title")
                    url = entry.get("link") or entry.get("id")

                    if not url:
                        log(f"WARNING [{name}]: Entry without URL: {title}")
                        continue

                    if is_url_seen(conn, url):
                        continue

                    matches = matches_filters(
                        title,
                        feed.get("include_filter", []),
                        feed.get("exclude_filter", []),
                    )

                    if baseline_mode:
                        log(f"  [BASELINE] {name} {title}")

                    elif matches:
                        log(f"  [NEW RELEASE] {name} {title}")
                        digest_updates.append({
                            "feed_name": name,
                            "title": title,
                            "url": url,
                        })

                    else:
                        log(f"  [EXCLUDED] {name} {title}")

                    # Every newly encountered URL is recorded, including
                    # excluded releases and releases whose notification fails.
                    mark_url_seen(conn, name, url)

                conn.commit()

            if baseline_mode:
                log("Initial baseline completed successfully.")

            webhook_failures = []

            if digest_updates:
                digest_lines = ["### New Software Releases", ""]

                for item in digest_updates:
                    title = escape_markdown_title(item["title"])
                    digest_lines.append(
                        f"- **{item['feed_name']}**: "
                        f"[{title}](<{item['url']}>)"
                    )

                digest_body = "\n".join(digest_lines)

                log("\n=== New Releases Found ===")
                log(digest_body)

                webhook_failures = send_notifications(
                    config.get("apprise_webhooks", []),
                    digest_body,
                )
            elif not baseline_mode:
                log("\nNo new matching releases across all feeds.")

            if feed_failures or webhook_failures:
                log("\nRun completed with errors.")

                if feed_failures:
                    log("Failed feeds: " + ", ".join(feed_failures))

                if webhook_failures:
                    log("Failed webhooks: " + ", ".join(webhook_failures))

                return 1

            log("\nRun completed successfully.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"FATAL ERROR: {exc}")
        sys.exit(1)
