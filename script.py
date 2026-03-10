from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import tempfile
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests

APP_SLUG = "veracode-workflow-app"
INTEGRATION_REPO_NAME = "veracode"
INTEGRATION_SOURCE_URL = "https://github.com/veracode/github-actions-integration.git"
API_VER = "2022-11-28"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VER,
        "User-Agent": "veracode-workflow-rollout-helper",
    }


def check_rate_limit(response: requests.Response) -> None:
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset_time = response.headers.get("X-RateLimit-Reset")

    if remaining and reset_time:
        remaining = int(remaining)
        reset_time = int(reset_time)

        if remaining < 100:
            reset_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset_time))
            print(f"  [WARNING] Rate limit low: {remaining} requests remaining (resets at {reset_dt})")

        if remaining < 10:
            wait_seconds = max(reset_time - int(time.time()), 0) + 5
            print(f"  [RATE LIMIT] Pausing {wait_seconds}s until rate limit resets...")
            time.sleep(wait_seconds)


def request(method: str, url: str, token: str, max_retries: int = 3, **kwargs) -> requests.Response:
    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, headers=headers(token), timeout=45, **kwargs)
            check_rate_limit(r)

            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                if attempt < max_retries - 1:
                    print(f"  [RATE LIMIT] 429 received, waiting {retry_after}s (retry {attempt + 1}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                return r

            if r.status_code >= 500:
                if attempt < max_retries - 1:
                    wait_seconds = (2 ** attempt) * 2
                    print(f"  [SERVER ERROR] {r.status_code}, waiting {wait_seconds}s (retry {attempt + 1}/{max_retries})...")
                    time.sleep(wait_seconds)
                    continue
                return r

            return r

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait_seconds = (2 ** attempt) * 2
                print(f"  [TIMEOUT] waiting {wait_seconds}s (retry {attempt + 1}/{max_retries})...")
                time.sleep(wait_seconds)
                continue
            raise

        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                wait_seconds = (2 ** attempt) * 2
                print(f"  [NETWORK ERROR] {str(exc)[:50]}, waiting {wait_seconds}s (retry {attempt + 1}/{max_retries})...")
                time.sleep(wait_seconds)
                continue
            raise

    raise RuntimeError(f"Request failed after {max_retries} retries: {method} {url}")


def veracode_request(
    method: str,
    endpoint: str,
    api_id: str,
    api_key: str,
    max_retries: int = 3,
    **kwargs,
) -> requests.Response:
    from veracode_api_signing.plugin_requests import RequestsAuthPluginVeracodeHMAC

    url = f"https://api.veracode.com{endpoint}"
    auth = RequestsAuthPluginVeracodeHMAC(api_key_id=api_id, api_key_secret=api_key)

    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, auth=auth, timeout=45, **kwargs)

            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                if attempt < max_retries - 1:
                    print(f"  [VERACODE RATE LIMIT] 429, waiting {retry_after}s (retry {attempt + 1}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                return r

            if r.status_code >= 500:
                if attempt < max_retries - 1:
                    wait_seconds = (2 ** attempt) * 2
                    print(f"  [VERACODE SERVER ERROR] {r.status_code}, waiting {wait_seconds}s (retry {attempt + 1}/{max_retries})...")
                    time.sleep(wait_seconds)
                    continue
                return r

            return r

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait_seconds = (2 ** attempt) * 2
                print(f"  [VERACODE TIMEOUT] waiting {wait_seconds}s (retry {attempt + 1}/{max_retries})...")
                time.sleep(wait_seconds)
                continue
            raise

        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                wait_seconds = (2 ** attempt) * 2
                print(f"  [VERACODE NETWORK ERROR] {str(exc)[:50]}, waiting {wait_seconds}s (retry {attempt + 1}/{max_retries})...")
                time.sleep(wait_seconds)
                continue
            raise

    raise RuntimeError(f"Veracode request failed after {max_retries} retries: {method} {endpoint}")


def parse_link_next(link_header: str) -> Optional[str]:
    for part in [p.strip() for p in link_header.split(",")]:
        if 'rel="next"' in part:
            left = part.split(";")[0].strip()
            if left.startswith("<") and left.endswith(">"):
                return left[1:-1]
    return None


def paginate_list(url: str, token: str, params: Optional[dict] = None) -> List[dict]:
    out: List[dict] = []
    while url:
        r = request("GET", url, token, params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {url} failed: {r.status_code} {r.text}")
        data = r.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Expected list from {url}, got {type(data)}")
        out.extend(data)
        link = r.headers.get("Link") or r.headers.get("link")
        url = parse_link_next(link) if link else None
        params = None
    return out


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_csv(path: Path, header: List[str], rows: List[List[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(header)
        writer.writerows(rows)


def write_report_entry(report_path: Path, entry: Dict[str, Any]) -> None:
    if not report_path.exists():
        report_path.write_text("[\n" + json.dumps(entry, indent=2) + "\n]\n", encoding="utf-8")
        return
    with report_path.open("r+b") as f:
        f.seek(0, 2)
        size = f.tell()
        if size >= 2:
            f.seek(size - 2)
            if f.read(2) == b"]\n":
                f.seek(size - 2)
                f.truncate()
                f.write((",\n" + json.dumps(entry, indent=2) + "\n]\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Git / repo import helpers
# ---------------------------------------------------------------------------

def check_git_available() -> bool:
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def git_mirror_import(
    source_url: str,
    target_org: str,
    target_repo: str,
    token: str,
) -> Tuple[bool, str]:
    temp_dir: Optional[str] = None

    try:
        temp_dir = tempfile.mkdtemp(prefix="veracode-import-")
        bare_repo = os.path.join(temp_dir, "repo.git")

        clone_result = subprocess.run(
            ["git", "clone", "--bare", source_url, bare_repo],
            capture_output=True, text=True, timeout=300,
        )
        if clone_result.returncode != 0:
            return False, f"Clone failed: {clone_result.stderr}"

        target_url = f"https://{token}@github.com/{target_org}/{target_repo}.git"

        push_result = subprocess.run(
            ["git", "-C", bare_repo, "push", "--mirror", target_url],
            capture_output=True, text=True, timeout=300,
        )
        if push_result.returncode != 0:
            return False, f"Push failed: {push_result.stderr}"

        return True, "Import successful"

    except subprocess.TimeoutExpired:
        return False, "Timeout during git operation"
    except Exception as exc:
        return False, str(exc)
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Veracode SCA workspace and agent helpers
# ---------------------------------------------------------------------------

def _find_workspace_by_name(org_name: str, api_id: str, api_key: str) -> Optional[str]:
    """Walk all pages of GET /v3/workspaces using filter[workspace] and return the workspace UUID."""
    page = 0
    while True:
        r = veracode_request(
            "GET", "/srcclr/v3/workspaces",
            api_id, api_key,
            params={"filter[workspace]": org_name, "size": 100, "page": page},
        )
        if r.status_code == 401:
            print("  [ERROR] Veracode authentication failed - check credentials")
            return None
        if r.status_code == 403:
            print("  [ERROR] Veracode permission denied - insufficient access")
            return None
        if r.status_code != 200:
            print(f"  [ERROR] Failed to list workspaces: {r.status_code} - {r.text[:200]}")
            return None

        body = r.json()
        for ws in body.get("_embedded", {}).get("workspaces", []):
            if ws.get("name") == org_name:
                return ws.get("id")

        page_meta = body.get("page", {})
        total_pages = page_meta.get("total_pages", 1)
        if page >= total_pages - 1:
            break
        page += 1

    return None


def create_veracode_workspace(org_name: str, api_id: str, api_key: str) -> Optional[str]:
    """Return the workspace UUID for org_name, creating it if it does not exist.

    POST /v3/workspaces returns a ResponseEntity envelope with no workspace ID in the body.
    The UUID is resolved via a follow-up GET with filter[workspace]=name.
    """
    try:
        existing_id = _find_workspace_by_name(org_name, api_id, api_key)
        if existing_id:
            return existing_id

        r = veracode_request("POST", "/srcclr/v3/workspaces", api_id, api_key, json={"name": org_name})
        if r.status_code not in (200, 201):
            print(f"  [ERROR] Failed to create workspace: {r.status_code} - {r.text[:200]}")
            return None

        time.sleep(1)
        workspace_id = _find_workspace_by_name(org_name, api_id, api_key)
        if not workspace_id:
            print(f"  [ERROR] Workspace created but not found on follow-up lookup for: {org_name}")
            return None

        return workspace_id

    except Exception as exc:
        print(f"  [ERROR] create_veracode_workspace: {exc}")
        return None


def list_veracode_agents(workspace_id: str, api_id: str, api_key: str) -> Optional[List[dict]]:
    """Return the list of AgentSummary objects for a workspace, or None on failure.

    GET /v3/workspaces/{id}/agents returns Resources«AgentSummary»:
    { _embedded: [ {id, name, ...} ] } - flat list, no pagination.
    """
    try:
        r = veracode_request("GET", f"/srcclr/v3/workspaces/{workspace_id}/agents", api_id, api_key)
        if r.status_code == 200:
            return r.json().get("_embedded", {}).get("agents", [])
        return None
    except Exception:
        return None


def delete_veracode_agent(workspace_id: str, agent_id: str, api_id: str, api_key: str) -> bool:
    """Delete an agent from a workspace. Returns True on success."""
    try:
        r = veracode_request(
            "DELETE",
            f"/srcclr/v3/workspaces/{workspace_id}/agents/{agent_id}",
            api_id, api_key,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


def create_veracode_agent_token(
    workspace_id: str,
    org_name: str,
    api_id: str,
    api_key: str,
) -> Optional[str]:
    """Return an agent access_token for the workspace.

    If an agent with the expected name already exists, regenerate its token via
    token:regenerate (invalidates old tokens, returns fresh access_token).
    Otherwise create a new agent - POST /v3/workspaces/{id}/agents returns CreatedAgent
    which embeds token.access_token directly.
    """
    try:
        suffix = "-agt"
        max_org_len = 20 - len(suffix)
        truncated_org = org_name[:max_org_len]
        if not truncated_org[0].isalpha():
            truncated_org = "gh" + truncated_org[:max_org_len - 2]
        agent_name = f"{truncated_org}{suffix}"

        existing_agents = list_veracode_agents(workspace_id, api_id, api_key)
        if existing_agents:
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
                        print("  [ERROR] token:regenerate succeeded but no access_token in response")
                        return None
                    print(f"  [ERROR] token:regenerate failed: {regen.status_code} - {regen.text[:200]}")
                    return None

        r = veracode_request(
            "POST",
            f"/srcclr/v3/workspaces/{workspace_id}/agents",
            api_id, api_key,
            json={"name": agent_name, "agent_type": "CLI"},
        )
        if r.status_code != 200:
            print(f"  [ERROR] Failed to create agent: {r.status_code} - {r.text[:200]}")
            return None

        if not r.content:
            print("  [ERROR] Agent POST returned empty body")
            return None

        try:
            agent_body = r.json()
        except json.JSONDecodeError:
            print("  [ERROR] Failed to parse agent POST response")
            return None

        access_token = agent_body.get("token", {}).get("access_token")
        if access_token:
            return access_token

        print(f"  [ERROR] Agent created but no token.access_token in response: {agent_body}")
        return None

    except Exception as exc:
        print(f"  [ERROR] create_veracode_agent_token: {exc}")
        return None


# ---------------------------------------------------------------------------
# GitHub Actions secrets helpers
# ---------------------------------------------------------------------------

def get_org_public_key(api_base: str, org: str, token: str) -> Optional[Tuple[str, str]]:
    try:
        r = request("GET", f"{api_base}/orgs/{org}/actions/secrets/public-key", token)
        if r.status_code == 200:
            data = r.json()
            return data.get("key_id"), data.get("key")
        return None
    except Exception:
        return None


def encrypt_secret(public_key: str, secret_value: str) -> str:
    from base64 import b64encode
    from nacl import encoding, public

    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
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
            print(f"  [{org}] Warning: no permission to check secret {secret_name}")
            return False
        print(f"  [{org}] Unexpected response checking {secret_name}: {r.status_code}")
        return False
    except Exception as exc:
        print(f"  [{org}] Error checking secret {secret_name}: {exc}")
        return False


def set_org_secret(
    api_base: str,
    org: str,
    token: str,
    secret_name: str,
    secret_value: str,
) -> bool:
    try:
        key_info = get_org_public_key(api_base, org, token)
        if not key_info:
            return False

        key_id, public_key = key_info
        payload = {
            "encrypted_value": encrypt_secret(public_key, secret_value),
            "key_id": key_id,
            "visibility": "all",
        }
        r = request("PUT", f"{api_base}/orgs/{org}/actions/secrets/{secret_name}", token, json=payload)

        if r.status_code in (201, 204):
            return True
        print(f"    [ERROR] Secret {secret_name} PUT failed: {r.status_code}")
        return False

    except Exception as exc:
        print(f"    [ERROR] Exception setting secret {secret_name}: {exc}")
        return False


def set_veracode_secrets(
    api_base: str,
    org: str,
    github_token: str,
    veracode_sa_api_id: str,
    veracode_sa_api_key: str,
    veracode_agent_token: str,
) -> Tuple[bool, Dict[str, str]]:
    secrets_to_set = {
        "VERACODE_API_ID": veracode_sa_api_id,
        "VERACODE_API_KEY": veracode_sa_api_key,
        "VERACODE_AGENT_TOKEN": veracode_agent_token,
    }
    results: Dict[str, str] = {}

    for secret_name, secret_value in secrets_to_set.items():
        if secret_exists(api_base, org, github_token, secret_name):
            results[secret_name] = "exists"
        else:
            ok = set_org_secret(api_base, org, github_token, secret_name, secret_value)
            if ok:
                time.sleep(0.5)
                verified = secret_exists(api_base, org, github_token, secret_name)
                results[secret_name] = "set" if verified else "set_unverified"
            else:
                results[secret_name] = "failed"

    all_ok = all(v in ("set", "exists") for v in results.values())
    return all_ok, results


# ---------------------------------------------------------------------------
# GitHub org discovery
# ---------------------------------------------------------------------------

def list_orgs_graphql(api_base: str, token: str, enterprise: str) -> Optional[List[str]]:
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

        all_orgs: List[str] = []
        cursor: Optional[str] = None

        while True:
            variables: Dict[str, Any] = {"enterprise": enterprise}
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


def list_orgs(
    api_base: str,
    token: str,
    enterprise: Optional[str],
    orgs_file: Optional[str],
) -> List[str]:
    errors: List[str] = []

    if enterprise:
        print(f'Discovering orgs via enterprise GraphQL: enterprise(slug: "{enterprise}")')
        try:
            orgs = list_orgs_graphql(api_base, token, enterprise)
            if orgs:
                print(f"[OK] Found {len(orgs)} orgs via GraphQL")
                return orgs
            print(f"\n[ERROR] Enterprise GraphQL returned 0 organizations", file=sys.stderr)
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

    try:
        print("Discovering orgs via /user/orgs (all orgs the token user belongs to)")
        org_objs = paginate_list(f"{api_base}/user/orgs", token, params={"per_page": 100})
        orgs = [o["login"] for o in org_objs if "login" in o]
        if orgs:
            print(f"[OK] Found {len(orgs)} orgs via user API")
            return orgs
        errors.append("User API returned no orgs")
    except Exception as exc:
        errors.append(f"User API failed: {exc}")

    if orgs_file:
        try:
            print(f"Reading orgs from file: {orgs_file}")
            with open(orgs_file, encoding="utf-8") as f:
                orgs = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
            if orgs:
                print(f"[OK] Found {len(orgs)} orgs from file")
                return orgs
            errors.append(f"File '{orgs_file}' contains no valid org names")
        except Exception as exc:
            errors.append(f"File read failed: {exc}")

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
# Repository helpers
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


def get_import_status(api_base: str, org: str, repo: str, token: str) -> dict:
    r = request("GET", f"{api_base}/repos/{org}/{repo}/import", token)
    if r.status_code == 200:
        return r.json()
    raise RuntimeError(f"{org}/{repo}: import status failed {r.status_code} {r.text}")


def wait_for_import(
    api_base: str,
    org: str,
    repo: str,
    token: str,
    timeout_s: int = 900,
    poll_s: int = 5,
) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        last = get_import_status(api_base, org, repo, token)
        status = (last.get("status") or "").lower()
        if status in ("complete", "succeeded"):
            return last
        if status in ("failed", "error"):
            raise RuntimeError(f"{org}/{repo}: import failed: {last}")
        time.sleep(poll_s)
    raise RuntimeError(f"{org}/{repo}: import timed out; last={last}")


# ---------------------------------------------------------------------------
# Workflow file injection
# ---------------------------------------------------------------------------

def _inject_teams_regex(content: str, org: str) -> Tuple[str, bool]:
    pattern = re.compile(
        r"([ \t]*(?:-[ \t]+)?uses:[ \t]+veracode/(?:veracode-)?uploadandscan-action@[^\n]+\n"
        r"(?:[ \t]+[^\n]+\n)*?"
        r"[ \t]+with:\n)"
        r"((?:[ \t]+[^\n]+\n)+)",
        re.MULTILINE,
    )

    changed = False

    def replacer(m: re.Match) -> str:
        nonlocal changed
        header, body = m.group(1), m.group(2)
        if re.search(r"^\s+teams\s*:", body, re.MULTILINE):
            return m.group(0)
        first_param = body.splitlines()[0]
        indent = len(first_param) - len(first_param.lstrip())
        changed = True
        return header + " " * indent + f'teams: "{org}"\n' + body

    return pattern.sub(replacer, content), changed


def inject_teams_into_workflows(api_base: str, org: str, repo: str, token: str, teams_value: str) -> Tuple[bool, str]:
    from base64 import b64decode, b64encode

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
            print(f"  [{org}] Regex injection error for {workflow_path}: {exc}")
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
            print(f"  [{org}] Failed to update {workflow_path}: {r.status_code}")

    if modified_count > 0:
        return True, f"teams_added_to_{modified_count}_files"
    return True, "teams_already_present"


def inject_veracode_yml(api_base: str, org: str, repo: str, token: str) -> Tuple[bool, str]:
    from base64 import b64decode, b64encode

    template_path = Path(__file__).parent / "veracode.yml"
    if not template_path.exists():
        print(f"  [{org}] Warning: veracode.yml template not found, skipping injection")
        return False, "template_not_found"

    with open(template_path, encoding="utf-8") as f:
        custom_yml = f.read()

    veracode_url = f"{api_base}/repos/{org}/{repo}/contents/veracode.yml"
    default_veracode_url = f"{api_base}/repos/{org}/{repo}/contents/default-veracode.yml"

    r = request("GET", veracode_url, token)

    if r.status_code == 200:
        original_data = r.json()
        original_sha = original_data.get("sha")
        original_content_b64 = original_data.get("content", "")

        r_default = request("GET", default_veracode_url, token)
        backup_payload: Dict[str, Any] = {
            "message": "Preserve original Veracode template as default-veracode.yml",
            "content": original_content_b64,
            "branch": "main",
        }
        if r_default.status_code == 200:
            backup_payload["sha"] = r_default.json().get("sha")
        request("PUT", default_veracode_url, token, json=backup_payload)

        r = request("PUT", veracode_url, token, json={
            "message": "Update Veracode workflow configuration with custom settings",
            "content": b64encode(custom_yml.encode("utf-8")).decode("utf-8"),
            "branch": "main",
            "sha": original_sha,
        })
        return (True, "updated_with_backup") if r.status_code in (200, 201) else (False, "failed")

    else:
        r = request("PUT", veracode_url, token, json={
            "message": "Add Veracode workflow configuration",
            "content": b64encode(custom_yml.encode("utf-8")).decode("utf-8"),
            "branch": "main",
        })
        return (True, "created") if r.status_code in (200, 201) else (False, "failed")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def ensure_veracode_repo_imported(
    api_base: str,
    org: str,
    token: str,
    do_apply: bool,
    auto_import: bool = False,
    teams_value: Optional[str] = None,
    import_timeout_s: int = 900,
    import_poll_s: int = 5,
) -> Tuple[bool, Dict[str, Any]]:
    details: Dict[str, Any] = {"repo": INTEGRATION_REPO_NAME}
    exists = repo_exists(api_base, org, INTEGRATION_REPO_NAME, token)

    if exists and not repo_is_empty(api_base, org, INTEGRATION_REPO_NAME, token):
        details["status"] = "repo_exists"
        if teams_value:
            _ok, teams_msg = inject_teams_into_workflows(api_base, org, INTEGRATION_REPO_NAME, token, teams_value)
            details["teams_injection"] = teams_msg
        return True, details

    if exists:
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
            print(f"  [{org}] Git CLI not available - skipping auto import")
            auto_import = False
        else:
            ok, message = git_mirror_import(INTEGRATION_SOURCE_URL, org, INTEGRATION_REPO_NAME, token)
            if ok:
                time.sleep(2)
                if check_main_branch_exists(api_base, org, INTEGRATION_REPO_NAME, token):
                    yml_ok, yml_action = inject_veracode_yml(api_base, org, INTEGRATION_REPO_NAME, token)
                    details["status"] = "repo_created_and_imported"
                    details["import_method"] = "git_cli_auto"
                    details["veracode_yml_injected"] = yml_action if yml_ok else "failed"
                    if teams_value:
                        time.sleep(1)
                        _ok, teams_msg = inject_teams_into_workflows(
                            api_base, org, INTEGRATION_REPO_NAME, token, teams_value
                        )
                        details["teams_injection"] = teams_msg
                    return True, details
                else:
                    print(f"  [{org}] Warning: main branch not found after import")
                    details["status"] = "repo_created_and_imported"
                    details["import_method"] = "git_cli_auto"
                    details["veracode_yml_injected"] = False
                    return True, details
            else:
                print(f"  [{org}] Auto import failed: {message}")
                auto_import = False

    details["status"] = "repo_created_manual_import_required"
    details["import_instructions"] = {
        "web_importer_url": f"https://github.com/{org}/{INTEGRATION_REPO_NAME}/import",
        "source_url": INTEGRATION_SOURCE_URL,
        "note": "Manual import required - use GitHub web UI",
    }
    return False, details


def list_org_installations(api_base: str, org: str, token: str) -> List[dict]:
    r = request("GET", f"{api_base}/orgs/{org}/installations", token)
    if r.status_code >= 400:
        raise RuntimeError(f"{org}: cannot list installations ({r.status_code}) {r.text}")
    return r.json().get("installations", [])


def find_app_installation(api_base: str, org: str, token: str, app_slug: str) -> Optional[dict]:
    for inst in list_org_installations(api_base, org, token):
        slug = inst.get("app_slug") or inst.get("app", {}).get("slug")
        if slug == app_slug:
            return inst
    return None


def get_org_id(api_base: str, org: str, token: str) -> Optional[int]:
    try:
        r = request("GET", f"{api_base}/orgs/{org}", token)
        if r.status_code == 200:
            return r.json().get("id")
    except Exception:
        pass
    return None


def manual_install_url(web_base: str, org: str, org_id: Optional[int] = None) -> str:
    if org_id:
        return f"{web_base}/apps/{APP_SLUG}/installations/new/permissions?target_id={org_id}"
    return f"{web_base}/apps/{APP_SLUG}/installations/new"


def enterprise_install(
    api_base: str,
    enterprise: str,
    org: str,
    token: str,
    client_id: str,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"{api_base}/enterprises/{enterprise}/apps/organizations/{org}/installations"
    payload: Dict[str, Any] = {"client_id": client_id, "repository_selection": "all"}
    r = request("POST", url, token, json=payload)
    res: Dict[str, Any] = {
        "endpoint": url,
        "http_status": r.status_code,
        "response_snippet": r.text[:500] if r.text else "",
    }
    if r.status_code in (200, 201):
        res["result"] = "installed"
        return True, res
    if r.status_code in (403, 404):
        res["result"] = "blocked"
        return False, res
    res["result"] = "error"
    return False, res


def ensure_app_installed(
    api_base: str,
    web_base: str,
    org: str,
    token: str,
    do_apply: bool,
    allow_install_attempt: bool,
    enterprise: Optional[str],
    client_id: Optional[str],
) -> Tuple[bool, Dict[str, Any]]:
    inst = find_app_installation(api_base, org, token, APP_SLUG)
    if inst:
        return True, {
            "status": "already_installed",
            "installation_id": inst.get("id"),
            "repository_selection": inst.get("repository_selection"),
        }

    details: Dict[str, Any] = {"status": "missing"}
    org_id = get_org_id(api_base, org, token)

    if not do_apply or not allow_install_attempt or not enterprise or not client_id:
        details["next"] = "manual_install"
        details["install_url"] = manual_install_url(web_base, org, org_id)
        details["reason"] = "manual_install_required"
        return False, details

    _ok, attempt = enterprise_install(api_base, enterprise, org, token, client_id)
    details["automation_attempt"] = attempt

    time.sleep(0.5)
    inst2 = find_app_installation(api_base, org, token, APP_SLUG)
    if inst2:
        return True, {
            "status": "installed_after_attempt",
            "installation_id": inst2.get("id"),
            "repository_selection": inst2.get("repository_selection"),
            "attempt": attempt,
        }

    details["next"] = "manual_install"
    details["install_url"] = manual_install_url(web_base, org, org_id)
    details["reason"] = "auto_install_blocked"
    return False, details


# ---------------------------------------------------------------------------
# Teams map helpers
# ---------------------------------------------------------------------------

def load_teams_map(teams_file: str) -> Dict[str, str]:
    teams_map: Dict[str, str] = {}
    try:
        with open(teams_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                org_name = (row.get("org") or "").strip()
                teams_value = (row.get("teams") or "").strip().strip('"')
                if org_name:
                    teams_map[org_name] = teams_value
        print(f"[teams-map] Loaded {len(teams_map)} org->teams mappings from {teams_file}")
    except Exception as exc:
        print(f"[ERROR] Failed to load teams file '{teams_file}': {exc}", file=sys.stderr)
        sys.exit(1)
    return teams_map


def write_teams_map_csv(path: Path, orgs: List[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(["org", "teams"])
        for org in orgs:
            writer.writerow([org, ""])


def write_orgs_txt(path: Path, orgs: List[str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for org in orgs:
            f.write(org + "\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Veracode GitHub Workflow Integration rollout helper"
    )
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

    ap.add_argument("--install-app", action="store_true",
                    help="[apply] Attempt enterprise installation of the Veracode Workflow App.")
    ap.add_argument("--set-secrets", action="store_true",
                    help="[apply] Set VERACODE_API_ID, VERACODE_API_KEY, VERACODE_AGENT_TOKEN secrets.")

    ap.add_argument("--enterprise", help="GitHub Enterprise slug.")
    ap.add_argument("--app-client-id", help="GitHub App client ID (required for --install-app).")
    ap.add_argument("--orgs-file", help="Path to a file with one org login per line.")
    ap.add_argument("--out", default="out", help="Output directory (default: ./out).")

    ap.add_argument("--api-base", default=env("GITHUB_API_BASE", "https://api.github.com"),
                    help="GitHub API base URL.")
    ap.add_argument("--web-base", default=env("GITHUB_WEB_BASE", "https://github.com"),
                    help="GitHub web base URL (used for manual install links).")
    ap.add_argument("--token-env", default="GITHUB_TOKEN",
                    help="Environment variable holding the GitHub PAT (default: GITHUB_TOKEN).")

    ap.add_argument("--import-timeout", type=int, default=900,
                    help="Repo import timeout in seconds (default: 900).")
    ap.add_argument("--import-poll", type=int, default=5,
                    help="Repo import poll interval in seconds (default: 5).")

    ap.add_argument("--skip-to", help="Skip all orgs before this one and start from here.")
    ap.add_argument("--continue", dest="resume", action="store_true",
                    help="Resume from the last checkpoint saved in checkpoint.json.")

    args = ap.parse_args()

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

    api_base: str = args.api_base.rstrip("/")
    web_base: str = args.web_base.rstrip("/")
    enterprise: Optional[str] = args.enterprise
    client_id: Optional[str] = args.app_client_id

    do_apply_repo = bool(args.apply and args.import_repo)
    do_apply_app = bool(args.apply and args.install_app)
    do_set_secrets = bool(args.apply and args.set_secrets)
    do_set_teams = bool(args.apply and (args.set_teams_auto or args.set_teams_file))

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

    teams_map: Dict[str, str] = {}
    if args.set_teams_file:
        teams_map = load_teams_map(args.set_teams_file)

    print(f"\n{'=' * 60}")
    print(f"MODE: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"{'=' * 60}")
    if args.apply:
        print(f"  Import missing repos  : {'YES' if do_apply_repo else 'NO (--import-repo)'}")
        if do_set_teams:
            if args.set_teams_auto:
                print(f"  Set teams in workflows: YES (auto - org name)")
            else:
                print(f"  Set teams in workflows: YES (from {args.set_teams_file})")
        else:
            print(f"  Set teams in workflows: NO (--set-teams-auto or --set-teams-file)")
        print(f"  Install missing apps  : {'YES' if do_apply_app else 'NO (--install-app)'}")
        print(f"  Set Veracode secrets  : {'YES' if do_set_secrets else 'NO (--set-secrets)'}")
        if do_apply_app:
            print(f"    Enterprise   : {enterprise or 'NOT SET (required for app install)'}")
            print(f"    App Client ID: {client_id or 'NOT SET (required for app install)'}")
        if do_set_secrets:
            print(f"    VERACODE_API_ID     : {'SET' if veracode_api_id else 'NOT SET'}  (admin - for API calls)")
            print(f"    VERACODE_API_KEY    : {'SET' if veracode_api_key else 'NOT SET'}  (admin - for API calls)")
            print(f"    VERACODE_SA_API_ID  : {'SET' if veracode_sa_api_id else 'NOT SET'}  (service account - stored in orgs)")
            print(f"    VERACODE_SA_API_KEY : {'SET' if veracode_sa_api_key else 'NOT SET'}  (service account - stored in orgs)")
    else:
        print("  No changes will be made (use --apply to enable changes)")
    print(f"{'=' * 60}\n")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    all_orgs = list_orgs(api_base, token, enterprise, args.orgs_file)

    if args.orgs_file and enterprise:
        try:
            with open(args.orgs_file, encoding="utf-8") as f:
                filter_orgs = {line.strip() for line in f if line.strip() and not line.strip().startswith("#")}
            filtered = [o for o in all_orgs if o in filter_orgs]
            print(f"[OK] Filtered to {len(filtered)} orgs from {args.orgs_file}")
            orgs = filtered
        except Exception as exc:
            print(f"[WARNING] Could not apply orgs-file filter: {exc}")
            orgs = all_orgs
    else:
        orgs = all_orgs

    orgs_txt_path = outdir / "orgs.txt"
    teams_map_csv_path = outdir / "teams_map.csv"

    if args.dry_run or not orgs_txt_path.exists():
        write_orgs_txt(orgs_txt_path, orgs)

    if args.dry_run or not teams_map_csv_path.exists():
        write_teams_map_csv(teams_map_csv_path, orgs)

    checkpoint_file = outdir / "checkpoint.json"
    start_index = 0

    if args.resume and checkpoint_file.exists():
        try:
            checkpoint_data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            last_org = checkpoint_data.get("last_org")
            if last_org and last_org in orgs:
                start_index = orgs.index(last_org) + 1
                print(f"[RESUME] Continuing after: {last_org}  (skipping {start_index} orgs)\n")
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
    report_path = outdir / "audit_report.json"

    if report_path.exists():
        report_path.unlink()

    missing_repo_rows: List[List[str]] = []
    missing_app_rows: List[List[str]] = []
    manual_links_rows: List[List[str]] = []

    for org_idx, org in enumerate(orgs, 1):
        progress_pct = (org_idx / total_orgs) * 100 if total_orgs else 100.0
        print(f"\n[{org_idx}/{total_orgs} ({progress_pct:.1f}%)] Processing: {org}")

        entry: Dict[str, Any] = {"org": org}

        if do_set_teams:
            if args.set_teams_auto:
                teams_value: Optional[str] = org
            else:
                teams_value = teams_map.get(org, "").strip() or None
        else:
            teams_value = None

        try:
            repo_ok, repo_details = ensure_veracode_repo_imported(
                api_base, org, token,
                do_apply=do_apply_repo,
                auto_import=do_apply_repo,
                teams_value=teams_value,
                import_timeout_s=args.import_timeout,
                import_poll_s=args.import_poll,
            )
            entry["veracode_repo"] = {"present": repo_ok, **repo_details}
            if not repo_ok:
                missing_repo_rows.append([org, INTEGRATION_REPO_NAME, repo_details.get("note", "missing")])
        except Exception as exc:
            entry["veracode_repo"] = {"present": None, "status": "error", "error": str(exc)}
            missing_repo_rows.append([org, INTEGRATION_REPO_NAME, f"error:{exc}"])
            print(f"[{org}] Repo error: {str(exc)[:80]}")

        try:
            app_ok, app_details = ensure_app_installed(
                api_base=api_base, web_base=web_base, org=org, token=token,
                do_apply=args.apply, allow_install_attempt=do_apply_app,
                enterprise=enterprise, client_id=client_id,
            )
            entry["workflow_app"] = {"installed": app_ok, **app_details}
            if not app_ok:
                missing_app_rows.append([org, APP_SLUG, app_details.get("reason", "missing")])
                if app_details.get("install_url"):
                    manual_links_rows.append([org, app_details["install_url"], app_details.get("reason", "")])
        except Exception as exc:
            entry["workflow_app"] = {"installed": None, "status": "error", "error": str(exc)}
            missing_app_rows.append([org, APP_SLUG, f"error:{exc}"])
            print(f"[{org}] App error: {str(exc)[:80]}")

        if do_set_secrets:
            try:
                workspace_id = create_veracode_workspace(org, veracode_api_id, veracode_api_key)
                if not workspace_id:
                    entry["secrets"] = {"status": "error", "error": "Failed to create or find Veracode workspace"}
                else:
                    agent_token = create_veracode_agent_token(workspace_id, org, veracode_api_id, veracode_api_key)
                    if not agent_token:
                        entry["secrets"] = {"status": "error", "error": "Failed to generate agent token"}
                    else:
                        ok, results = set_veracode_secrets(
                            api_base, org, token, veracode_sa_api_id, veracode_sa_api_key, agent_token
                        )
                        entry["secrets"] = {"status": "set" if ok else "partial", "results": results}
            except Exception as exc:
                entry["secrets"] = {"status": "error", "error": str(exc)}
                print(f"[{org}] Secrets error: {str(exc)[:80]}")

        write_report_entry(report_path, entry)

        repo_status = "+" if entry.get("veracode_repo", {}).get("present") else "x"
        app_status = "+" if entry.get("workflow_app", {}).get("installed") else "x"
        teams_detail = ""
        if do_set_teams:
            injection = entry.get("veracode_repo", {}).get("teams_injection")
            if teams_value:
                teams_detail = f" ({injection})" if injection else " (teams_injection_error)"
            else:
                teams_detail = " (no teams configured)"
        secrets_status = ""
        if do_set_secrets:
            s = entry.get("secrets", {})
            if s.get("status") == "set":
                r = s.get("results", {})
                set_count = sum(1 for v in r.values() if v == "set")
                exists_count = sum(1 for v in r.values() if v == "exists")
                if exists_count == 3:
                    secrets_status = "  Secrets: + (all exist)"
                elif set_count > 0:
                    secrets_status = f"  Secrets: + (set {set_count}, existed {exists_count})"
                else:
                    secrets_status = "  Secrets: +"
            else:
                secrets_status = "  Secrets: x"
        print(f"[{org}] Repo: {repo_status}{teams_detail}  App: {app_status}{secrets_status}")

        abs_processed = start_index + org_idx
        if org_idx % 10 == 0:
            try:
                checkpoint_file.write_text(
                    json.dumps({"last_org": org, "processed": abs_processed}, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:
                print(f"  [WARNING] Failed to save checkpoint: {exc}")

    write_csv(outdir / "missing_veracode_repo.csv", ["organization", "repo_name", "note"], missing_repo_rows)
    write_csv(outdir / "missing_workflow_app.csv", ["organization", "app_slug", "note"], missing_app_rows)
    write_csv(outdir / "manual_install_links.csv", ["organization", "install_link", "reason"], manual_links_rows)

    print("\nOutputs written to:", outdir.resolve())
    print(" - orgs.txt")
    print(" - teams_map.csv")
    print(" - audit_report.json")
    print(" - missing_veracode_repo.csv")
    print(" - missing_workflow_app.csv")
    print(" - manual_install_links.csv")

    if missing_repo_rows or missing_app_rows:
        sys.exit(3)
    sys.exit(0)


if __name__ == "__main__":
    main()
