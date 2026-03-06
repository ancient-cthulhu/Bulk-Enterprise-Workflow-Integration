from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests 

APP_SLUG = "veracode-workflow-app"
INTEGRATION_REPO_NAME = "veracode"
INTEGRATION_SOURCE_URL = "https://github.com/veracode/github-actions-integration.git"
API_VER = "2022-11-28"

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
    """Check GitHub rate limit from response headers and warn/wait if low."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset_time = response.headers.get("X-RateLimit-Reset")
    
    if remaining and reset_time:
        remaining = int(remaining)
        reset_time = int(reset_time)
        
        # Warn when getting low
        if remaining < 100:
            reset_dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset_time))
            print(f"  [WARNING] Rate limit low: {remaining} requests remaining (resets at {reset_dt})")
        
        # Wait if critically low
        if remaining < 10:
            wait_time = max(reset_time - int(time.time()), 0) + 5
            if wait_time > 0:
                print(f"  [RATE LIMIT] Pausing for {wait_time}s until rate limit resets...")
                time.sleep(wait_time)

def request(method: str, url: str, token: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """
    Make HTTP request with rate limit checking and retry logic.
    Retries on 429 (rate limit) and 5xx errors with exponential backoff.
    """
    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, headers=headers(token), timeout=45, **kwargs)
            
            # Check rate limits
            check_rate_limit(r)
            
            # Handle rate limiting
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                if attempt < max_retries - 1:
                    print(f"  [RATE LIMIT] 429 received, waiting {retry_after}s before retry {attempt + 1}/{max_retries}...")
                    time.sleep(retry_after)
                    continue
                else:
                    return r  # Last attempt, return the error
            
            # Handle server errors with exponential backoff
            if r.status_code >= 500:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 2  # 2s, 4s, 8s
                    print(f"  [SERVER ERROR] {r.status_code} received, waiting {wait_time}s before retry {attempt + 1}/{max_retries}...")
                    time.sleep(wait_time)
                    continue
                else:
                    return r  # Last attempt, return the error
            
            # Success or client error (4xx except 429)
            return r
            
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 2
                print(f"  [TIMEOUT] Request timed out, waiting {wait_time}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
                continue
            else:
                raise
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 2
                print(f"  [NETWORK ERROR] {str(e)[:50]}, waiting {wait_time}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
                continue
            else:
                raise
    
    # Should not reach here
    raise RuntimeError(f"Failed after {max_retries} retries")

def parse_link_next(link_header: str) -> Optional[str]:
    # Minimal RFC5988 parsing for rel="next"
    parts = [p.strip() for p in link_header.split(",")]
    for part in parts:
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

        nxt = None
        link = r.headers.get("Link") or r.headers.get("link")
        if link:
            nxt = parse_link_next(link)

        url = nxt
        params = None
    return out

def write_csv(path: Path, header: List[str], rows: List[List[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for row in rows:
            # Basic CSV writing; org names normally won't contain commas.
            f.write(",".join(row) + "\n")

def check_git_available() -> bool:
    """Check if git CLI is available on the system."""
    try:
        result = os.system("git --version > nul 2>&1" if sys.platform == "win32" else "git --version > /dev/null 2>&1")
        return result == 0
    except Exception:
        return False

def git_mirror_import(source_url: str, target_org: str, target_repo: str, token: str) -> Tuple[bool, str]:
    """
    Use git CLI to mirror a repository.
    Returns (success, message).
    """
    import tempfile
    import shutil
    import subprocess
    
    temp_dir = None
    try:
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="veracode-import-")
        bare_repo = os.path.join(temp_dir, "repo.git")
        
        # Clone bare repository
        result = subprocess.run(
            ["git", "clone", "--bare", source_url, bare_repo],
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode != 0:
            return False, f"Clone failed: {result.stderr}"
        
        # Push mirror to target
        target_url = f"https://{token}@github.com/{target_org}/{target_repo}.git"
        result = subprocess.run(
            ["git", "-C", bare_repo, "push", "--mirror", target_url],
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode != 0:
            return False, f"Push failed: {result.stderr}"
        
        return True, "Import successful"
        
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)
    finally:
        # Cleanup temp directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

def veracode_request(method: str, endpoint: str, api_id: str, api_key: str, **kwargs) -> requests.Response:
    """Make authenticated request to Veracode API using the official Veracode signing library."""
    from veracode_api_signing.plugin_requests import RequestsAuthPluginVeracodeHMAC
    
    base_url = "https://api.veracode.com"
    url = f"{base_url}{endpoint}"
    
    # Use the official Veracode HMAC authentication plugin
    auth = RequestsAuthPluginVeracodeHMAC(api_key_id=api_id, api_key_secret=api_key)
    
    return requests.request(method, url, auth=auth, timeout=30, **kwargs)

def create_veracode_workspace(org_name: str, api_id: str, api_key: str) -> Optional[str]:
    """
    Create a Veracode workspace for the organization.
    Returns workspace_id or None on failure.
    """
    try:
        r = veracode_request("GET", "/srcclr/v3/workspaces", api_id, api_key)
        
        if r.status_code == 200:
            data = r.json()
            workspaces = data.get("_embedded", {}).get("workspaces", [])
            for ws in workspaces:
                if ws.get("name") == org_name:
                    return ws.get("id")
        elif r.status_code == 401:
            print(f"  [ERROR] Veracode authentication failed - check credentials")
            return None
        elif r.status_code == 403:
            print(f"  [ERROR] Veracode permission denied - insufficient access")
            return None
        
        payload = {"name": org_name}
        r = veracode_request("POST", "/srcclr/v3/workspaces", api_id, api_key, json=payload)
        
        if r.status_code in (200, 201):
            if not r.content:
                print(f"  [ERROR] Workspace created but response is empty")
                return None
            
            try:
                data = r.json()
                ws_id = data.get("id")
                if ws_id:
                    return ws_id
                else:
                    print(f"  [ERROR] Workspace created but no ID in response")
                    return None
            except json.JSONDecodeError:
                print(f"  [ERROR] Failed to parse workspace response")
                return None
        else:
            print(f"  [ERROR] Failed to create workspace: {r.status_code}")
            return None
        
    except Exception as e:
        print(f"  [ERROR] create_veracode_workspace: {str(e)}")
        return None

def list_veracode_agents(workspace_id: str, api_id: str, api_key: str) -> Optional[List[dict]]:
    """
    List all agents in a Veracode workspace.
    Returns list of agent objects or None on failure.
    """
    try:
        r = veracode_request("GET", f"/srcclr/v3/workspaces/{workspace_id}/agents", api_id, api_key)
        if r.status_code == 200:
            data = r.json()
            # API returns {"_embedded": {"agents": [...]}}
            agents = data.get("_embedded", {}).get("agents", [])
            return agents
        return None
    except Exception:
        return None

def delete_veracode_agent(workspace_id: str, agent_id: str, api_id: str, api_key: str) -> bool:
    """
    Delete an agent from a Veracode workspace.
    Returns True on success, False on failure.
    """
    try:
        r = veracode_request("DELETE", f"/srcclr/v3/workspaces/{workspace_id}/agents/{agent_id}", api_id, api_key)
        return r.status_code in (200, 204)
    except Exception:
        return False

def create_veracode_agent_token(workspace_id: str, org_name: str, api_id: str, api_key: str) -> Optional[str]:
    """
    Create an agent token for the workspace.
    Checks for existing agents with the same name and deletes them first.
    Returns agent token or None on failure.
    """
    try:
        suffix = "-agt"
        max_org_len = 20 - len(suffix)
        
        truncated_org = org_name[:max_org_len]
        if not truncated_org[0].isalpha():
            truncated_org = "gh" + truncated_org[:(max_org_len-2)]
        
        agent_name = f"{truncated_org}{suffix}"
        
        existing_agents = list_veracode_agents(workspace_id, api_id, api_key)
        
        if existing_agents:
            for agent in existing_agents:
                if agent.get("name") == agent_name:
                    agent_id = agent.get("id")
                    if not delete_veracode_agent(workspace_id, agent_id, api_id, api_key):
                        print(f"  [WARNING] Failed to delete old agent, creating new one")
        
        payload = {"name": agent_name, "agent_type": "CLI"}
        r = veracode_request("POST", f"/srcclr/v3/workspaces/{workspace_id}/agents", api_id, api_key, json=payload)
        
        if r.status_code in (200, 201):
            if not r.content:
                print(f"  [ERROR] Agent created but response is empty")
                return None
            
            try:
                agent_data = r.json()
                token_data = agent_data.get("token", {})
                access_token = token_data.get("access_token")
                if access_token:
                    return access_token
                else:
                    print(f"  [ERROR] Agent created but no access_token in response")
                    return None
            except json.JSONDecodeError:
                print(f"  [ERROR] Failed to parse agent response")
                return None
        else:
            print(f"  [ERROR] Failed to create agent: {r.status_code} - {r.text[:200]}")
            return None
    except Exception as e:
        print(f"  [ERROR] create_veracode_agent_token: {str(e)}")
        return None

def get_org_public_key(api_base: str, org: str, token: str) -> Optional[Tuple[str, str]]:
    """
    Get organization's public key for encrypting secrets.
    Returns (key_id, key) or None on failure.
    """
    try:
        r = request("GET", f"{api_base}/orgs/{org}/actions/secrets/public-key", token)
        if r.status_code == 200:
            data = r.json()
            return data.get("key_id"), data.get("key")
        return None
    except Exception:
        return None

def encrypt_secret(public_key: str, secret_value: str) -> str:
    """Encrypt a secret using the organization's public key."""
    from base64 import b64encode
    from nacl import encoding, public
    
    public_key_bytes = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key_bytes)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")

def check_existing_secrets(api_base: str, org: str, token: str) -> Dict[str, bool]:
    """
    Check which Veracode secrets already exist in the organization.
    Returns dict with True/False for each secret.
    """
    secrets_to_check = ["VERACODE_API_ID", "VERACODE_API_KEY", "VERACODE_AGENT_TOKEN"]
    results = {}
    
    for secret_name in secrets_to_check:
        try:
            r = request("GET", f"{api_base}/orgs/{org}/actions/secrets/{secret_name}", token)
            results[secret_name] = (r.status_code == 200)
        except Exception:
            results[secret_name] = False
    
    return results

def secret_exists(api_base: str, org: str, token: str, secret_name: str) -> bool:
    """Check if an organization-level Actions secret exists."""
    try:
        url = f"{api_base}/orgs/{org}/actions/secrets/{secret_name}"
        r = request("GET", url, token)
        
        if r.status_code == 200:
            return True
        elif r.status_code == 404:
            return False
        elif r.status_code == 403:
            print(f"  [{org}] Warning: No permission to check secret {secret_name}")
            return False
        else:
            print(f"  [{org}] Unexpected response checking {secret_name}: {r.status_code}")
            return False
            
    except Exception as e:
        print(f"  [{org}] Error checking secret {secret_name}: {str(e)}")
        return False

def set_org_secret(api_base: str, org: str, token: str, secret_name: str, secret_value: str) -> bool:
    """Set an organization-level Actions secret."""
    try:
        key_info = get_org_public_key(api_base, org, token)
        if not key_info:
            return False
        
        key_id, public_key = key_info
        encrypted_value = encrypt_secret(public_key, secret_value)
        
        payload = {
            "encrypted_value": encrypted_value,
            "key_id": key_id,
            "visibility": "all"
        }
        url = f"{api_base}/orgs/{org}/actions/secrets/{secret_name}"
        r = request("PUT", url, token, json=payload)
        
        if r.status_code in (201, 204):
            return True
        else:
            print(f"    [ERROR] Secret {secret_name} PUT failed: {r.status_code}")
            return False
    except Exception as e:
        print(f"    [ERROR] Exception setting secret {secret_name}: {str(e)}")
        return False

def set_veracode_secrets(api_base: str, org: str, github_token: str, 
                        veracode_api_id: str, veracode_api_key: str, 
                        veracode_agent_token: str) -> Tuple[bool, Dict[str, str]]:
    """Set all three Veracode secrets for an organization."""
    results = {}
    secrets_to_set = {
        "VERACODE_API_ID": veracode_api_id,
        "VERACODE_API_KEY": veracode_api_key,
        "VERACODE_AGENT_TOKEN": veracode_agent_token
    }
    
    for secret_name, secret_value in secrets_to_set.items():
        if secret_exists(api_base, org, github_token, secret_name):
            results[secret_name] = "exists"
        else:
            success = set_org_secret(api_base, org, github_token, secret_name, secret_value)
            
            if success:
                time.sleep(0.5)
                verified = secret_exists(api_base, org, github_token, secret_name)
                if verified:
                    results[secret_name] = "set"
                else:
                    print(f"  [ERROR] Secret {secret_name} PUT succeeded but verification failed")
                    results[secret_name] = "set_unverified"
            else:
                results[secret_name] = "failed"
    
    all_success = all(status in ("set", "exists") for status in results.values())
    return all_success, results

def list_orgs_graphql(api_base: str, token: str, enterprise: str) -> Optional[List[str]]:
    """
    Try to list enterprise orgs using GraphQL API.
    Returns list of org logins or None if it fails.
    This works on trial enterprises when REST API is blocked.
    """
    try:
        graphql_url = f"{api_base.replace('/api/v3', '')}/graphql"
        if graphql_url == f"{api_base}/graphql":  # For github.com
            graphql_url = "https://api.github.com/graphql"
        
        query = """
        query($enterprise: String!, $cursor: String) {
          enterprise(slug: $enterprise) {
            organizations(first: 100, after: $cursor) {
              nodes {
                login
              }
              pageInfo {
                hasNextPage
                endCursor
              }
            }
          }
        }
        """
        
        all_orgs = []
        cursor = None
        
        while True:
            variables = {"enterprise": enterprise}
            if cursor:
                variables["cursor"] = cursor
            
            r = request("POST", graphql_url, token, json={"query": query, "variables": variables})
            
            if r.status_code != 200:
                return None
            
            data = r.json()
            if "errors" in data:
                return None
            
            if "data" not in data or not data["data"].get("enterprise"):
                return None
            
            orgs_data = data["data"]["enterprise"]["organizations"]
            nodes = orgs_data.get("nodes", [])
            all_orgs.extend([node["login"] for node in nodes if "login" in node])
            
            page_info = orgs_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            
            cursor = page_info.get("endCursor")
        
        return all_orgs if all_orgs else None
        
    except Exception:
        return None

def list_orgs(api_base: str, token: str, enterprise: Optional[str], orgs_file: Optional[str]) -> List[str]:
    errors = []
    
    # Preferred: enterprise enumeration (GHEC)
    if enterprise:
        # Try GraphQL first (works on trial enterprises and is more reliable)
        print(f"Attempting to discover orgs via enterprise GraphQL API: enterprise(slug: \"{enterprise}\")")
        try:
            orgs = list_orgs_graphql(api_base, token, enterprise)
            if orgs:
                print(f"[OK] Found {len(orgs)} orgs via GraphQL API")
                return orgs
            else:
                print("  GraphQL returned no orgs")
                # When --enterprise is specified, don't fallback to user orgs
                print("\n[ERROR] Enterprise GraphQL API returned 0 organizations", file=sys.stderr)
                print(f"[ERROR] The enterprise '{enterprise}' appears to have no organizations", file=sys.stderr)
                print("\nPossible causes:", file=sys.stderr)
                print("  • Enterprise slug is incorrect (check spelling/capitalization)", file=sys.stderr)
                print("  • Enterprise exists but has no organizations", file=sys.stderr)
                print("  • Token lacks 'read:enterprise' scope", file=sys.stderr)
                print("  • You don't have access to this enterprise", file=sys.stderr)
                print("\nTo fix:", file=sys.stderr)
                print("  1. Verify your token with: gh auth status", file=sys.stderr)
                print("  2. Check enterprise slug at: https://github.com/enterprises/<slug>", file=sys.stderr)
                print("  3. Try without --enterprise to see your accessible orgs: python script.py --dry-run", file=sys.stderr)
                raise RuntimeError(f"Enterprise '{enterprise}' returned no organizations")
        except requests.exceptions.RequestException as e:
            # Network or HTTP errors
            print(f"\n[ERROR] Network/API error when accessing enterprise: {e}", file=sys.stderr)
            print("\nPossible causes:", file=sys.stderr)
            print("  • Invalid or expired GitHub token", file=sys.stderr)
            print("  • Token missing required scopes (need 'read:enterprise')", file=sys.stderr)
            print("  • Network connectivity issues", file=sys.stderr)
            print("\nTo fix:", file=sys.stderr)
            print("  1. Verify GITHUB_TOKEN is set: echo $env:GITHUB_TOKEN", file=sys.stderr)
            print("  2. Test token validity: gh auth status", file=sys.stderr)
            print("  3. Create new token with scopes: read:org, read:enterprise, admin:org", file=sys.stderr)
            print("     at https://github.com/settings/tokens", file=sys.stderr)
            raise RuntimeError(f"Failed to authenticate with enterprise API. Check your token credentials.")
        except Exception as e:
            # Other unexpected errors
            print(f"\n[ERROR] Unexpected error accessing enterprise: {e}", file=sys.stderr)
            print("\nTroubleshooting:", file=sys.stderr)
            print("  • Verify the enterprise slug is correct", file=sys.stderr)
            print("  • Check your token at: https://github.com/settings/tokens", file=sys.stderr)
            print("  • Try without --enterprise to use /user/orgs instead", file=sys.stderr)
            raise RuntimeError(f"Enterprise API failed: {e}")

    # Fallback: orgs token user belongs to (only when --enterprise NOT specified)
    try:
        print("Attempting to discover orgs via user API: /user/orgs")
        print("  Note: This returns ALL orgs you have access to, not filtered by enterprise")
        org_objs = paginate_list(f"{api_base}/user/orgs", token, params={"per_page": 100})
        orgs = [o["login"] for o in org_objs if "login" in o]
        if orgs:
            print(f"[OK] Found {len(orgs)} orgs via user API")
            return orgs
        else:
            errors.append("User API returned no orgs (token may not belong to any orgs)")
    except Exception as e:
        errors.append(f"User API failed: {e}")

    # Fallback: explicit file list
    if orgs_file:
        try:
            print(f"Attempting to read orgs from file: {orgs_file}")
            with open(orgs_file, "r", encoding="utf-8") as f:
                orgs = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
            if orgs:
                print(f"[OK] Found {len(orgs)} orgs from file")
                return orgs
            else:
                errors.append(f"File '{orgs_file}' contains no valid org names")
        except Exception as e:
            errors.append(f"File read failed: {e}")

    # If we got here, nothing worked
    print("\n[ERROR] Unable to determine org list. Tried:", file=sys.stderr)
    for i, error in enumerate(errors, 1):
        print(f"   {i}. {error}", file=sys.stderr)
    print("\nTroubleshooting:", file=sys.stderr)
    print("  • Ensure GITHUB_TOKEN environment variable is set with a valid token", file=sys.stderr)
    print("  • Verify token has 'read:org' scope", file=sys.stderr)
    print("  • Provide --enterprise <slug> if using GitHub Enterprise Cloud", file=sys.stderr)
    print("  • Provide --orgs-file <path> with one org name per line", file=sys.stderr)
    raise RuntimeError("Unable to determine org list. See errors above.")

def repo_exists(api_base: str, org: str, repo: str, token: str) -> bool:
    url = f"{api_base}/repos/{org}/{repo}"
    r = request("GET", url, token)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"{org}/{repo}: repo check failed {r.status_code} {r.text}")

def repo_is_empty(api_base: str, org: str, repo: str, token: str) -> bool:
    """Check if a repository is empty (has no commits)."""
    try:
        url = f"{api_base}/repos/{org}/{repo}/commits"
        r = request("GET", url, token, params={"per_page": 1})
        if r.status_code == 409:
            return True
        if r.status_code == 200:
            commits = r.json()
            return len(commits) == 0
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

def start_source_import(api_base: str, org: str, repo: str, token: str) -> None:
    payload = {"vcs_url": INTEGRATION_SOURCE_URL}
    r = request("PUT", f"{api_base}/repos/{org}/{repo}/import", token, json=payload)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"{org}/{repo}: import start failed {r.status_code} {r.text}")

def get_import_status(api_base: str, org: str, repo: str, token: str) -> dict:
    r = request("GET", f"{api_base}/repos/{org}/{repo}/import", token)
    if r.status_code == 200:
        return r.json()
    raise RuntimeError(f"{org}/{repo}: import status failed {r.status_code} {r.text}")

def wait_for_import(api_base: str, org: str, repo: str, token: str,
                    timeout_s: int = 900, poll_s: int = 5) -> dict:
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

def check_main_branch_exists(api_base: str, org: str, repo: str, token: str) -> bool:
    """Check if the main branch exists in the repository."""
    try:
        r = request("GET", f"{api_base}/repos/{org}/{repo}/branches/main", token)
        return r.status_code == 200
    except Exception:
        return False

def inject_teams_into_workflows(api_base: str, org: str, repo: str, token: str) -> Tuple[bool, str]:
    """
    Inject teams parameter into Veracode workflow files.
    Adds 'teams: "org-name"' to the uploadandscan-action steps.
    Returns (success, message).
    """
    try:
        workflow_files = [
            ".github/workflows/veracode-sandbox-scan.yml",
            ".github/workflows/veracode-policy-scan.yml"
        ]
        
        from base64 import b64encode, b64decode
        modified_count = 0
        
        for workflow_path in workflow_files:
            url = f"{api_base}/repos/{org}/{repo}/contents/{workflow_path}"
            r = request("GET", url, token)
            
            if r.status_code != 200:
                continue
            
            file_data = r.json()
            sha = file_data.get("sha")
            content_b64 = file_data.get("content", "")
            
            # Decode the content
            content = b64decode(content_b64).decode("utf-8")
            
            # Check if teams already exists
            if f'teams: "{org}"' in content or f"teams: '{org}'" in content or f"teams: {org}" in content:
                continue
            
            # Find the uploadandscan-action step and add teams parameter
            lines = content.split("\n")
            modified_lines = []
            in_uploadandscan_step = False
            in_with_section = False
            teams_added = False
            indent_level = 0
            
            for i, line in enumerate(lines):
                modified_lines.append(line)
                
                # Detect the uploadandscan-action step
                if "uses: veracode/uploadandscan-action@" in line:
                    in_uploadandscan_step = True
                    teams_added = False
                    continue
                
                # If we're in the uploadandscan step, look for the with: section
                if in_uploadandscan_step and "with:" in line:
                    in_with_section = True
                    indent_level = len(line) - len(line.lstrip())
                    continue
                
                # Add teams parameter right after "with:"
                if in_with_section and not teams_added:
                    # Check if this is the first parameter line after "with:"
                    stripped = line.lstrip()
                    if stripped and not stripped.startswith("#"):
                        # Get the indentation of the first parameter
                        param_indent = len(line) - len(line.lstrip())
                        # Insert teams parameter before this line
                        teams_line = " " * param_indent + f'teams: "{org}"'
                        modified_lines.insert(-1, teams_line)
                        teams_added = True
                        in_uploadandscan_step = False
                        in_with_section = False
            
            # Only update if we actually added teams
            if teams_added:
                new_content = "\n".join(modified_lines)
                new_content_b64 = b64encode(new_content.encode("utf-8")).decode("utf-8")
                
                payload = {
                    "message": f"Add teams parameter to {workflow_path.split('/')[-1]}",
                    "content": new_content_b64,
                    "sha": sha,
                    "branch": "main"
                }
                
                r = request("PUT", url, token, json=payload)
                if r.status_code in (200, 201):
                    modified_count += 1
        
        if modified_count > 0:
            return True, f"teams_added_to_{modified_count}_files"
        else:
            return True, "teams_already_present"
            
    except Exception as e:
        print(f"  [{org}] Failed to inject teams: {str(e)[:50]}")
        return False, "error"

def inject_veracode_yml(api_base: str, org: str, repo: str, token: str) -> Tuple[bool, str]:
    """
    Preserve the original veracode.yml as default-veracode.yml, then inject our custom veracode.yml.
    Returns (success, action) where action is 'created', 'updated', or 'failed'.
    """
    try:
        template_path = Path(__file__).parent / "veracode.yml"
        if not template_path.exists():
            print(f"  [{org}] Warning: veracode.yml template not found, skipping injection")
            return False, "template_not_found"
        
        with open(template_path, "r", encoding="utf-8") as f:
            custom_veracode_yml = f.read()
        
        from base64 import b64encode, b64decode
        
        veracode_url = f"{api_base}/repos/{org}/{repo}/contents/veracode.yml"
        default_veracode_url = f"{api_base}/repos/{org}/{repo}/contents/default-veracode.yml"
        
        # Check if original veracode.yml exists from the import
        r = request("GET", veracode_url, token)
        
        if r.status_code == 200:
            # Original exists - preserve it as default-veracode.yml
            original_data = r.json()
            original_sha = original_data.get("sha")
            original_content = original_data.get("content", "")
            
            # Check if default-veracode.yml already exists
            r_default = request("GET", default_veracode_url, token)
            default_payload = {
                "message": "Preserve original Veracode template as default-veracode.yml",
                "content": original_content,
                "branch": "main"
            }
            
            if r_default.status_code == 200:
                # default-veracode.yml exists, update it
                default_payload["sha"] = r_default.json().get("sha")
            
            # Create/update default-veracode.yml with original content
            request("PUT", default_veracode_url, token, json=default_payload)
            
            # Update veracode.yml with our custom content
            custom_content_encoded = b64encode(custom_veracode_yml.encode("utf-8")).decode("utf-8")
            custom_payload = {
                "message": "Update Veracode workflow configuration with custom settings",
                "content": custom_content_encoded,
                "branch": "main",
                "sha": original_sha
            }
            
            r = request("PUT", veracode_url, token, json=custom_payload)
            if r.status_code in (200, 201):
                return True, "updated_with_backup"
            return False, "failed"
        
        else:
            # No original veracode.yml - just create our custom one
            custom_content_encoded = b64encode(custom_veracode_yml.encode("utf-8")).decode("utf-8")
            payload = {
                "message": "Add Veracode workflow configuration",
                "content": custom_content_encoded,
                "branch": "main"
            }
            
            r = request("PUT", veracode_url, token, json=payload)
            if r.status_code in (200, 201):
                return True, "created"
            return False, "failed"
        
    except Exception as e:
        print(f"  [{org}] Failed to inject veracode.yml: {str(e)[:50]}")
        return False, "error"

def ensure_veracode_repo_imported(api_base: str, org: str, token: str, do_apply: bool, auto_import: bool = False, set_teams: bool = False) -> Tuple[bool, Dict[str, Any]]:
    """
    Returns (present, details).
    If do_apply=True, will create repo when missing or empty.
    If auto_import=True, will use git CLI to populate the repo automatically.
    If set_teams=True, will inject teams parameter into workflow files after import.
    """
    details: Dict[str, Any] = {"repo": INTEGRATION_REPO_NAME}
    exists = repo_exists(api_base, org, INTEGRATION_REPO_NAME, token)
    
    if exists:
        # Check if repo is empty
        is_empty = repo_is_empty(api_base, org, INTEGRATION_REPO_NAME, token)
        if not is_empty:
            details["status"] = "repo_exists"
            
            # If set_teams is enabled and repo exists with content, still try to add teams
            if set_teams:
                teams_success, teams_msg = inject_teams_into_workflows(api_base, org, INTEGRATION_REPO_NAME, token)
                details["teams_injection"] = teams_msg
            
            return True, details
        else:
            # Repo exists but is empty, treat as if it doesn't exist
            details["was_empty"] = True
    
    details["status"] = "missing"
    if not do_apply:
        details["note"] = "dry_run_only"
        return False, details

    # Apply: create repo if it doesn't exist
    if not exists:
        try:
            create_repo(api_base, org, INTEGRATION_REPO_NAME, token)
            details["created"] = True
        except Exception as e:
            raise RuntimeError(f"Failed to create repo: {str(e)}")

    # Try automatic import if enabled
    if auto_import:
        if not check_git_available():
            print(f"  [{org}] Git CLI not available - skipping import")
            auto_import = False
        else:
            success, message = git_mirror_import(INTEGRATION_SOURCE_URL, org, INTEGRATION_REPO_NAME, token)
            if success:
                time.sleep(2)
                
                if check_main_branch_exists(api_base, org, INTEGRATION_REPO_NAME, token):
                    yml_success, yml_action = inject_veracode_yml(api_base, org, INTEGRATION_REPO_NAME, token)
                    
                    details["status"] = "repo_created_and_imported"
                    details["import_method"] = "git_cli_auto"
                    details["veracode_yml_injected"] = yml_action if yml_success else "failed"
                    
                    # Inject teams if enabled
                    if set_teams:
                        time.sleep(1)
                        teams_success, teams_msg = inject_teams_into_workflows(api_base, org, INTEGRATION_REPO_NAME, token)
                        details["teams_injection"] = teams_msg
                    
                    return True, details
                else:
                    print(f"  [{org}] Warning: Main branch not found after import")
                    details["status"] = "repo_created_and_imported"
                    details["import_method"] = "git_cli_auto"
                    details["veracode_yml_injected"] = False
                    return True, details
            else:
                print(f"  [{org}] Import failed: {message}")
                auto_import = False

    if not auto_import:
        details["status"] = "repo_created_manual_import_required"
        details["import_instructions"] = {
            "web_importer_url": f"https://github.com/{org}/{INTEGRATION_REPO_NAME}/import",
            "source_url": INTEGRATION_SOURCE_URL,
            "note": "Manual import required - use GitHub web UI"
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

def enterprise_install(api_base: str, enterprise: str, org: str, token: str, client_id: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Attempt enterprise org-install endpoint.
    If blocked by policy, returns ok=False with result=blocked and the status code.
    """
    url = f"{api_base}/enterprises/{enterprise}/apps/organizations/{org}/installations"
    payload: Dict[str, Any] = {"client_id": client_id, "repository_selection": "all"}
    r = request("POST", url, token, json=payload)
    res = {
        "endpoint": url,
        "http_status": r.status_code,
        "response_snippet": (r.text[:500] if r.text else ""),
    }
    if r.status_code in (200, 201):
        res["result"] = "installed"
        return True, res
    if r.status_code in (403, 404):
        res["result"] = "blocked"
        return False, res
    res["result"] = "error"
    return False, res

def get_org_id(api_base: str, org: str, token: str) -> Optional[int]:
    """Get the numeric organization ID."""
    try:
        r = request("GET", f"{api_base}/orgs/{org}", token)
        if r.status_code == 200:
            return r.json().get("id")
    except Exception:
        pass
    return None

def manual_install_url(web_base: str, org: str, org_id: Optional[int] = None) -> str:
    """Generate manual install URL. Uses org ID if available for better UX."""
    if org_id:
        return f"{web_base}/apps/{APP_SLUG}/installations/new/permissions?target_id={org_id}"
    return f"{web_base}/apps/{APP_SLUG}/installations/new"

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
    """
    Returns (installed, details). If allowed, attempts enterprise installation and then re-checks.
    """
    inst = find_app_installation(api_base, org, token, APP_SLUG)
    if inst:
        return True, {
            "status": "already_installed",
            "installation_id": inst.get("id"),
            "repository_selection": inst.get("repository_selection"),
        }

    details: Dict[str, Any] = {"status": "missing"}

    # Get org ID for better install links
    org_id = get_org_id(api_base, org, token)

    # If not applying or not enabled, only provide manual link
    if (not do_apply) or (not allow_install_attempt) or (not enterprise) or (not client_id):
        details["next"] = "manual_install"
        details["install_url"] = manual_install_url(web_base, org, org_id)
        details["reason"] = "manual_install_required"
        return False, details

    ok, attempt = enterprise_install(api_base, enterprise, org, token, client_id)
    details["automation_attempt"] = attempt

    # Re-check: even if API says OK, org policies may prevent actual installation
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
    details["reason"] = f"auto_install_blocked"
    return False, details

def main() -> None:
    import argparse
    import json
    import sys
    import time
    from pathlib import Path
    from typing import Any, Dict, List, Optional

    ap = argparse.ArgumentParser(
        description="Veracode GitHub Workflow Integration rollout helper"
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode (default). No changes.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply mode. Enables changes (requires explicit flags below).",
    )

    ap.add_argument(
        "--import-repo",
        action="store_true",
        help="In apply mode: create/import the 'veracode' repo if missing (uses git CLI for automatic import).",
    )
    ap.add_argument(
        "--install-app",
        action="store_true",
        help="In apply mode: attempt installation of the Veracode Workflow App (enterprise API only).",
    )
    ap.add_argument(
        "--set-secrets",
        action="store_true",
        help="In apply mode: check and set GitHub Actions secrets (VERACODE_API_ID, VERACODE_API_KEY, VERACODE_AGENT_TOKEN).",
    )
    ap.add_argument(
        "--set-teams",
        action="store_true",
        help="In apply mode with --import-repo: inject teams parameter into Veracode workflow files (uses org name as team).",
    )

    ap.add_argument(
        "--enterprise",
        help="GitHub Enterprise slug (used to enumerate orgs and attempt enterprise install).",
    )
    ap.add_argument(
        "--app-client-id",
        help="GitHub App client ID (required to attempt enterprise app install).",
    )

    ap.add_argument("--orgs-file", help="Optional org list file (one org login per line).")
    ap.add_argument("--out", default="out", help="Output directory (default: ./out)")

    ap.add_argument(
        "--api-base",
        default=env("GITHUB_API_BASE", "https://api.github.com"),
        help="GitHub API base URL (default: https://api.github.com; GHES example: https://github.company.com/api/v3)",
    )
    ap.add_argument(
        "--web-base",
        default=env("GITHUB_WEB_BASE", "https://github.com"),
        help="GitHub Web base URL for install links (default: https://github.com; GHES example: https://github.company.com)",
    )

    ap.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="Environment variable that holds the GitHub token (default: GITHUB_TOKEN)",
    )

    ap.add_argument(
        "--import-timeout",
        type=int,
        default=900,
        help="Repo import timeout in seconds (default: 900)",
    )
    ap.add_argument(
        "--import-poll",
        type=int,
        default=5,
        help="Repo import poll interval seconds (default: 5)",
    )

    ap.add_argument("--skip-to", help="Skip to this organization name and continue from there")
    ap.add_argument(
        "--continue",
        dest="resume",
        action="store_true",
        help="Resume from last checkpoint (checkpoint.json)",
    )

    args = ap.parse_args()

    # Default to dry-run if neither set
    if not args.dry_run and not args.apply:
        args.dry_run = True

    # Check for pynacl if setting secrets
    if args.apply and args.set_secrets:
        try:
            import nacl  # noqa: F401
        except ImportError:
            print("ERROR: --set-secrets requires the 'pynacl' library.", file=sys.stderr)
            print("Install it with: pip install pynacl", file=sys.stderr)
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
    do_set_teams = bool(args.apply and args.set_teams)

    # Get Veracode credentials from environment if setting secrets
    veracode_api_id = env("VERACODE_API_ID") if do_set_secrets else None
    veracode_api_key = env("VERACODE_API_KEY") if do_set_secrets else None

    if do_set_secrets:
        if not veracode_api_id or not veracode_api_key:
            print(
                "ERROR: --set-secrets requires VERACODE_API_ID and VERACODE_API_KEY environment variables",
                file=sys.stderr,
            )
            print("\nSet them with:", file=sys.stderr)
            print("  Windows:   set VERACODE_API_ID=your_id", file=sys.stderr)
            print("             set VERACODE_API_KEY=your_key", file=sys.stderr)
            print("  Linux/Mac: export VERACODE_API_ID=your_id", file=sys.stderr)
            print("             export VERACODE_API_KEY=your_key", file=sys.stderr)
            sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"MODE: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"{'=' * 60}")
    if args.apply:
        print(f"  Import missing repos: {'YES' if do_apply_repo else 'NO (use --import-repo to enable)'}")
        print(f"  Set teams in workflows: {'YES' if do_set_teams else 'NO (use --set-teams to enable)'}")
        print(f"  Install missing apps: {'YES' if do_apply_app else 'NO (use --install-app to enable)'}")
        print(f"  Set Veracode secrets: {'YES' if do_set_secrets else 'NO (use --set-secrets to enable)'}")
        if do_apply_app:
            print(f"    Enterprise: {enterprise if enterprise else 'NOT SET (required for app install)'}")
            print(f"    App Client ID: {client_id if client_id else 'NOT SET (required for app install)'}")
        if do_set_secrets:
            print(f"    Veracode API ID: {'SET' if veracode_api_id else 'NOT SET'}")
            print(f"    Veracode API Key: {'SET' if veracode_api_key else 'NOT SET'}")
    else:
        print("  No changes will be made (use --apply to enable changes)")
    print(f"{'=' * 60}\n")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    orgs = list_orgs(api_base, token, enterprise, args.orgs_file)

    # Handle resume/skip-to
    checkpoint_file = outdir / "checkpoint.json"
    start_index = 0

    if args.resume and checkpoint_file.exists():
        try:
            checkpoint_data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            last_org = checkpoint_data.get("last_org")
            if last_org and last_org in orgs:
                start_index = orgs.index(last_org) + 1
                print(f"[RESUME] Continuing from checkpoint after: {last_org}")
                print(f"[RESUME] Skipping {start_index} already processed orgs\n")
        except Exception as e:
            print(f"[WARNING] Failed to load checkpoint: {e}")

    if args.skip_to:
        if args.skip_to in orgs:
            start_index = orgs.index(args.skip_to)
            print(f"[SKIP] Starting from: {args.skip_to}")
            print(f"[SKIP] Skipping {start_index} orgs\n")
        else:
            print(f"[WARNING] --skip-to org '{args.skip_to}' not found in org list")

    original_total = len(orgs)
    if start_index > 0:
        orgs = orgs[start_index:]
        print(f"Processing {len(orgs)} remaining organizations\n")

    # Track timing
    start_time = time.time()
    total_orgs = len(orgs)

    report: List[Dict[str, Any]] = []
    missing_repo_rows: List[List[str]] = []
    missing_app_rows: List[List[str]] = []
    manual_links_rows: List[List[str]] = []

    # Patch import timing settings into wait_for_import via globals (simple and explicit)
    global wait_for_import

    def wait_for_import(
        api_base_: str,
        org_: str,
        repo_: str,
        token_: str,
        timeout_s: int = None,
        poll_s: int = None,
    ) -> dict:
        timeout_s = args.import_timeout if timeout_s is None else timeout_s
        poll_s = args.import_poll if poll_s is None else poll_s

        deadline = time.time() + timeout_s
        last: dict = {}
        while time.time() < deadline:
            last = get_import_status(api_base_, org_, repo_, token_)
            status = (last.get("status") or "").lower()
            if status in ("complete", "succeeded"):
                return last
            if status in ("failed", "error"):
                raise RuntimeError(f"{org_}/{repo_}: import failed: {last}")
            time.sleep(poll_s)
        raise RuntimeError(f"{org_}/{repo_}: import timed out; last={last}")

    for org_idx, org in enumerate(orgs, 1):
        progress_pct = (org_idx / total_orgs) * 100 if total_orgs else 100.0
        print(f"\n[{org_idx}/{total_orgs} ({progress_pct:.1f}%)] Processing: {org}")

        entry: Dict[str, Any] = {"org": org}

        # Repo check/apply
        try:
            repo_ok, repo_details = ensure_veracode_repo_imported(
                api_base,
                org,
                token,
                do_apply=do_apply_repo,
                auto_import=do_apply_repo,
                set_teams=do_set_teams,
            )
            entry["veracode_repo"] = {"present": repo_ok, **repo_details}
            if not repo_ok:
                missing_repo_rows.append(
                    [org, INTEGRATION_REPO_NAME, entry["veracode_repo"].get("note", "missing")]
                )
        except Exception as e:
            entry["veracode_repo"] = {"present": None, "status": "error", "error": str(e)}
            missing_repo_rows.append([org, INTEGRATION_REPO_NAME, f"error:{e}"])
            print(f"[{org}] Repo error: {str(e)[:80]}")

        # App check/apply
        try:
            app_ok, app_details = ensure_app_installed(
                api_base=api_base,
                web_base=web_base,
                org=org,
                token=token,
                do_apply=args.apply,
                allow_install_attempt=do_apply_app,
                enterprise=enterprise,
                client_id=client_id,
            )
            entry["workflow_app"] = {"installed": app_ok, **app_details}
            if not app_ok:
                missing_app_rows.append([org, APP_SLUG, entry["workflow_app"].get("reason", "missing")])
                if entry["workflow_app"].get("install_url"):
                    manual_links_rows.append(
                        [org, entry["workflow_app"]["install_url"], entry["workflow_app"].get("reason", "")]
                    )
        except Exception as e:
            entry["workflow_app"] = {"installed": None, "status": "error", "error": str(e)}
            missing_app_rows.append([org, APP_SLUG, f"error:{e}"])
            print(f"[{org}] App error: {str(e)[:80]}")

        # Secrets setup
        if do_set_secrets:
            try:
                workspace_id = create_veracode_workspace(org, veracode_api_id, veracode_api_key)
                if not workspace_id:
                    entry["secrets"] = {"status": "error", "error": "Failed to create Veracode workspace"}
                else:
                    agent_token = create_veracode_agent_token(
                        workspace_id, org, veracode_api_id, veracode_api_key
                    )
                    if not agent_token:
                        entry["secrets"] = {"status": "error", "error": "Failed to generate agent token"}
                    else:
                        success, results = set_veracode_secrets(
                            api_base,
                            org,
                            token,
                            veracode_api_id,
                            veracode_api_key,
                            agent_token,
                        )
                        entry["secrets"] = {"status": "set" if success else "partial", "results": results}
            except Exception as e:
                entry["secrets"] = {"status": "error", "error": str(e)}
                print(f"[{org}] Secrets error: {str(e)[:80]}")

        report.append(entry)

        # Status summary
        repo_status = "✓" if entry.get("veracode_repo", {}).get("present") else "✗"
        app_status = "✓" if entry.get("workflow_app", {}).get("installed") else "✗"
        secrets_status = ""
        if do_set_secrets:
            if entry.get("secrets", {}).get("status") == "set":
                results = entry["secrets"].get("results", {})
                set_count = sum(1 for v in results.values() if v == "set")
                exists_count = sum(1 for v in results.values() if v == "exists")
                if exists_count == 3:
                    secrets_status = "  Secrets: ✓ (all exist)"
                elif set_count > 0:
                    secrets_status = f"  Secrets: ✓ (set {set_count}, existed {exists_count})"
                else:
                    secrets_status = "  Secrets: ✓"
            else:
                secrets_status = "  Secrets: ✗"
        print(f"[{org}] Repo: {repo_status}  App: {app_status}{secrets_status}")

        # Save checkpoint every 10 orgs (absolute processed count)
        abs_processed = start_index + org_idx
        if org_idx % 10 == 0:
            try:
                checkpoint_file.write_text(
                    json.dumps({"last_org": org, "processed": abs_processed}, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"  [WARNING] Failed to save checkpoint: {e}")

    # Write outputs
    (outdir / "audit_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    write_csv(
        outdir / "missing_veracode_repo.csv",
        ["organization", "repo_name", "note"],
        missing_repo_rows,
    )

    write_csv(
        outdir / "missing_workflow_app.csv",
        ["organization", "app_slug", "note"],
        missing_app_rows,
    )

    write_csv(
        outdir / "manual_install_links.csv",
        ["organization", "install_link", "reason"],
        manual_links_rows,
    )

    print("\nOutputs written to:", outdir.resolve())
    print(" - audit_report.json")
    print(" - missing_veracode_repo.csv")
    print(" - missing_workflow_app.csv")
    print(" - manual_install_links.csv")

    # Exit code: 0 if everything present; 3 if anything missing/error
    if missing_repo_rows or missing_app_rows:
        sys.exit(3)
    sys.exit(0)


if __name__ == "__main__":
    main()
