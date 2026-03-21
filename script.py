from __future__ import annotations

import argparse
import csv
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from base64 import b64decode, b64encode
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import requests

APP_SLUG = "veracode-workflow-app"
INTEGRATION_REPO_NAME = "veracode"
INTEGRATION_SOURCE_URL = "https://github.com/veracode/github-actions-integration.git"
API_VER = "2022-11-28"

# Pre-compiled regex constants (compiled once at module load, not per call)
_TEAMS_INJECT_RE = re.compile(
    r"([ \t]*(?:-[ \t]+)?uses:[ \t]+veracode/(?:veracode-)?uploadandscan-action@[^\n]+\n"
    r"(?:[ \t]+[^\n]+\n)*?"
    r"[ \t]+with:\n)"
    r"((?:[ \t]+[^\n]+\n)+)",
    re.MULTILINE,
)
# Detect a pre-existing `teams:` entry so we don't inject a duplicate.
_TEAMS_ALREADY_SET_RE = re.compile(r"^\s+teams\s*:", re.MULTILINE)

# Secret names referenced in multiple places — defined once to avoid string duplication
_VERACODE_SECRET_NAMES: tuple[str, ...] = (
    "VERACODE_API_ID",
    "VERACODE_API_KEY",
    "VERACODE_AGENT_TOKEN",
)

# Thread-safety primitives
_print_lock = threading.Lock()
_rate_limit_lock = threading.Lock()
_rate_limit_pause_until: float = 0.0


# ---------------------------------------------------------------------------
# Per-org output buffer
# ---------------------------------------------------------------------------

class OrgBuffer:
    """Collects log lines for one org and flushes them atomically to stdout.

    In sequential mode (workers == 1) flush_on_add=True streams lines
    immediately, preserving the original behaviour. In parallel mode
    flush_on_add=False buffers everything until flush() is called at the
    end of process_org, so output for one org always appears as a single
    contiguous block.
    """

    def __init__(self, org: str, org_idx: int, total_orgs: int, flush_on_add: bool = False) -> None:
        self.org = org
        self.org_idx = org_idx
        self.total_orgs = total_orgs
        self.flush_on_add = flush_on_add
        self._lines: list[str] = []

    def add(self, msg: str) -> None:
        if self.flush_on_add:
            with _print_lock:
                print(msg, flush=True)
        else:
            self._lines.append(msg)

    def flush(self) -> None:
        """Print all buffered lines atomically. No-op in flush_on_add mode."""
        if self.flush_on_add or not self._lines:
            return
        pct = (self.org_idx / self.total_orgs * 100) if self.total_orgs else 100.0
        header = f"\n[{self.org_idx}/{self.total_orgs} ({pct:.1f}%)] {self.org}"
        block = "\n".join([header] + self._lines)
        try:
            with _print_lock:
                print(block, flush=True)
        finally:
            self._lines.clear()


# ---------------------------------------------------------------------------
# Live progress display for parallel mode
# ---------------------------------------------------------------------------

class ProgressDisplay:
    """Maintains a live N-line worker status block at the bottom of the terminal.

    Uses ANSI escape codes to redraw in place. Automatically disabled when
    stdout is not a TTY (CI, redirected output) so it degrades safely.

    The display is owned by the main thread. Workers call update() and
    clear_slot() which are thread-safe via an internal lock.
    """

    def __init__(self, workers: int) -> None:
        self._workers = workers
        self._slots: dict[int, str] = {}
        self._lock = threading.Lock()
        self._active = sys.stdout.isatty() and workers > 1
        if self._active:
            print("\n" * workers, end="", flush=True)

    def _redraw(self) -> None:
        """Redraw all worker slot lines in place. Must be called under self._lock."""
        if not self._active:
            return
        # Move cursor up by workers lines, redraw each slot line
        lines = []
        for i in range(self._workers):
            lines.append(self._slots.get(i, ""))
        # Move up N lines then overwrite
        up = f"\x1b[{self._workers}A"
        body = "\n".join(f"\x1b[2K{line}" for line in lines)
        sys.stdout.write(up + body + "\n")
        sys.stdout.flush()

    def update(self, slot_id: int, org: str, elapsed: float) -> None:
        """Set the status line for a worker slot."""
        if not self._active:
            return
        with self._lock:
            self._slots[slot_id] = f"  [worker {slot_id + 1}] {org} ... {elapsed:.0f}s"
            self._redraw()

    def clear_slot(self, slot_id: int) -> None:
        """Clear a worker slot when its org completes."""
        if not self._active:
            return
        with self._lock:
            self._slots.pop(slot_id, None)
            self._redraw()

    def stop(self) -> None:
        """Clear all slots and move cursor past the display area."""
        if not self._active:
            return
        with self._lock:
            self._slots.clear()
            self._redraw()


# ---------------------------------------------------------------------------
# Typed stats container
# ---------------------------------------------------------------------------

@dataclass
class RunStats:
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime | None = None
    total_orgs: int = 0
    processed: int = 0
    repo_success: int = 0
    repo_fail: int = 0
    app_installed: int = 0
    app_missing: int = 0
    secrets_success: int = 0
    secrets_fail: int = 0
    secrets_checked: int = 0
    secrets_all_exist: int = 0
    secrets_partial: int = 0
    secrets_all_missing: int = 0
    secrets_no_permission: int = 0
    yml_updated: int = 0
    yml_skipped: int = 0
    yml_failed: int = 0


# ---------------------------------------------------------------------------
# Shared run context
# All values are passed explicitly so every function is testable in isolation.
# Locks are fields rather than module-level variables so multiple contexts can
# coexist in tests without cross-contamination.
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    api_base: str
    web_base: str
    token: str
    do_apply_repo: bool
    do_set_secrets: bool
    do_set_teams: bool
    do_update_yml: bool
    dry_run: bool
    teams_mode: Literal["auto", "file", "hybrid", "none"]  # teams injection strategy
    yml_content: str | None             # content for --update-veracode-yml
    onboarding_yml_content: str | None  # content for --import-repo post-steps
    teams_map: dict[str, str]
    team_prefix: str
    veracode_api_id: str | None
    veracode_api_key: str | None
    veracode_sa_api_id: str | None
    veracode_sa_api_key: str | None
    total_orgs: int
    report_path: Path
    checkpoint_file: Path
    stats: RunStats = field(default_factory=RunStats)
    stats_lock: threading.Lock = field(default_factory=threading.Lock)
    rows_lock: threading.Lock = field(default_factory=threading.Lock)
    report_lock: threading.Lock = field(default_factory=threading.Lock)
    checkpoint_lock: threading.Lock = field(default_factory=threading.Lock)
    missing_repo_rows: list[list[str]] = field(default_factory=list)
    missing_app_rows: list[list[str]] = field(default_factory=list)
    manual_links_rows: list[list[str]] = field(default_factory=list)
    completed_orgs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate invariants implied by flag combinations."""
        if self.do_set_secrets:
            if not self.veracode_api_id or not self.veracode_api_key:
                raise ValueError("do_set_secrets requires veracode_api_id and veracode_api_key")
            if not self.veracode_sa_api_id or not self.veracode_sa_api_key:
                raise ValueError("do_set_secrets requires veracode_sa_api_id and veracode_sa_api_key")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def tprint(*args: Any, **kwargs: Any) -> None:
    """Thread-safe print. Safe to call from any thread, including the main thread."""
    with _print_lock:
        print(*args, **kwargs)


def env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VER,
        "User-Agent": "veracode-workflow-rollout-helper",
    }


# ---------------------------------------------------------------------------
# Rate limit (shared across all threads)
# ---------------------------------------------------------------------------

def check_rate_limit(response: requests.Response) -> None:
    global _rate_limit_pause_until

    remaining_hdr = response.headers.get("X-RateLimit-Remaining")
    reset_hdr = response.headers.get("X-RateLimit-Reset")
    if not remaining_hdr or not reset_hdr:
        return

    remaining = int(remaining_hdr)
    reset_time = int(reset_hdr)

    # Warn at 100 remaining and every 10 below that to avoid flooding parallel logs.
    if remaining < 100 and remaining % 10 == 0:
        reset_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset_time))
        tprint(f"  [WARNING] Rate limit low: {remaining} requests remaining (resets at {reset_dt})")

    if remaining < 10:
        # Take a single clock sample inside the lock so resume_at is consistent.
        with _rate_limit_lock:
            now = time.time()
            wait_seconds = max(reset_time - int(now), 0) + 5
            resume_at = now + wait_seconds
            if resume_at > _rate_limit_pause_until:
                _rate_limit_pause_until = resume_at
                tprint(f"  [RATE LIMIT] Pausing {wait_seconds}s until rate limit resets...")
            else:
                wait_seconds = max(_rate_limit_pause_until - now, 0)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    else:
        with _rate_limit_lock:
            pause = max(_rate_limit_pause_until - time.time(), 0)
        if pause > 0:
            time.sleep(pause)


# ---------------------------------------------------------------------------
# Unified retry core
# ---------------------------------------------------------------------------

def _retry_request(
    make_request: Callable[[], requests.Response],
    label: str,
    max_retries: int = 3,
) -> requests.Response:
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")
    for attempt in range(max_retries):
        try:
            r = make_request()
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                if attempt < max_retries - 1:
                    tprint(f"  [{label}] 429, waiting {retry_after}s (retry {attempt + 1}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                return r
            if r.status_code >= 500:
                if attempt < max_retries - 1:
                    wait = (2 ** attempt) * 2
                    tprint(f"  [{label}] {r.status_code}, waiting {wait}s (retry {attempt + 1}/{max_retries})...")
                    time.sleep(wait)
                    continue
                return r
            return r
        except (requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
            if attempt < max_retries - 1:
                wait = (2 ** attempt) * 2
                label_exc = "timeout" if isinstance(exc, requests.exceptions.Timeout) else str(exc)[:50]
                tprint(f"  [{label}] {label_exc}, waiting {wait}s (retry {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue
            raise
    # Never reached — loop always returns or raises on the last attempt.
    assert False, "unreachable"  # pragma: no cover


def request(method: str, url: str, token: str, max_retries: int = 3, **kwargs: Any) -> requests.Response:
    def make() -> requests.Response:
        r = requests.request(method, url, headers=gh_headers(token), timeout=45, **kwargs)
        check_rate_limit(r)
        return r
    return _retry_request(make, "GITHUB", max_retries)


def veracode_request(
    method: str,
    endpoint: str,
    api_id: str,
    api_key: str,
    max_retries: int = 3,
    **kwargs: Any,
) -> requests.Response:
    from veracode_api_signing.plugin_requests import RequestsAuthPluginVeracodeHMAC
    url = f"https://api.veracode.com{endpoint}"
    auth = RequestsAuthPluginVeracodeHMAC(api_key_id=api_id, api_key_secret=api_key)

    def make() -> requests.Response:
        return requests.request(method, url, auth=auth, timeout=45, **kwargs)
    return _retry_request(make, "VERACODE", max_retries)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def parse_link_next(link_header: str) -> str | None:
    for part in (p.strip() for p in link_header.split(",")):
        if 'rel="next"' in part:
            left = part.split(";")[0].strip()
            if left.startswith("<") and left.endswith(">"):
                return left[1:-1]
    return None


def paginate_list(url: str, token: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    next_url: str | None = url
    while next_url:
        r = request("GET", next_url, token, params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {next_url} failed: {r.status_code} {r.text}")
        data = r.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Expected list from {next_url}, got {type(data)}")
        yield from data
        link = r.headers.get("Link") or r.headers.get("link")
        next_url = parse_link_next(link) if link else None
        params = None


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(header)
        writer.writerows(rows)


def append_report_entry(report_path: Path, entry: dict[str, Any]) -> None:
    """Append one JSONL line. O(1) per write regardless of report size."""
    with report_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry) + "\n")


def finalize_report(report_path: Path) -> None:
    """Convert JSONL to a pretty-printed JSON array. Called once after all orgs complete.

    Uses an atomic rename so the original JSONL file is preserved intact if the
    process is interrupted during finalization. Corrupt lines are warned about
    instead of silently dropped.
    """
    if not report_path.exists():
        return
    entries: list[Any] = []
    with report_path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    print(f"  [WARNING] Skipping corrupt report line {lineno}: {exc} — {line[:80]}", file=sys.stderr)
    tmp = report_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
    tmp.replace(report_path)  # atomic on POSIX; near-atomic on Windows


def write_teams_map_csv(path: Path, orgs: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(["org", "teams"])
        writer.writerows([org, ""] for org in orgs)


def write_orgs_txt(path: Path, orgs: list[str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.writelines(org + "\n" for org in orgs)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def check_git_available() -> bool:
    """Return True if the git CLI is accessible.

    Cached after the first call — git availability cannot change mid-run.
    Tests that mock subprocess should call check_git_available.cache_clear()
    before and after patching to avoid stale cache state.
    """
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def git_clone_bare(source_url: str) -> tuple[bool, str, str | None]:
    """Clone source_url as a bare repo. Returns (success, message, temp_dir).
    Caller owns cleanup of temp_dir on success."""
    temp_dir: str | None = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="veracode-clone-")
        bare_repo = os.path.join(temp_dir, "repo.git")
        result = subprocess.run(
            ["git", "clone", "--bare", source_url, bare_repo],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"Clone failed: {result.stderr}", None
        out = temp_dir
        temp_dir = None  # transfer ownership to caller; suppress finally cleanup
        return True, "Clone successful", out
    except Exception as exc:
        return False, str(exc), None
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def git_mirror_import(
    source_url: str,
    target_org: str,
    target_repo: str,
    token: str,
    web_base: str = "https://github.com",
    cached_clone_dir: str | None = None,
) -> tuple[bool, str]:
    """Mirror-push the integration repo into target_org/target_repo."""
    temp_dir: str | None = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="veracode-import-")
        bare_repo = os.path.join(temp_dir, "repo.git")

        if cached_clone_dir:
            # Each worker gets its own copy so concurrent pushes don't share git state.
            shutil.copytree(os.path.join(cached_clone_dir, "repo.git"), bare_repo)
        else:
            clone_result = subprocess.run(
                ["git", "clone", "--bare", source_url, bare_repo],
                capture_output=True, text=True,
            )
            if clone_result.returncode != 0:
                return False, f"Clone failed: {clone_result.stderr}"

        host = web_base.rstrip("/").removeprefix("https://").removeprefix("http://")
        target_url = f"https://x-access-token:{token}@{host}/{target_org}/{target_repo}.git"

        push_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        push_result = subprocess.run(
            ["git", "-C", bare_repo, "push", "--mirror", target_url],
            capture_output=True, text=True, env=push_env,
        )
        if push_result.returncode != 0:
            safe_stderr = push_result.stderr.replace(token, "***")
            return False, f"Push failed: {safe_stderr}"

        return True, "Import successful"

    except Exception as exc:
        return False, str(exc)
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Veracode API helpers
# ---------------------------------------------------------------------------

def _find_workspace_by_name(org_name: str, api_id: str, api_key: str) -> str | None:
    """Page through workspaces until exact name match; returns UUID or None."""
    page = 0
    while True:
        r = veracode_request(
            "GET", "/srcclr/v3/workspaces", api_id, api_key,
            params={"filter[workspace]": org_name, "size": 100, "page": page},
        )
        if r.status_code == 401:
            tprint("  [ERROR] Veracode authentication failed - check credentials")
            return None
        if r.status_code == 403:
            tprint("  [ERROR] Veracode permission denied - insufficient access")
            return None
        if r.status_code != 200:
            tprint(f"  [ERROR] Failed to list workspaces: {r.status_code} - {r.text[:200]}")
            return None

        body = r.json()
        for ws in body.get("_embedded", {}).get("workspaces", []):
            if ws.get("name") == org_name:
                ws_id = ws.get("id")
                if not ws_id:
                    tprint(f"  [WARNING] Workspace '{org_name}' matched but has no id in response - skipping")
                    continue
                return ws_id

        page_meta = body.get("page", {})
        total_pages = page_meta.get("total_pages", 1)
        if page >= total_pages - 1:
            break
        page += 1
    return None


def create_veracode_workspace(org_name: str, api_id: str, api_key: str) -> str | None:
    """Create or find an existing workspace. POST returns no ID; resolved via follow-up GET."""
    try:
        existing_id = _find_workspace_by_name(org_name, api_id, api_key)
        if existing_id:
            return existing_id

        r = veracode_request("POST", "/srcclr/v3/workspaces", api_id, api_key, json={"name": org_name})
        if r.status_code not in (200, 201):
            tprint(f"  [ERROR] Failed to create workspace: {r.status_code} - {r.text[:200]}")
            return None

        # Veracode's API is eventually consistent after POST — poll until the workspace
        # becomes visible, up to 3 attempts with 1s between each.
        for attempt in range(3):
            workspace_id = _find_workspace_by_name(org_name, api_id, api_key)
            if workspace_id:
                return workspace_id
            time.sleep(1)
        tprint(f"  [ERROR] Workspace created but not found after 3 lookup attempts for: {org_name}")
        return None
    except Exception as exc:
        tprint(f"  [ERROR] create_veracode_workspace: {exc}")
        return None


def list_veracode_agents(workspace_id: str, api_id: str, api_key: str) -> list[dict[str, Any]] | None:
    """Return the agent list for a workspace, or None on any API error.

    Callers must distinguish None (error) from [] (no agents).
    """
    try:
        r = veracode_request("GET", f"/srcclr/v3/workspaces/{workspace_id}/agents", api_id, api_key)
        if r.status_code == 200:
            return r.json().get("_embedded", {}).get("agents", [])
        return None
    except Exception:
        return None


def create_veracode_agent_token(
    workspace_id: str,
    org_name: str,
    api_id: str,
    api_key: str,
) -> str | None:
    """Regenerate token if agent exists; create one otherwise.
    None from list_veracode_agents is treated as an error, not an empty list.
    """
    try:
        suffix = "-agt"
        max_org_len = 20 - len(suffix)
        truncated_org = org_name[:max_org_len]
        if not truncated_org or not truncated_org[0].isalpha():
            truncated_org = "gh" + truncated_org[:max_org_len - 2]
        agent_name = f"{truncated_org}{suffix}"

        existing_agents = list_veracode_agents(workspace_id, api_id, api_key)
        if existing_agents is None:
            tprint(f"  [ERROR] Could not list agents for workspace {workspace_id} - aborting token creation")
            return None

        for agent in existing_agents:
            if agent.get("name") == agent_name:
                agent_id = agent.get("id")
                regen = veracode_request(
                    "POST",
                    f"/srcclr/v3/workspaces/{workspace_id}/agents/{agent_id}/token:regenerate",
                    api_id, api_key,
                )
                if regen.status_code == 200:
                    access_token = regen.json().get("access_token")
                    if access_token:
                        return access_token
                    tprint("  [ERROR] token:regenerate succeeded but no access_token in response")
                    return None
                tprint(f"  [ERROR] token:regenerate failed: {regen.status_code} - {regen.text[:200]}")
                return None

        r = veracode_request(
            "POST",
            f"/srcclr/v3/workspaces/{workspace_id}/agents",
            api_id, api_key,
            json={"name": agent_name, "agent_type": "CLI"},
        )
        if r.status_code != 200:
            tprint(f"  [ERROR] Failed to create agent: {r.status_code} - {r.text[:200]}")
            return None
        if not r.content:
            tprint("  [ERROR] Agent POST returned empty body")
            return None
        try:
            agent_body = r.json()
        except json.JSONDecodeError:
            tprint("  [ERROR] Failed to parse agent POST response")
            return None

        access_token = agent_body.get("token", {}).get("access_token")
        if access_token:
            return access_token
        tprint(f"  [ERROR] Agent created but no token.access_token in response: {agent_body}")
        return None
    except Exception as exc:
        tprint(f"  [ERROR] create_veracode_agent_token: {exc}")
        return None


# ---------------------------------------------------------------------------
# GitHub secrets helpers
# ---------------------------------------------------------------------------

def get_org_public_key(api_base: str, org: str, token: str) -> tuple[str, str] | None:
    """Fetch the org's Actions public key for secret encryption.

    Returns (key_id, key) as strings, or None on any failure.
    Both fields are validated to be non-empty strings before returning.
    """
    try:
        r = request("GET", f"{api_base}/orgs/{org}/actions/secrets/public-key", token)
        if r.status_code != 200:
            tprint(f"  [{org}] Failed to get public key: HTTP {r.status_code}")
            return None
        data = r.json()
        key_id = str(data.get("key_id") or "")
        key = str(data.get("key") or "")
        if not key_id or not key:
            tprint(f"  [{org}] Public key response missing key_id or key: {data}")
            return None
        return key_id, key
    except Exception as exc:
        tprint(f"  [{org}] Exception fetching public key: {exc}")
        return None


def encrypt_secret(public_key: str, secret_value: str) -> str:
    from nacl import encoding, public as nacl_public
    pk = nacl_public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = nacl_public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")


def secret_exists(api_base: str, org: str, token: str, secret_name: str) -> bool:
    try:
        r = request("GET", f"{api_base}/orgs/{org}/actions/secrets/{secret_name}", token)
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        if r.status_code == 403:
            tprint(f"  [{org}] Cannot check secret {secret_name}: token lacks admin:org scope")
            return False
        tprint(f"  [{org}] Unexpected response checking {secret_name}: {r.status_code}")
        return False
    except Exception as exc:
        tprint(f"  [{org}] Error checking secret {secret_name}: {exc}")
        return False


def check_veracode_secrets_status(api_base: str, org: str, github_token: str) -> dict[str, str]:
    """Read-only check of all three Veracode secrets for one org.

    Returns a dict mapping each secret name to one of:
    'exists', 'missing', 'no_permission', or 'error'.
    """
    results: dict[str, str] = {}
    for secret_name in _VERACODE_SECRET_NAMES:
        try:
            r = request("GET", f"{api_base}/orgs/{org}/actions/secrets/{secret_name}", github_token)
            if r.status_code == 200:
                results[secret_name] = "exists"
            elif r.status_code == 403:
                results[secret_name] = "no_permission"
            elif r.status_code == 404:
                results[secret_name] = "missing"
            else:
                results[secret_name] = "error"
        except Exception:
            results[secret_name] = "error"
    return results


def set_veracode_secrets(
    api_base: str,
    org: str,
    github_token: str,
    veracode_sa_api_id: str,
    veracode_sa_api_key: str,
    veracode_agent_token: str,
) -> tuple[bool, dict[str, str]]:
    """Set all three Veracode secrets. Fetches the org public key once."""
    key_info = get_org_public_key(api_base, org, github_token)
    if not key_info:
        tprint(f"  [{org}] Could not fetch org public key - skipping secrets")
        return False, {s: "failed" for s in _VERACODE_SECRET_NAMES}
    key_id, public_key = key_info

    secrets_to_set = {
        "VERACODE_API_ID": veracode_sa_api_id,
        "VERACODE_API_KEY": veracode_sa_api_key,
        "VERACODE_AGENT_TOKEN": veracode_agent_token,
    }
    results: dict[str, str] = {}
    for secret_name, secret_value in secrets_to_set.items():
        try:
            payload = {
                "encrypted_value": encrypt_secret(public_key, secret_value),
                "key_id": key_id,
                "visibility": "all",
            }
            r = request("PUT", f"{api_base}/orgs/{org}/actions/secrets/{secret_name}", github_token, json=payload)
            ok = r.status_code in (201, 204)
            if not ok:
                tprint(f"    [ERROR] Secret {secret_name} PUT failed: {r.status_code}")
        except Exception as exc:
            tprint(f"    [ERROR] Exception setting secret {secret_name}: {exc}")
            ok = False

        if ok:
            time.sleep(0.5)
            verified = secret_exists(api_base, org, github_token, secret_name)
            results[secret_name] = "set" if verified else "set_unverified"
        else:
            results[secret_name] = "failed"

    all_ok = all(v.startswith("set") for v in results.values())
    return all_ok, results


# ---------------------------------------------------------------------------
# Workflow file injection
# ---------------------------------------------------------------------------

def _inject_teams_regex(content: str, teams_value: str) -> tuple[str, bool]:
    """Inject a `teams:` parameter into every uploadandscan-action `with:` block
    that does not already have one.

    Returns (new_content, was_changed). teams_value is escaped before insertion
    so that any embedded double-quotes produce valid YAML.
    """
    changed = False

    def replacer(m: re.Match) -> str:
        nonlocal changed
        header, body = m.group(1), m.group(2)
        if _TEAMS_ALREADY_SET_RE.search(body):
            return m.group(0)
        first_param = body.splitlines()[0]
        indent = len(first_param) - len(first_param.lstrip())
        changed = True
        safe_value = teams_value.replace('"', '\\"')
        return header + " " * indent + f'teams: "{safe_value}"\n' + body

    return _TEAMS_INJECT_RE.sub(replacer, content), changed


def inject_teams_into_workflows(
    api_base: str, org: str, repo: str, token: str, teams_value: str
) -> tuple[bool, str]:
    workflow_files = [
        ".github/workflows/veracode-sandbox-scan.yml",
        ".github/workflows/veracode-policy-scan.yml",
    ]
    modified_count = 0

    for workflow_path in workflow_files:
        url = f"{api_base}/repos/{org}/{repo}/contents/{workflow_path}"
        r = request("GET", url, token)
        if r.status_code != 200:
            continue

        file_data = r.json()
        sha = file_data.get("sha")
        raw_content = b64decode(file_data.get("content", "")).decode("utf-8")

        try:
            new_content, was_changed = _inject_teams_regex(raw_content, teams_value)
        except Exception as exc:
            tprint(f"  [{org}] Regex injection error for {workflow_path}: {exc}")
            continue

        if not was_changed:
            continue

        payload = {
            "message": f"Add teams parameter to {workflow_path.split('/')[-1]}",
            "content": b64encode(new_content.encode("utf-8")).decode("utf-8"),
            "sha": sha,
            "branch": "main",
        }
        r = request("PUT", url, token, json=payload)
        if r.status_code in (200, 201):
            modified_count += 1
        else:
            tprint(f"  [{org}] Failed to update {workflow_path}: {r.status_code}")

    return (True, f"teams_added_to_{modified_count}_files") if modified_count > 0 else (True, "teams_already_present")


# ---------------------------------------------------------------------------
# veracode.yml helpers
# ---------------------------------------------------------------------------

def _put_veracode_yml_with_backup(
    api_base: str,
    org: str,
    repo: str,
    token: str,
    yml_content: str,
    update_message: str = "Update veracode.yml with new configuration",
) -> tuple[bool, str]:
    """Shared backup-and-update logic used by both inject and update paths."""
    veracode_url = f"{api_base}/repos/{org}/{repo}/contents/veracode.yml"
    default_veracode_url = f"{api_base}/repos/{org}/{repo}/contents/default-veracode.yml"

    r = request("GET", veracode_url, token)
    if r.status_code == 200:
        original_data = r.json()
        original_sha = original_data.get("sha")
        original_content_b64 = original_data.get("content", "")

        r_default = request("GET", default_veracode_url, token)
        backup_payload: dict[str, Any] = {
            "message": "Preserve current veracode.yml as default-veracode.yml before update",
            "content": original_content_b64,
            "branch": "main",
        }
        if r_default.status_code == 200:
            backup_payload["sha"] = r_default.json().get("sha")
        request("PUT", default_veracode_url, token, json=backup_payload)

        r_put = request("PUT", veracode_url, token, json={
            "message": update_message,
            "content": b64encode(yml_content.encode("utf-8")).decode("utf-8"),
            "branch": "main",
            "sha": original_sha,
        })
        return (True, "updated_with_backup") if r_put.status_code in (200, 201) else (False, f"put_failed:{r_put.status_code}")

    if r.status_code == 404:
        r_put = request("PUT", veracode_url, token, json={
            "message": "Add veracode.yml configuration",
            "content": b64encode(yml_content.encode("utf-8")).decode("utf-8"),
            "branch": "main",
        })
        return (True, "created") if r_put.status_code in (200, 201) else (False, f"put_failed:{r_put.status_code}")

    return False, f"get_failed:{r.status_code}"


def fetch_upstream_veracode_yml() -> str | None:
    """Fetch veracode.yml from the upstream integration repo with retry on 5xx.

    Returns the file content as a string, or None if all attempts fail.
    All failure paths are logged; the final failure is logged as an error.
    """
    url = (
        f"https://raw.githubusercontent.com/"
        f"{INTEGRATION_SOURCE_URL.removeprefix('https://github.com/').removesuffix('.git')}"
        f"/main/veracode.yml"
    )
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code < 500:
                print(f"  [ERROR] Failed to fetch upstream veracode.yml: HTTP {r.status_code}", file=sys.stderr)
                return None
            print(f"  [WARNING] Upstream returned {r.status_code}, attempt {attempt + 1}/3", file=sys.stderr)
            if attempt < 2:
                time.sleep((2 ** attempt) * 2)
        except requests.exceptions.RequestException as exc:
            print(f"  [WARNING] Network error fetching upstream veracode.yml: {exc}, attempt {attempt + 1}/3", file=sys.stderr)
            if attempt < 2:
                time.sleep((2 ** attempt) * 2)
    print("  [ERROR] Failed to fetch upstream veracode.yml after 3 attempts", file=sys.stderr)
    return None


def inject_veracode_yml(
    api_base: str, org: str, repo: str, token: str, yml_content: str | None
) -> tuple[bool, str]:
    """Write the onboarding veracode.yml into the integration repo.

    yml_content is the template read at startup; if it is None the template
    was not found next to the script and injection is skipped gracefully.
    """
    if yml_content is None:
        tprint(f"  [{org}] Warning: veracode.yml not found next to script, skipping injection")
        return False, "template_not_found"
    return _put_veracode_yml_with_backup(
        api_base, org, repo, token, yml_content,
        update_message="Update Veracode workflow configuration with custom settings",
    )


def update_veracode_yml_in_repo(
    api_base: str,
    org: str,
    repo: str,
    token: str,
    yml_content: str,
    repo_is_known_present: bool = False,
) -> tuple[bool, str]:
    """Push yml_content to org/repo.
    Accepts repo_is_known_present to skip redundant repo_exists/repo_is_empty calls.
    """
    if not repo_is_known_present:
        if not repo_exists(api_base, org, repo, token):
            tprint(f"  [{org}] Skipping veracode.yml update - repo '{repo}' not found")
            return False, "repo_not_found"
        if repo_is_empty(api_base, org, repo, token):
            tprint(f"  [{org}] Skipping veracode.yml update - repo '{repo}' is empty (not yet imported)")
            return False, "repo_empty"
    return _put_veracode_yml_with_backup(api_base, org, repo, token, yml_content)


# ---------------------------------------------------------------------------
# Org discovery
# When --enterprise and --orgs-file are both provided, enterprise orgs are
# fetched via GraphQL and the file-based filter is applied by the caller (main).
# ---------------------------------------------------------------------------

def list_orgs_graphql(api_base: str, token: str, enterprise: str) -> list[str] | None:
    try:
        graphql_url = (
            "https://api.github.com/graphql"
            if "api.github.com" in api_base
            else f"{api_base.rstrip('/')}/graphql"
        )
        query = """
        query($enterprise: String!, $cursor: String) {
          enterprise(slug: $enterprise) {
            organizations(first: 100, after: $cursor) {
              nodes { login }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        all_orgs: list[str] = []
        cursor: str | None = None
        while True:
            variables: dict[str, Any] = {"enterprise": enterprise}
            if cursor:
                variables["cursor"] = cursor
            r = request("POST", graphql_url, token, json={"query": query, "variables": variables})
            if r.status_code != 200:
                return None
            data = r.json()
            if "errors" in data or not data.get("data", {}).get("enterprise"):
                return None
            orgs_data = data["data"]["enterprise"]["organizations"]
            all_orgs.extend(node["login"] for node in orgs_data.get("nodes", []) if "login" in node)
            page_info = orgs_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return all_orgs or None
    except Exception:
        return None


def list_orgs(api_base: str, token: str, enterprise: str | None, orgs_file: str | None) -> list[str]:
    """Discover the org list from enterprise GraphQL, a file, or /user/orgs.
    When --enterprise and --orgs-file are both provided, enterprise orgs are fetched here
    and the file-based filter is applied by the caller.
    """
    errors: list[str] = []

    if enterprise:
        print(f'Discovering orgs via enterprise GraphQL: enterprise(slug: "{enterprise}")')
        try:
            orgs = list_orgs_graphql(api_base, token, enterprise)
            if orgs:
                print(f"[OK] Found {len(orgs)} orgs via GraphQL")
                return orgs
            print("\n[ERROR] Enterprise GraphQL returned 0 organizations", file=sys.stderr)
            for line in [
                f"Enterprise slug '{enterprise}' may be wrong, or token lacks 'read:enterprise' scope.",
                "Verify: gh auth status",
                f"Check:  https://github.com/enterprises/{enterprise}",
                "Retry without --enterprise to see accessible orgs: python script.py --dry-run",
            ]:
                print(f"  {line}", file=sys.stderr)
            raise RuntimeError(f"Enterprise '{enterprise}' returned no organizations")
        except RuntimeError:
            raise
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Network/API error accessing enterprise: {exc}")
        except Exception as exc:
            raise RuntimeError(f"Enterprise API failed: {exc}")

    if orgs_file:
        print(f"Reading orgs from file: {orgs_file}")
        try:
            with open(orgs_file, encoding="utf-8") as f:
                orgs = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
            if orgs:
                print(f"[OK] Found {len(orgs)} orgs from file")
                return orgs
            errors.append(f"File '{orgs_file}' contains no valid org names")
        except Exception as exc:
            errors.append(f"File read failed: {exc}")

    try:
        print("Discovering orgs via /user/orgs (all orgs the token user belongs to)")
        orgs = [
            o["login"]
            for o in paginate_list(f"{api_base}/user/orgs", token, params={"per_page": 100})
            if "login" in o
        ]
        if orgs:
            print(f"[OK] Found {len(orgs)} orgs via user API")
            return orgs
        errors.append("User API returned no orgs")
    except Exception as exc:
        errors.append(f"User API failed: {exc}")

    print("\n[ERROR] Unable to determine org list. Tried:", file=sys.stderr)
    for i, error in enumerate(errors, 1):
        print(f"   {i}. {error}", file=sys.stderr)
    print("\nTroubleshooting:", file=sys.stderr)
    print("  - Ensure GITHUB_TOKEN is set with a valid token", file=sys.stderr)
    print("  - Verify token has 'read:org' scope", file=sys.stderr)
    print("  - Provide --enterprise <slug> if using GHEC", file=sys.stderr)
    print("  - Provide --orgs-file <path> with one org per line", file=sys.stderr)
    raise RuntimeError("Unable to determine org list. See errors above.")


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------

def repo_exists(api_base: str, org: str, repo: str, token: str) -> bool:
    r = request("GET", f"{api_base}/repos/{org}/{repo}", token)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"{org}/{repo}: repo check failed {r.status_code} {r.text}")


def repo_is_empty(api_base: str, org: str, repo: str, token: str) -> bool:
    try:
        r = request("GET", f"{api_base}/repos/{org}/{repo}/commits", token, params={"per_page": 1})
        if r.status_code == 409:
            return True
        if r.status_code == 200:
            return len(r.json()) == 0
        return False
    except Exception:
        return False


def create_repo(api_base: str, org: str, repo: str, token: str) -> None:
    payload = {
        "name": repo,
        "private": True,
        "auto_init": False,
        "description": "Veracode GitHub Workflow Integration (imported template workflows & config).",
    }
    r = request("POST", f"{api_base}/orgs/{org}/repos", token, json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{org}/{repo}: repo create failed {r.status_code} {r.text}")


def check_main_branch_exists(api_base: str, org: str, repo: str, token: str) -> bool:
    try:
        r = request("GET", f"{api_base}/repos/{org}/{repo}/branches/main", token)
        return r.status_code == 200
    except Exception:
        return False


def wait_for_main_branch(
    api_base: str,
    org: str,
    repo: str,
    token: str,
    timeout: int = 900,
    poll_interval: int = 10,
) -> bool:
    """Poll until the main branch is visible via the API or timeout is reached.

    GitHub processes a mirror push asynchronously — the branch becomes visible
    some time after `git push --mirror` returns success. On large repos or under
    load this can take 30-90 seconds or more.

    Polls every `poll_interval` seconds for up to `timeout` seconds (default 15
    minutes). Returns True if the branch appears, False if the timeout expires.
    """
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        if check_main_branch_exists(api_base, org, repo, token):
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sleep = min(poll_interval, remaining)
        tprint(f"  [{org}] Waiting for main branch... ({attempt * poll_interval}s elapsed, "
               f"up to {int(remaining)}s remaining)")
        time.sleep(sleep)
        attempt += 1
    return False


def ensure_veracode_repo_imported(
    api_base: str,
    org: str,
    token: str,
    do_apply: bool,
    onboarding_yml_content: str | None,
    auto_import: bool = False,
    teams_value: str | None = None,
    web_base: str = "https://github.com",
    cached_clone_dir: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Ensure the Veracode integration repo exists and is populated in org.

    Returns (repo_ok, details). details contains status fields and, on success,
    an internal '_repo_confirmed_present' key that callers pop to skip redundant
    existence checks for downstream operations.
    """
    details: dict[str, Any] = {"repo": INTEGRATION_REPO_NAME}
    exists = repo_exists(api_base, org, INTEGRATION_REPO_NAME, token)
    is_empty = exists and repo_is_empty(api_base, org, INTEGRATION_REPO_NAME, token)

    def _run_post_import_steps() -> None:
        default_yml_url = f"{api_base}/repos/{org}/{INTEGRATION_REPO_NAME}/contents/default-veracode.yml"
        if request("GET", default_yml_url, token).status_code == 200:
            return
        _, yml_action = inject_veracode_yml(api_base, org, INTEGRATION_REPO_NAME, token, onboarding_yml_content)
        details["veracode_yml_injected"] = yml_action
        if teams_value:
            _, teams_msg = inject_teams_into_workflows(
                api_base, org, INTEGRATION_REPO_NAME, token, teams_value
            )
            details["teams_injection"] = teams_msg

    if exists and not is_empty:
        details["status"] = "repo_exists"
        details["_repo_confirmed_present"] = True
        if do_apply:
            _run_post_import_steps()
            if "veracode_yml_injected" in details:
                details["status"] = "repo_exists_post_import_incomplete"
        return True, details

    if is_empty:
        details["was_empty"] = True

    details["status"] = "missing"
    if not do_apply:
        details["note"] = "dry_run_only"
        return False, details

    if not exists:
        create_repo(api_base, org, INTEGRATION_REPO_NAME, token)
        details["created"] = True

    if auto_import:
        if not check_git_available():
            tprint(f"  [{org}] Git CLI not available - skipping auto import")
        else:
            ok, message = git_mirror_import(
                INTEGRATION_SOURCE_URL, org, INTEGRATION_REPO_NAME, token, web_base, cached_clone_dir
            )
            if ok:
                tprint(f"  [{org}] Push succeeded — waiting for GitHub to process the import (up to 15 min)...")
                branch_visible = wait_for_main_branch(api_base, org, INTEGRATION_REPO_NAME, token)
                if branch_visible:
                    details["status"] = "repo_created_and_imported"
                    details["import_method"] = "git_cli_auto"
                    details["_repo_confirmed_present"] = True
                    _run_post_import_steps()
                    return True, details
                tprint(f"  [{org}] Warning: main branch not visible after 15 minutes — "
                       f"GitHub may still be processing. Re-run to complete post-import steps.")
                details["status"] = "repo_created_import_incomplete"
                details["import_method"] = "git_cli_auto"
                return True, details
            else:
                tprint(f"  [{org}] Auto import failed: {message}")
                # fall through to manual import path

    details["status"] = "repo_created_manual_import_required"
    details["import_instructions"] = {
        "web_importer_url": f"{web_base.rstrip('/')}/{org}/{INTEGRATION_REPO_NAME}/import",
        "source_url": INTEGRATION_SOURCE_URL,
        "note": "Manual import required - use GitHub web UI",
    }
    return False, details


# ---------------------------------------------------------------------------
# App installation helpers
# ---------------------------------------------------------------------------

def list_org_installations(api_base: str, org: str, token: str) -> list[dict[str, Any]]:
    """Return all installed GitHub Apps for org.

    Requests page size 100 (the API maximum) to avoid truncation for orgs with
    many installed apps. Pagination beyond 100 is not handled here but is
    extremely rare in practice.
    """
    r = request("GET", f"{api_base}/orgs/{org}/installations", token,
                params={"per_page": 100})
    if r.status_code >= 400:
        raise RuntimeError(f"{org}: cannot list installations ({r.status_code}) {r.text}")
    return r.json().get("installations", [])


def find_app_installation(api_base: str, org: str, token: str, app_slug: str) -> dict[str, Any] | None:
    for inst in list_org_installations(api_base, org, token):
        slug = inst.get("app_slug") or inst.get("app", {}).get("slug")
        if slug == app_slug:
            return inst
    return None


def get_org_id(api_base: str, org: str, token: str) -> int | None:
    try:
        r = request("GET", f"{api_base}/orgs/{org}", token)
        if r.status_code == 200:
            return r.json().get("id")
    except Exception:
        pass
    return None


def manual_install_url(web_base: str, org: str, org_id: int | None = None) -> str:
    if org_id is not None:
        return f"{web_base}/apps/{APP_SLUG}/installations/new/permissions?target_id={org_id}"
    return f"{web_base}/apps/{APP_SLUG}/installations/new"


def check_app_installed(api_base: str, web_base: str, org: str, token: str) -> tuple[bool, dict[str, Any]]:
    inst = find_app_installation(api_base, org, token, APP_SLUG)
    if inst:
        return True, {
            "status": "already_installed",
            "installation_id": inst.get("id"),
            "repository_selection": inst.get("repository_selection"),
        }
    org_id = get_org_id(api_base, org, token)
    return False, {"status": "missing", "install_url": manual_install_url(web_base, org, org_id)}


# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------

def validate_credentials(
    api_base: str,
    token: str,
    veracode_api_id: str | None,
    veracode_api_key: str | None,
    check_veracode: bool,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    print("\n[VALIDATION] Checking credentials...")

    try:
        r = request("GET", f"{api_base}/user", token)
        if r.status_code == 200:
            username = r.json().get("login", "unknown")
            print(f"  ✓ GitHub token valid (user: {username})")
            scopes = r.headers.get("X-OAuth-Scopes", "")
            print(f"  ✓ GitHub token scopes: {scopes}" if scopes else "  ⚠ Could not determine GitHub token scopes")
        elif r.status_code == 401:
            errors.append("GitHub token is invalid or expired")
            print("  ✗ GitHub token authentication failed")
        elif r.status_code == 403:
            errors.append("GitHub token lacks required permissions")
            print("  ✗ GitHub token permission denied")
        else:
            errors.append(f"GitHub API returned unexpected status: {r.status_code}")
            print(f"  ✗ GitHub API error: {r.status_code}")
    except Exception as exc:
        errors.append(f"GitHub API connection failed: {str(exc)[:100]}")
        print(f"  ✗ GitHub API connection error: {str(exc)[:80]}")

    if check_veracode and veracode_api_id and veracode_api_key:
        try:
            r = veracode_request(
                "GET", "/srcclr/v3/workspaces", veracode_api_id, veracode_api_key,
                params={"size": 1, "page": 0},
            )
            if r.status_code == 200:
                print("  ✓ Veracode credentials valid")
            elif r.status_code == 401:
                errors.append("Veracode credentials are invalid")
                print("  ✗ Veracode authentication failed")
            elif r.status_code == 403:
                errors.append("Veracode credentials lack required permissions")
                print("  ✗ Veracode permission denied")
            else:
                errors.append(f"Veracode API returned unexpected status: {r.status_code}")
                print(f"  ✗ Veracode API error: {r.status_code}")
        except Exception as exc:
            errors.append(f"Veracode API connection failed: {str(exc)[:100]}")
            print(f"  ✗ Veracode API connection error: {str(exc)[:80]}")

    if errors:
        print(f"\n[VALIDATION] ✗ Failed with {len(errors)} error(s)")
        return False, errors
    print("[VALIDATION] ✓ All credentials validated successfully\n")
    return True, []


# ---------------------------------------------------------------------------
# Teams map loading
# ---------------------------------------------------------------------------

def load_teams_map(teams_file: str) -> dict[str, str]:
    """Raises OSError/csv.Error on failure; caller handles sys.exit."""
    teams_map: dict[str, str] = {}
    with open(teams_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            org_name = (row.get("org") or "").strip()
            teams_value = (row.get("teams") or "").strip().strip('"')
            if org_name:
                teams_map[org_name] = teams_value
    print(f"[teams-map] Loaded {len(teams_map)} org->teams mappings from {teams_file}")
    return teams_map


# ---------------------------------------------------------------------------
# Per-org processing
# ---------------------------------------------------------------------------

def process_org(
    org: str,
    org_idx: int,
    ctx: RunContext,
    cached_clone_dir: str | None = None,
    buf: OrgBuffer | None = None,
) -> None:
    # In sequential mode buf is None - create a flush_on_add buffer so behaviour
    # is identical to before (lines stream immediately via tprint).
    if buf is None:
        buf = OrgBuffer(org, org_idx, ctx.total_orgs, flush_on_add=True)

    progress_pct = (org_idx / ctx.total_orgs) * 100 if ctx.total_orgs else 100.0
    # Sequential mode: print the header line immediately as before.
    # Parallel mode: header is prepended by OrgBuffer.flush().
    if buf.flush_on_add:
        buf.add(f"\n[{org_idx}/{ctx.total_orgs} ({progress_pct:.1f}%)] Processing: {org}")

    now = datetime.now()
    entry: dict[str, Any] = {
        "org": org,
        "timestamp": now.isoformat(),
        "timestamp_readable": now.strftime("%Y-%m-%d %H:%M:%S %A"),
    }

    # teams_value resolved once from ctx so it is available throughout this function
    if ctx.do_set_teams:
        if ctx.teams_mode == "auto":
            teams_value: str | None = org
        elif ctx.teams_mode == "hybrid":
            teams_value = ctx.teams_map.get(org, "").strip() or org
        else:
            teams_value = ctx.teams_map.get(org, "").strip() or None
        if teams_value and ctx.team_prefix:
            teams_value = ctx.team_prefix + teams_value
    else:
        teams_value = None

    # --- Repo import ----------------------------------------------------------
    repo_confirmed_present = False
    try:
        repo_ok, repo_details = ensure_veracode_repo_imported(
            ctx.api_base, org, ctx.token,
            do_apply=ctx.do_apply_repo,
            onboarding_yml_content=ctx.onboarding_yml_content,
            auto_import=ctx.do_apply_repo,
            teams_value=teams_value,
            web_base=ctx.web_base,
            cached_clone_dir=cached_clone_dir,
        )
        repo_confirmed_present = repo_details.pop("_repo_confirmed_present", False)
        entry["veracode_repo"] = {"present": repo_ok, **repo_details}
        with ctx.stats_lock:
            if repo_ok:
                ctx.stats.repo_success += 1
            else:
                ctx.stats.repo_fail += 1
        if not repo_ok:
            with ctx.rows_lock:
                ctx.missing_repo_rows.append([org, INTEGRATION_REPO_NAME, repo_details.get("status", "missing")])
    except Exception as exc:
        entry["veracode_repo"] = {"present": None, "status": "error", "error": str(exc)}
        with ctx.rows_lock:
            ctx.missing_repo_rows.append([org, INTEGRATION_REPO_NAME, f"error:{exc}"])
        with ctx.stats_lock:
            ctx.stats.repo_fail += 1
        buf.add(f"  Repo error: {str(exc)[:80]}")

    # --- App install check ----------------------------------------------------
    try:
        app_ok, app_details = check_app_installed(ctx.api_base, ctx.web_base, org, ctx.token)
        entry["workflow_app"] = {"installed": app_ok, **app_details}
        with ctx.stats_lock:
            if app_ok:
                ctx.stats.app_installed += 1
            else:
                ctx.stats.app_missing += 1
        if not app_ok:
            with ctx.rows_lock:
                ctx.missing_app_rows.append([org, APP_SLUG, "missing"])
                ctx.manual_links_rows.append([org, app_details["install_url"], "manual_install_required"])
    except Exception as exc:
        entry["workflow_app"] = {"installed": None, "status": "error", "error": str(exc)}
        with ctx.rows_lock:
            ctx.missing_app_rows.append([org, APP_SLUG, f"error:{exc}"])
        with ctx.stats_lock:
            ctx.stats.app_missing += 1
        buf.add(f"  App check error: {str(exc)[:80]}")

    # --- veracode.yml update --------------------------------------------------
    if ctx.do_update_yml and ctx.yml_content:
        try:
            yml_ok, yml_action = update_veracode_yml_in_repo(
                ctx.api_base, org, INTEGRATION_REPO_NAME, ctx.token, ctx.yml_content,
                repo_is_known_present=repo_confirmed_present,
            )
            entry["veracode_yml_update"] = {"success": yml_ok, "action": yml_action}
            with ctx.stats_lock:
                if yml_ok:
                    ctx.stats.yml_updated += 1
                elif yml_action in ("repo_not_found", "repo_empty"):
                    ctx.stats.yml_skipped += 1
                else:
                    ctx.stats.yml_failed += 1
        except Exception as exc:
            entry["veracode_yml_update"] = {"success": False, "action": f"error:{exc}"}
            with ctx.stats_lock:
                ctx.stats.yml_failed += 1
            buf.add(f"  veracode.yml update error: {str(exc)[:80]}")

    # --- Secrets --------------------------------------------------------------
    if ctx.dry_run or ctx.do_set_secrets:
        try:
            if ctx.dry_run:
                results = check_veracode_secrets_status(ctx.api_base, org, ctx.token)
                counts = {
                    v: sum(1 for x in results.values() if x == v)
                    for v in ("no_permission", "missing", "exists", "error")
                }
                with ctx.stats_lock:
                    ctx.stats.secrets_checked += 1
                    if counts["no_permission"] == 3:
                        status = "no_permission"
                        ctx.stats.secrets_no_permission += 1
                    elif counts["error"] == 3:
                        status = "error"
                        ctx.stats.secrets_fail += 1
                    elif counts["missing"] == 0 and counts["no_permission"] == 0 and counts["error"] == 0:
                        status = "all_exist"
                        ctx.stats.secrets_all_exist += 1
                    elif counts["exists"] == 0 and counts["no_permission"] == 0 and counts["error"] == 0:
                        status = "all_missing"
                        ctx.stats.secrets_all_missing += 1
                    else:
                        status = "partial"
                        ctx.stats.secrets_partial += 1
                entry["secrets"] = {"status": status, "results": results}

            elif ctx.do_set_secrets:
                workspace_id = create_veracode_workspace(org, ctx.veracode_api_id, ctx.veracode_api_key)
                if not workspace_id:
                    entry["secrets"] = {"status": "error", "error": "Failed to create or find Veracode workspace"}
                    with ctx.stats_lock:
                        ctx.stats.secrets_fail += 1
                else:
                    agent_token = create_veracode_agent_token(workspace_id, org, ctx.veracode_api_id, ctx.veracode_api_key)
                    if not agent_token:
                        entry["secrets"] = {"status": "error", "error": "Failed to generate agent token"}
                        with ctx.stats_lock:
                            ctx.stats.secrets_fail += 1
                    else:
                        ok, set_results = set_veracode_secrets(
                            ctx.api_base, org, ctx.token,
                            ctx.veracode_sa_api_id, ctx.veracode_sa_api_key, agent_token,
                        )
                        entry["secrets"] = {"status": "set" if ok else "partial", "results": set_results}
                        with ctx.stats_lock:
                            if ok:
                                ctx.stats.secrets_success += 1
                            else:
                                ctx.stats.secrets_fail += 1
        except Exception as exc:
            entry["secrets"] = {"status": "error", "error": str(exc)}
            with ctx.stats_lock:
                ctx.stats.secrets_fail += 1
                if ctx.dry_run:
                    ctx.stats.secrets_checked += 1
            buf.add(f"  Secrets error: {str(exc)[:80]}")

    # --- Write report + checkpoint (checkpoint saved AFTER all work completes) ---
    with ctx.report_lock:
        append_report_entry(ctx.report_path, entry)

    # processed reflects orgs that have fully completed all work.
    with ctx.stats_lock:
        ctx.stats.processed += 1

    # use len(ctx.completed_orgs) inside checkpoint_lock for the completion count —
    # avoids reading ctx.stats.processed across two different locks.
    with ctx.checkpoint_lock:
        ctx.completed_orgs.append(org)
        try:
            ctx.checkpoint_file.write_text(
                json.dumps(
                    {
                        "last_org": org,
                        "processed": len(ctx.completed_orgs),
                        "completed": ctx.completed_orgs,
                    },
                    indent=2,
                ),
                encoding="utf-8",
                newline="\n",
            )
        except Exception as exc:
            buf.add(f"  [WARNING] Failed to save checkpoint: {exc}")

    # --- Console summary line -------------------------------------------------
    repo_status = "✓" if entry.get("veracode_repo", {}).get("present") else "✗"
    app_status = "✓" if entry.get("workflow_app", {}).get("installed") else "✗"

    teams_detail = ""
    if ctx.do_set_teams:
        injection = entry.get("veracode_repo", {}).get("teams_injection")
        if teams_value:
            teams_detail = f" ({injection})" if injection else " (teams_injection_error)"
        else:
            teams_detail = " (no teams configured)"

    yml_status = ""
    if ctx.do_update_yml:
        yml_info = entry.get("veracode_yml_update", {})
        yml_status = f"  YML: {'✓' if yml_info.get('success') else '✗'} ({yml_info.get('action', 'error')})"

    secrets_status = ""
    if "secrets" in entry:
        s = entry["secrets"]
        sec_status = s.get("status", "")
        if sec_status == "no_permission":
            secrets_status = "  Secrets: ⚠ (no_permission - token needs admin:org scope)"
        elif sec_status == "all_exist":
            secrets_status = "  Secrets: ✓ (all exist)"
        elif sec_status == "all_missing":
            secrets_status = "  Secrets: ✗ (all missing)"
        elif sec_status == "partial":
            r_map = s.get("results", {})
            ec = sum(1 for v in r_map.values() if v == "exists")
            mc = sum(1 for v in r_map.values() if v == "missing")
            secrets_status = f"  Secrets: ⚠ ({ec} exist, {mc} missing)"
        elif sec_status == "set":
            secrets_status = "  Secrets: ✓"
        elif sec_status == "error":
            secrets_status = "  Secrets: ✗ (error)"
        else:
            secrets_status = "  Secrets: ✗"

    buf.add(f"  Repo: {repo_status}{teams_detail}  App: {app_status}{yml_status}{secrets_status}")
    buf.flush()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Veracode GitHub Workflow Integration rollout helper")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report status only, no changes (default).")
    mode.add_argument("--apply", action="store_true", help="Apply changes (requires action flags below).")

    ap.add_argument("--import-repo", action="store_true",
                    help="[apply] Create and import the 'veracode' repo if missing.")

    teams_group = ap.add_mutually_exclusive_group()
    teams_group.add_argument("--set-teams-auto", action="store_true",
                             help="[apply] Inject teams parameter using the org name.")
    teams_group.add_argument("--set-teams-file", metavar="FILE",
                             help="[apply] CSV file (org,teams) for per-org team injection.")
    teams_group.add_argument("--set-teams-hybrid", metavar="FILE",
                             help="[apply] CSV file (org,teams); orgs with blank teams fall back to org name.")

    ap.add_argument("--team-prefix", default="", metavar="PREFIX",
                    help="[apply] Prepend PREFIX to every injected teams value. "
                         "Applied after --set-teams-auto/file/hybrid resolution. "
                         "Example: --team-prefix 'gh-' turns 'acme-dev' into 'gh-acme-dev'.")

    ap.add_argument("--set-secrets", action="store_true",
                    help="[apply] Set VERACODE_API_ID, VERACODE_API_KEY, VERACODE_AGENT_TOKEN. "
                         "Always overwrites - safe to re-run for credential rotation.")

    ap.add_argument(
        "--update-veracode-yml", metavar="FILE", nargs="?", const="",
        help=(
            "[apply] Push a veracode.yml to the 'veracode' repo in every org, overwriting the "
            "current file. Omit FILE to fetch from the upstream integration repo; pass a local "
            "FILE path to use a custom file instead. The current file is backed up as "
            "default-veracode.yml before overwriting. Orgs with a missing or not-yet-imported "
            "repo are skipped."
        ),
    )

    ap.add_argument("--enterprise", help="GitHub Enterprise slug.")
    ap.add_argument("--orgs-file", help="Path to a file with one org login per line.")
    ap.add_argument("--out", default="out", help="Output directory (default: ./out).")
    ap.add_argument("--api-base", default=env("GITHUB_API_BASE", "https://api.github.com"),
                    help="GitHub API base URL.")
    ap.add_argument("--web-base", default=env("GITHUB_WEB_BASE", "https://github.com"),
                    help="GitHub web base URL (used for manual install links).")
    ap.add_argument("--token-env", default="GITHUB_TOKEN",
                    help="Environment variable holding the GitHub PAT (default: GITHUB_TOKEN).")
    ap.add_argument("--skip-to", help="Skip all orgs before this one and start from here.")
    ap.add_argument("--continue", dest="resume", action="store_true",
                    help="Resume from the last checkpoint saved in checkpoint.json.")
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="Number of parallel worker threads (default: 1). Recommended: 3-5. "
                         "Higher values increase throughput but consume GitHub API rate limit faster. "
                         "Values above 10 are not recommended.")

    args = ap.parse_args()

    if args.workers < 1:
        print("ERROR: --workers must be at least 1.", file=sys.stderr)
        sys.exit(1)
    if args.workers > 10:
        print(f"[WARNING] --workers {args.workers} is high. GitHub API rate limit is 5,000 req/hour. "
              "Consider 3-5 workers to avoid exhaustion.")

    if not args.dry_run and not args.apply:
        args.dry_run = True

    if args.apply and args.set_secrets:
        try:
            import nacl  # noqa: F401
        except ImportError:
            print("ERROR: --set-secrets requires pynacl.  Install with: pip install pynacl", file=sys.stderr)
            sys.exit(1)

    token = env(args.token_env)
    if not token:
        print(f"ERROR: Set {args.token_env} environment variable.", file=sys.stderr)
        sys.exit(1)

    api_base = args.api_base.rstrip("/")
    web_base = args.web_base.rstrip("/")

    do_apply_repo = bool(args.apply and args.import_repo)
    do_set_secrets = bool(args.apply and args.set_secrets)
    do_set_teams = bool(args.apply and (args.set_teams_auto or args.set_teams_file or args.set_teams_hybrid))
    do_update_yml = bool(args.apply and args.update_veracode_yml is not None)

    # Derive teams_mode string once here so workers receive a clean Literal value.
    if args.set_teams_auto:
        teams_mode = "auto"
    elif args.set_teams_hybrid:
        teams_mode = "hybrid"
    elif args.set_teams_file:
        teams_mode = "file"
    else:
        teams_mode = "none"

    # Read the onboarding template once at startup; pass through RunContext.
    # Pre-clone runs for both sequential and parallel paths when do_apply_repo is True.
    onboarding_yml_content: str | None = None
    onboarding_yml_path: Path | None = None
    if do_apply_repo:
        onboarding_yml_path = Path(__file__).parent / "veracode.yml"
        if onboarding_yml_path.exists():
            onboarding_yml_content = onboarding_yml_path.read_text(encoding="utf-8")
            print(f"[import-repo] Onboarding veracode.yml: {onboarding_yml_path.resolve()}")
        else:
            print("[WARNING] veracode.yml not found next to script - repo will be imported but yml injection will be skipped.")
            print(f"          Expected: {onboarding_yml_path.resolve()}", file=sys.stderr)
            onboarding_yml_path = None

    yml_content: str | None = None
    yml_source_label: str | None = None
    if do_update_yml:
        raw_path = args.update_veracode_yml
        if raw_path:
            local_path = Path(raw_path)
            if not local_path.exists():
                print(f"ERROR: --update-veracode-yml file not found: {local_path}", file=sys.stderr)
                sys.exit(1)
            yml_content = local_path.read_text(encoding="utf-8")
            yml_source_label = str(local_path.resolve())
        else:
            print("[update-veracode-yml] Fetching veracode.yml from upstream integration repo...")
            yml_content = fetch_upstream_veracode_yml()
            if not yml_content:
                print("ERROR: Could not fetch veracode.yml from upstream repo. "
                      "Pass a local file with --update-veracode-yml FILE.", file=sys.stderr)
                sys.exit(1)
            yml_source_label = INTEGRATION_SOURCE_URL
        print(f"[update-veracode-yml] Source: {yml_source_label}")

    veracode_api_id = env("VERACODE_API_ID") if do_set_secrets else None
    veracode_api_key = env("VERACODE_API_KEY") if do_set_secrets else None
    veracode_sa_api_id = env("VERACODE_SA_API_ID") if do_set_secrets else None
    veracode_sa_api_key = env("VERACODE_SA_API_KEY") if do_set_secrets else None

    if do_set_secrets and (not veracode_api_id or not veracode_api_key):
        print("ERROR: --set-secrets requires VERACODE_API_ID and VERACODE_API_KEY env vars.", file=sys.stderr)
        sys.exit(1)
    if do_set_secrets and (not veracode_sa_api_id or not veracode_sa_api_key):
        print("ERROR: --set-secrets requires VERACODE_SA_API_ID and VERACODE_SA_API_KEY env vars.", file=sys.stderr)
        sys.exit(1)

    teams_map: dict[str, str] = {}
    teams_file = args.set_teams_file or args.set_teams_hybrid
    if teams_file:
        try:
            teams_map = load_teams_map(teams_file)
        except Exception as exc:
            print(f"[ERROR] Failed to load teams file '{teams_file}': {exc}", file=sys.stderr)
            sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"MODE: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"{'=' * 60}")
    if args.apply:
        print(f"  Import missing repos  : {'YES' if do_apply_repo else 'NO (--import-repo)'}")
        if do_apply_repo:
            yml_note = str(onboarding_yml_path.resolve()) if onboarding_yml_path else "NOT FOUND - import only, yml injection skipped"
            print(f"    Onboarding YML      : {yml_note}")
        if do_set_teams:
            if args.set_teams_auto:
                print("  Set teams in workflows: YES (auto - org name)")
            elif args.set_teams_hybrid:
                print(f"  Set teams in workflows: YES (hybrid - from {args.set_teams_hybrid}, org name fallback)")
            else:
                print(f"  Set teams in workflows: YES (from {args.set_teams_file})")
            if args.team_prefix:
                print(f"    Team prefix         : '{args.team_prefix}'")
        else:
            print("  Set teams in workflows: NO (--set-teams-auto or --set-teams-file or --set-teams-hybrid)")
        if do_update_yml:
            print(f"  Update veracode.yml   : YES (source: {yml_source_label})")
        print(f"  Set Veracode secrets  : {'YES' if do_set_secrets else 'NO (--set-secrets)'}")
        if do_set_secrets:
            print(f"    VERACODE_API_ID     : {'SET' if veracode_api_id else 'NOT SET'}  (admin - for API calls)")
            print(f"    VERACODE_API_KEY    : {'SET' if veracode_api_key else 'NOT SET'}  (admin - for API calls)")
            print(f"    VERACODE_SA_API_ID  : {'SET' if veracode_sa_api_id else 'NOT SET'}  (service account - stored in orgs)")
            print(f"    VERACODE_SA_API_KEY : {'SET' if veracode_sa_api_key else 'NOT SET'}  (service account - stored in orgs)")
    else:
        print("  No changes will be made (use --apply to enable changes)")
    print(f"  Workers               : {args.workers}{' (parallel)' if args.workers > 1 else ' (sequential)'}")
    print(f"{'=' * 60}\n")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    all_orgs = list_orgs(api_base, token, args.enterprise, args.orgs_file)

    # When --enterprise and --orgs-file are both provided, filter the enterprise
    # org list down to only the orgs named in the file.
    if args.orgs_file and args.enterprise:
        try:
            with open(args.orgs_file, encoding="utf-8") as f:
                filter_orgs = {ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")}
            filtered = [o for o in all_orgs if o in filter_orgs]
            print(f"[OK] Filtered to {len(filtered)} orgs from {args.orgs_file}")
            orgs = filtered
        except Exception as exc:
            print(f"[ERROR] Could not apply orgs-file filter: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        orgs = all_orgs

    orgs_txt_path = outdir / "orgs.txt"
    teams_map_csv_path = outdir / "teams_map.csv"

    if args.dry_run or not orgs_txt_path.exists():
        write_orgs_txt(orgs_txt_path, orgs)
    if not teams_map_csv_path.exists():
        write_teams_map_csv(teams_map_csv_path, orgs)

    validation_ok, validation_errors = validate_credentials(
        api_base=api_base,
        token=token,
        veracode_api_id=veracode_api_id,
        veracode_api_key=veracode_api_key,
        check_veracode=do_set_secrets,
    )
    if not validation_ok:
        print("\n[ERROR] Credential validation failed:", file=sys.stderr)
        for error in validation_errors:
            print(f"  - {error}", file=sys.stderr)
        print("\nPlease fix the credential issues and try again.", file=sys.stderr)
        sys.exit(1)

    checkpoint_file = outdir / "checkpoint.json"
    start_index = 0

    if args.resume and checkpoint_file.exists():
        try:
            checkpoint_data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            completed_set = set(checkpoint_data.get("completed", []))
            last_org = checkpoint_data.get("last_org")

            if completed_set:
                before = len(orgs)
                orgs = [o for o in orgs if o not in completed_set]
                skipped = before - len(orgs)
                print(f"[RESUME] Skipping {skipped} already-completed orgs (parallel checkpoint)\n")
            elif last_org and last_org in orgs:
                start_index = orgs.index(last_org)
                print(f"[RESUME] Restarting from: {last_org}  (skipping {start_index} orgs)\n")
        except Exception as exc:
            print(f"[WARNING] Failed to load checkpoint: {exc}")

    if args.skip_to:
        if args.skip_to in orgs:
            start_index = orgs.index(args.skip_to)
            print(f"[SKIP] Starting from: {args.skip_to}  (skipping {start_index} orgs)\n")
        else:
            print(f"[WARNING] --skip-to org '{args.skip_to}' not found in org list")

    if start_index > 0:
        orgs = orgs[start_index:]
        print(f"Processing {len(orgs)} remaining organizations\n")

    total_orgs = len(orgs)

    if args.apply and not args.resume:
        print(f"\n{'=' * 60}")
        print("   CONFIRMATION REQUIRED")
        print(f"{'=' * 60}")
        print(f"About to modify {total_orgs} organizations in APPLY mode.")
        print("Actions enabled:")
        if do_apply_repo:
            print("  - Create and import veracode repos")
        if do_set_teams:
            print("  - Inject teams parameters into workflows")
        if do_update_yml:
            print(f"  - Push veracode.yml from {yml_source_label}")
        if do_set_secrets:
            print("  - Set/overwrite Veracode org secrets")
        print("\nType 'yes' to continue (anything else will cancel): ", end="")
        if input().strip().lower() != "yes":
            print("\n[CANCELLED] Operation cancelled by user.")
            sys.exit(0)
        print(f"{'=' * 60}\n")

    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = outdir / f"audit_report_{run_timestamp}.json"

    ctx = RunContext(
        api_base=api_base,
        web_base=web_base,
        token=token,
        do_apply_repo=do_apply_repo,
        do_set_secrets=do_set_secrets,
        do_set_teams=do_set_teams,
        do_update_yml=do_update_yml,
        dry_run=args.dry_run,
        teams_mode=teams_mode,
        yml_content=yml_content,
        onboarding_yml_content=onboarding_yml_content,
        teams_map=teams_map,
        team_prefix=args.team_prefix,
        veracode_api_id=veracode_api_id,
        veracode_api_key=veracode_api_key,
        veracode_sa_api_id=veracode_sa_api_id,
        veracode_sa_api_key=veracode_sa_api_key,
        total_orgs=total_orgs,
        report_path=report_path,
        checkpoint_file=checkpoint_file,
        stats=RunStats(total_orgs=total_orgs),
    )

    workers = args.workers

    # Pre-clone the integration repo once; workers copy it rather than each cloning independently.
    _cached_clone_dir: str | None = None
    if do_apply_repo and check_git_available():
        label = "[PARALLEL]" if workers > 1 else "[import-repo]"
        print(f"{label} Pre-cloning integration repo...")
        clone_ok, clone_msg, _cached_clone_dir = git_clone_bare(INTEGRATION_SOURCE_URL)
        if clone_ok:
            print(f"{label} Pre-clone successful\n")
        else:
            print(f"[WARNING] Pre-clone failed ({clone_msg}) - will clone per org\n")
            _cached_clone_dir = None

    try:
        if workers > 1:
            print(f"[PARALLEL] Running with {workers} workers\n")
            progress = ProgressDisplay(workers)

            _ticker_stop = threading.Event()
            _active_slots: dict[int, tuple[str, float]] = {}
            _active_lock = threading.Lock()

            def _ticker() -> None:
                while not _ticker_stop.is_set():
                    time.sleep(2)
                    with _active_lock:
                        snapshot = list(_active_slots.items())
                    for sid, (o, t0) in snapshot:
                        progress.update(sid, o, time.time() - t0)

            ticker_thread = threading.Thread(target=_ticker, daemon=True)
            ticker_thread.start()

            try:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    # Submit all futures upfront. The executor queues them and runs
                    # at most max_workers concurrently. Slot IDs are assigned via
                    # modulo so each display line is reused across orgs without a pool.
                    all_futures: dict[Any, tuple[str, int, OrgBuffer]] = {}
                    for idx, org in enumerate(orgs, 1):
                        slot_id = (idx - 1) % workers
                        org_buf = OrgBuffer(org, idx, total_orgs, flush_on_add=False)
                        with _active_lock:
                            _active_slots[slot_id] = (org, time.time())
                        progress.update(slot_id, org, 0)
                        f = executor.submit(process_org, org, idx, ctx, _cached_clone_dir, org_buf)
                        all_futures[f] = (org, slot_id, org_buf)

                    for future in as_completed(all_futures):
                        org_name, slot_id, _ = all_futures[future]
                        with _active_lock:
                            _active_slots.pop(slot_id, None)
                        progress.clear_slot(slot_id)
                        try:
                            future.result()
                        except Exception as exc:
                            tprint(f"[ERROR] Unhandled exception for org {org_name}: {exc}")
            finally:
                _ticker_stop.set()
                ticker_thread.join(timeout=3)
                progress.stop()
        else:
            for idx, org in enumerate(orgs, 1):
                process_org(org, idx, ctx, _cached_clone_dir)
    finally:
        if _cached_clone_dir and os.path.exists(_cached_clone_dir):
            shutil.rmtree(_cached_clone_dir, ignore_errors=True)

    # Convert JSONL -> pretty JSON array
    finalize_report(report_path)

    write_csv(outdir / "missing_veracode_repo.csv", ["organization", "repo_name", "note"], ctx.missing_repo_rows)
    write_csv(outdir / "missing_workflow_app.csv", ["organization", "app_slug", "note"], ctx.missing_app_rows)
    write_csv(outdir / "manual_install_links.csv", ["organization", "install_link", "reason"], ctx.manual_links_rows)

    st = ctx.stats
    st.end_time = datetime.now()
    duration_str = str(st.end_time - st.start_time).split(".")[0]

    print(f"\n{'=' * 70}")
    print("EXECUTION SUMMARY")
    print(f"{'=' * 70}")
    print(f"Mode            : {'APPLY' if args.apply else 'DRY-RUN'}")
    if workers > 1:
        print(f"Workers         : {workers}")
    print(f"Start Time      : {st.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"End Time        : {st.end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Duration        : {duration_str}")
    print()
    print(f"Organizations   : {st.processed}/{st.total_orgs} processed")
    print()

    repo_total = st.repo_success + st.repo_fail
    if repo_total > 0:
        repo_pct = (st.repo_success / repo_total) * 100
        print(f"Veracode Repos  : {st.repo_success} success, {st.repo_fail} failed ({repo_pct:.1f}% success)")

    app_total = st.app_installed + st.app_missing
    if app_total > 0:
        print(f"Workflow App    : {st.app_installed} installed, {st.app_missing} missing (see manual_install_links.csv)")

    if do_update_yml:
        yml_total = st.yml_updated + st.yml_skipped + st.yml_failed
        print(f"veracode.yml    : {st.yml_updated} updated, {st.yml_skipped} skipped, {st.yml_failed} failed (of {yml_total} orgs)")

    if args.dry_run and st.secrets_checked > 0:
        suffix = "" if st.secrets_no_permission == 0 else " - add admin:org scope to check secrets"
        print(
            f"Secrets (check) : {st.secrets_all_exist} all exist, {st.secrets_partial} partial, "
            f"{st.secrets_all_missing} all missing, {st.secrets_no_permission} no_permission "
            f"(of {st.secrets_checked} orgs checked){suffix}"
        )
    elif st.secrets_success > 0 or st.secrets_fail > 0:
        secrets_total = st.secrets_success + st.secrets_fail
        secrets_pct = (st.secrets_success / secrets_total) * 100
        print(f"Secrets         : {st.secrets_success} success, {st.secrets_fail} failed ({secrets_pct:.1f}% success)")

    print(f"{'=' * 70}")
    print("\nOutputs written to:", outdir.resolve())
    print(" - orgs.txt")
    print(" - teams_map.csv")
    print(f" - audit_report_{run_timestamp}.json (this run)")
    print(" - missing_veracode_repo.csv")
    print(" - missing_workflow_app.csv")
    print(" - manual_install_links.csv")

    if ctx.missing_repo_rows or ctx.missing_app_rows:
        print(f"\n  Note: {len(ctx.missing_repo_rows)} org(s) have missing repos, "
              f"{len(ctx.missing_app_rows)} org(s) need app installation")
        print("    See CSV files above for details and actions needed.")

    sys.exit(0)


if __name__ == "__main__":
    main()
