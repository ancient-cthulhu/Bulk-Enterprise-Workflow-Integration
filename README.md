# Veracode Bulk GitHub Workflow Integration

Deploys Veracode security scanning across GitHub Enterprise organizations at scale. Handles repository creation, workflow configuration, team assignments, Veracode platform team provisioning, app installation, and secrets management with audit trails, checkpoint/resume support, and parallel execution.

---

## How It Works

For each organization, the script can:

1. Create the `veracode` integration repository and mirror-import the Veracode workflow template
2. Inject a customized `veracode.yml` onboarding configuration
3. Create teams on the Veracode platform via the Identity API (if they do not already exist)
4. Inject `teams:` parameter into workflow files
5. Create a Veracode SCA workspace, generate a unique agent token, and set GitHub Actions secrets

All operations are idempotent, safe to re-run. If a repo import completes after the script times out, the next run detects the incomplete state via the absence of `default-veracode.yml` and automatically applies the missing post-import steps.

> **App installation is manual.** The script checks whether `veracode-workflow-app` is installed per org and generates `manual_install_links.csv` with a direct install URL for each org that needs it. Automated installation via the GitHub API is not supported for third-party apps.

---

## Modes

| Mode | Flag | Behavior |
|------|------|----------|
| Dry-run | *(default)* | Read-only. Reports current state and generates helper output files. |
| Apply | `--apply` | Makes changes. Requires one or more action flags. |

---

## Quickstart

### Phase 1 - Dry-run

```bash
export GITHUB_TOKEN="..."

python script.py --enterprise YOUR-ENTERPRISE
```

Discovers all orgs, checks current state, and writes output files to `./out/`:
- `orgs.txt` - one org per line
- `teams_map.csv` - fill in the `teams` column before apply
- `missing_veracode_repo.csv`, `missing_workflow_app.csv`, `manual_install_links.csv`

### Phase 2 - Apply

```bash
export GITHUB_TOKEN="..."
export VERACODE_API_ID="admin_api_id"
export VERACODE_API_KEY="admin_api_key"
export VERACODE_SA_API_ID="service_account_api_id"
export VERACODE_SA_API_KEY="service_account_api_key"

python script.py --apply \
  --enterprise YOUR-ENTERPRISE \
  --import-repo \
  --set-teams-file out/teams_map.csv \
  --create-teams \
  --set-secrets
```

---

## Requirements

```bash
pip install requests
pip install veracode-api-signing
pip install pynacl    # required for --set-secrets
git --version         # required for --import-repo
```

Python 3.8+

---

## Credentials

### Veracode Credentials

Two credential pairs serve different purposes:

| Variable | Purpose | Account Type |
|----------|---------|--------------|
| `VERACODE_API_ID` | Admin - used by the script to call Veracode APIs (create workspaces, generate tokens, create teams) | **Human user account** with the **Administrator** role |
| `VERACODE_API_KEY` | Admin | Same as above |
| `VERACODE_SA_API_ID` | Service account - stored as `VERACODE_API_ID` in each org's Actions secrets | API service account |
| `VERACODE_SA_API_KEY` | Service account - stored as `VERACODE_API_KEY` in each org's Actions secrets | API service account |

**Important:** The admin credentials (`VERACODE_API_ID` / `VERACODE_API_KEY`) must belong to a **human user account** with the **Administrator** role on the Veracode platform. API service accounts do not have sufficient permissions for the Identity API operations used by `--create-teams` and `--set-secrets`. The Identity API requires either a human user with the Administrator role or an API service account with the Admin API role, but workspace and agent token operations require a human administrator.

Admin credentials are never stored in any org. Service account credentials are what gets deployed to orgs and used by workflows at scan time.

### Which flags need which credentials

| Flag | Requires `VERACODE_API_ID/KEY` | Requires `VERACODE_SA_API_ID/KEY` |
|------|-------------------------------|-----------------------------------|
| `--create-teams` | Yes | No |
| `--set-secrets` | Yes | Yes |
| `--import-repo` | No | No |
| `--set-teams-*` | No | No |
| `--update-veracode-yml` | No | No |

---

## GitHub Token Permissions

| Operation | Required Scopes |
|-----------|----------------|
| Dry-run | `read:org`, `admin:org` (`admin:org` required to check secret status - without it secrets show as `no_permission` in the report) |
| `--enterprise` (org discovery) | + `read:enterprise` |
| `--import-repo` / `--set-teams-*` | + `repo`, `workflow` |
| `--update-veracode-yml` | + `repo`, `workflow` |
| `--set-secrets` | `admin:org` *(already covered above)* |

Full rollout with all flags: `read:org`, `admin:org`, `read:enterprise`, `repo`, `workflow`

---

## Command-Line Reference

### Action Flags *(require `--apply`)*

| Flag | Description |
|------|-------------|
| `--import-repo` | Create and populate the `veracode` repository |
| `--set-teams-auto` | Inject `teams: "<org-name>"` for every org |
| `--set-teams-file FILE` | Inject per-org team values from `teams_map.csv`. Blank rows are skipped. |
| `--set-teams-hybrid FILE` | Same as `--set-teams-file` but blank rows fall back to the org name |
| `--team-prefix PREFIX` | Prepend `PREFIX` to every resolved teams value. Applied after `--set-teams-auto/file/hybrid`. Example: `--team-prefix "gh-"` turns `acme-dev` into `gh-acme-dev`. Orgs with no resolved teams value (blank file row on the `--set-teams-file FILE` option) are not affected. |
| `--create-teams` | Create teams on the Veracode platform via the Identity API if they do not already exist. Uses the resolved teams value from `--set-teams-auto/file/hybrid` (after prefix). Requires `VERACODE_API_ID` and `VERACODE_API_KEY`. Must be combined with one of the `--set-teams-*` flags. See [Platform Team Creation](#platform-team-creation). |
| `--set-secrets` | Set `VERACODE_API_ID`, `VERACODE_API_KEY`, `VERACODE_AGENT_TOKEN` per org. Always overwrites all three - safe to re-run for annual credential rotation. The SCA agent token is regenerated via `token:regenerate` on each run, invalidating the previous one. |
| `--update-veracode-yml [FILE]` | Push a `veracode.yml` to the `veracode` repo in every org. By default fetches `veracode.yml` directly from the upstream integration repo (`github.com/veracode/github-actions-integration`). Pass a local `FILE` path to use a custom file instead. The current file is backed up as `default-veracode.yml` first. Orgs with a missing or not-yet-imported repo are skipped with a warning. |

### Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--enterprise SLUG` | - | GitHub Enterprise slug for org discovery |
| `--orgs-file FILE` | - | Plain text file, one org per line, `#` for comments. Used directly as the org scope when provided alone; used as a filter when combined with `--enterprise`. |
| `--api-base URL` | `https://api.github.com` | Override for GHES |
| `--web-base URL` | `https://github.com` | Override for GHES |
| `--out DIR` | `./out` | Output directory |
| `--skip-to ORG` | - | Skip all orgs before this one |
| `--continue` | - | Resume from last checkpoint |
| `--workers N` | `1` | Number of parallel worker threads. See [Parallel Execution](#parallel-execution). |

---

## Platform Team Creation

The `--create-teams` flag provisions teams on the Veracode platform before injecting the `teams:` parameter into workflow files. This ensures that the team referenced in scan workflows actually exists on the platform when scans run.

The script uses the Veracode Identity REST API (`/api/authn/v2/teams`) to:

1. Search all teams in the org for an exact name match
2. Create the team if it does not exist
3. Record the result (created, already_exists, or error) in the audit report

The operation is idempotent. Re-running with the same team names is safe and will report `already_exists` for teams that were previously created.

### Credential requirements

`--create-teams` requires the `VERACODE_API_ID` and `VERACODE_API_KEY` environment variables. These credentials must belong to a **human user account** with the **Administrator** role on the Veracode platform. The Identity API team creation endpoint requires this level of access.

### Usage

`--create-teams` must be combined with one of the `--set-teams-*` flags so the script knows which team name to create for each org.

```bash
# Create teams using the org name
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --set-teams-auto --create-teams

# Create teams from CSV, with prefix
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --set-teams-file out/teams_map.csv --team-prefix "gh-" --create-teams

# Full rollout: repo + platform teams + workflow injection + secrets
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --import-repo \
  --set-teams-auto --create-teams \
  --set-secrets
```
---

## Team Injection

The `teams` parameter maps to Veracode Platform teams that receive scan results. It is injected into both `veracode-policy-scan.yml` and `veracode-sandbox-scan.yml`:

```yaml
- uses: veracode/uploadandscan-action@v0.1.6
  with:
    teams: "security,devops"
```

Three modes are available:

- **`--set-teams-auto`** - uses the org name for every org, no configuration needed
- **`--set-teams-file`** - reads from `teams_map.csv`, skips blank rows
- **`--set-teams-hybrid`** - reads from `teams_map.csv`, falls back to org name for blank rows

Use `--team-prefix PREFIX` with any mode to prepend a fixed string to every resolved value. The prefix is applied after mode resolution, so it works identically across all three modes. Orgs that produce no teams value (blank row in file mode) are skipped and the prefix is not applied to them.

```bash
# Results in teams: "gh-acme-dev"
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --set-teams-auto --team-prefix "gh-"

# Results in teams: "security-<value-from-csv>"
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --set-teams-file out/teams_map.csv --team-prefix "security-"
```

`teams_map.csv` is generated automatically on every dry-run. Fill in the `teams` column (comma-separated names accepted) and pass it back on apply. Files that already have `teams:` are left unchanged.

The teams map is a lookup table, not a scope filter. Only orgs that are being processed (determined by `--enterprise`, `--orgs-file`, or `/user/orgs`) will be touched. If your orgs file has 1 org and your teams map has 100 entries, only the 1 org gets processed using its matching entry from the map if one exists.

> **Tip:** Combine `--set-teams-*` with `--create-teams` to ensure the team exists on the Veracode platform before it is referenced in workflow files. Without `--create-teams`, you are responsible for creating teams on the platform manually or through other automation before scans run.

---

## veracode.yml Configuration

The injected `veracode.yml` is pre-configured for onboarding with build gating disabled:

```yaml
analysis_on_platform: true
break_build_policy_findings: false
break_build_invalid_policy: false
break_build_on_error: false
policy: 'Omnicom Base Policy'
issues:
  trigger: true
  commands:
    - "Veracode All Scans"
```

Once onboarding is complete, re-enable gating by setting `break_build_policy_findings: true` and `break_build_invalid_policy: true`.

### Bulk veracode.yml Update

To push a new `veracode.yml` to all orgs after initial onboarding, for example to update the policy name, change scan triggers, or enable build gating across the fleet:

```bash
# Fetch veracode.yml from the upstream integration repo (default)
python script.py --apply --enterprise YOUR-ENTERPRISE --update-veracode-yml

# Use a custom local file instead
python script.py --apply --enterprise YOUR-ENTERPRISE --update-veracode-yml /path/to/veracode.yml
```

With no file argument the script fetches `veracode.yml` directly from `github.com/veracode/github-actions-integration` at runtime, so it always pulls the latest upstream version without needing a local copy. Pass a local file path when you want to deploy a customized configuration.

The current `veracode.yml` in each repo is preserved as `default-veracode.yml` before being overwritten. Orgs whose `veracode` repo is missing or not yet imported are skipped with a warning and counted separately in the summary.

`--update-veracode-yml` is optional and independent of the other flags. It only appears in the mode header, per-org log line, and execution summary when it is actively used.

---

## Credential Rotation

`--set-secrets` always overwrites all three secrets unconditionally, so re-running it with new credentials is all that is needed for annual rotation:

```bash
export GITHUB_TOKEN="..."
export VERACODE_API_ID="admin_api_id"
export VERACODE_API_KEY="admin_api_key"
export VERACODE_SA_API_ID="new_service_account_api_id"
export VERACODE_SA_API_KEY="new_service_account_api_key"

python script.py --apply --enterprise YOUR-ENTERPRISE --set-secrets
```

The SCA agent token (`VERACODE_AGENT_TOKEN`) is also rotated as part of this - the script calls `token:regenerate` on the existing agent for each org, which invalidates the old token and returns a fresh one.

---

## Organization Discovery

The script resolves orgs in this order:

1. **`--enterprise SLUG`** - GraphQL enterprise API (requires `read:enterprise`). If `--orgs-file` is also provided, the file is used to filter the enterprise org list down to only the listed orgs.
2. **`--orgs-file FILE`** - explicit list used directly as the org scope, one org per line.
3. **No flags** - falls back to `/user/orgs` (all orgs accessible to the token).

---

## Output Files

| File | Description |
|------|-------------|
| `orgs.txt` | All discovered orgs, one per line |
| `teams_map.csv` | Org list with blank `teams` column - fill in and pass to `--set-teams-file` |
| `audit_report_<timestamp>.json` | Per-org result log, written incrementally (crash-safe) |
| `missing_veracode_repo.csv` | Orgs missing the `veracode` repository |
| `missing_workflow_app.csv` | Orgs missing the workflow app |
| `manual_install_links.csv` | App install URLs for orgs that require manual installation |

### Audit Report Example

```json
{
  "org": "acme-dev",
  "veracode_repo": {
    "present": true,
    "status": "repo_created_and_imported",
    "import_method": "git_cli_auto",
    "veracode_yml_injected": "created"
  },
  "veracode_team_platform": {
    "team_name": "gh-acme-dev",
    "team_id": "7336556f-9ef2-4a1c-b536-be8608822db6",
    "action": "created"
  },
  "teams_injection": {
    "success": true,
    "action": "teams_updated_2_files",
    "value": "gh-acme-dev"
  },
  "workflow_app": {
    "installed": true,
    "status": "already_installed",
    "installation_id": 12345678
  },
  "veracode_yml_update": {
    "success": true,
    "action": "updated_with_backup"
  },
  "secrets": {
    "status": "set",
    "results": {
      "VERACODE_API_ID": "set",
      "VERACODE_API_KEY": "set",
      "VERACODE_AGENT_TOKEN": "set"
    }
  }
}
```

`veracode_team_platform.action` values: `already_exists`, `created`, `error`.

`veracode_yml_update.action` values: `updated_with_backup`, `created`, `repo_not_found`, `repo_empty`, `put_failed:<status_code>`.

`veracode_repo.status` values: `repo_exists`, `repo_exists_post_import_incomplete` (imported but post-import steps never ran - self-healed on this run), `repo_created_and_imported`, `repo_created_import_incomplete` (push succeeded but main branch not found yet), `repo_created_manual_import_required`, `missing`.

`secrets.status` values in dry-run: `all_exist`, `all_missing`, `partial`, `no_permission` (token lacks `admin:org` scope).

---

## Platform Notes

### GitHub Enterprise Cloud (GHEC)

All features supported.

```bash
python script.py --apply --import-repo --set-teams-auto --create-teams \
  --set-secrets \
  --enterprise your-enterprise-slug
```

### GitHub Enterprise Server (GHES)

```bash
python script.py --apply --import-repo --set-teams-file out/teams_map.csv --create-teams \
  --set-secrets \
  --enterprise your-enterprise-slug \
  --api-base https://github.company.com/api/v3 \
  --web-base https://github.company.com
```

Differences from GHEC:

- **Outbound access to github.com required for `--import-repo`** - the script clones from `github.com/veracode/github-actions-integration` and pushes to your GHES instance. If outbound access is blocked, mirror the template repo internally first or pre-populate the `veracode` repos manually.

---

## Large Deployments

For deployments across many orgs, use `--continue` to resume after interruption:

```bash
# Initial run
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --import-repo --set-teams-auto --create-teams --set-secrets

# Resume after interruption
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --import-repo --set-teams-auto --create-teams --set-secrets --continue
```

Checkpoint state is saved to `out/checkpoint.json` after each org completes. If the script is interrupted mid-org, `--continue` restarts from that org so nothing is skipped. All operations are idempotent so re-running a completed org is safe. The `--continue` flag also skips the confirmation prompt - confirmation was already given on the initial run.

Use `--skip-to ORG` to jump to a specific org without needing a checkpoint file.

For large enterprises, combine `--workers` with `--continue` to maximize throughput with crash recovery. See [Parallel Execution](#parallel-execution).

---

## Parallel Execution

By default the script processes one org at a time. Use `--workers N` to process multiple orgs concurrently using a thread pool. All API calls are I/O-bound, so threading provides real throughput gains with no additional processes or dependencies.

```bash
# Dry-run across 200 orgs using 5 parallel workers
python script.py --dry-run --enterprise YOUR-ENTERPRISE --workers 5

# Apply with 5 workers - fastest safe setting for most token budgets
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --import-repo --set-teams-auto --create-teams --set-secrets --workers 5
```

### Choosing a worker count

| Workers | Use case |
|---------|----------|
| `1` | Default. Sequential. Easiest to read logs. |
| `3` | Safe starting point for most deployments. |
| `5` | Recommended for large enterprises (100+ orgs). |
| `10` | Maximum recommended. Approaches rate limit risk at peak. |

GitHub's authenticated API rate limit is 5,000 requests per hour per token. Each org in a full apply run (`--import-repo --set-teams-auto --create-teams --set-secrets`) consumes roughly 18-25 API calls (the Identity API calls for `--create-teams` add 2-3 requests per org). At 5 workers processing ~10 orgs/min, a 200-org run stays well within the hourly limit. If the limit is approached, the script automatically pauses all workers until the window resets.

### Rate limit behavior with parallel workers

All workers share a single global rate limit state. When any worker receives a near-limit signal (`X-RateLimit-Remaining < 10`), it sets a shared resume timestamp and sleeps. Other workers check the shared timestamp before each request and wait if a pause is active. This prevents mass restarts once the window resets.

### Checkpoint compatibility

`--workers` is fully compatible with `--continue`. The checkpoint file records all completed orgs as a list alongside the latest processed org. On resume, the org list is replayed from the checkpoint position regardless of the order workers originally completed them.

```bash
# Initial parallel run
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --import-repo --set-teams-auto --create-teams --set-secrets --workers 5

# Resume after interruption, keeping the same worker count
python script.py --apply --enterprise YOUR-ENTERPRISE \
  --import-repo --set-teams-auto --create-teams --set-secrets --workers 5 --continue
```

### Log output with multiple workers

Per-org log lines are printed atomically but may arrive out of order relative to the org list (this is expected). The `[N/TOTAL]` progress prefix on each line shows the submission order. The audit report always contains one entry per org regardless of completion order.

---

## Security Notes

- Veracode admin credentials are used only for API calls and never stored anywhere
- Admin credentials must belong to a human user account with the Administrator role
- Service account credentials are encrypted via GitHub's public key API before being written to secrets
- Agent tokens are unique per organization and regenerated on each `--set-secrets` run
- All credentials are passed via environment variables and never hardcoded in source
- Default mode is read-only; all changes require explicit `--apply`

---

## Support

Supported platforms: GitHub.com, GitHub Enterprise Cloud, GitHub Enterprise Server

For issues, provide `out/audit_report_<timestamp>.json`, your platform type, and the command used.

> This is a community tool and is not officially supported by Veracode.
