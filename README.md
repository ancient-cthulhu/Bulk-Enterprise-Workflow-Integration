# Veracode Bulk GitHub Workflow Integration

Automated script to deploy Veracode GitHub Workflow integration across multiple GitHub organizations.

## Overview

Automates Veracode security scanning deployment across GitHub Enterprise organizations. Handles repository creation, workflow configuration, team assignments, app installation, and secrets management with detailed audit trails.

**Key capabilities:**
- Enterprise organization discovery
- Automatic repository import via git CLI
- Team parameter injection into workflow files (auto or per-org from CSV)
- Veracode workspace and GitHub Actions secrets automation
- Idempotent operations (safe to re-run)
- Comprehensive JSON audit reports
- Automatic rate limit handling with retry logic
- Checkpoint/resume for large deployments (100+ orgs)

## What It Does

For each GitHub organization:
1. Check/Create the `veracode` integration repository
2. Auto-import repository content from Veracode's template (via git CLI)
3. Inject customized `veracode.yml` with onboarding settings
4. Update workflow files with team name (via `--set-teams-auto` or `--set-teams-file`)
5. Check/Install the `veracode-workflow-app` GitHub App
6. Create Veracode workspace, generate agent token, and set GitHub Actions secrets

## Modes

- **DRY-RUN** (default): Read-only - reports status and generates helper files, no changes made
- **APPLY**: Makes changes when explicitly enabled with `--apply` and action flags

---

## Dry-Run & Apply Walkthrough

### Phase 1 - Dry-Run (No Changes Made)

Discovers your organizations, checks current state, and generates output files to plan your rollout.

#### Full audit + generate teams map

```bash
export GITHUB_TOKEN="your_github_token"

python script.py --enterprise YOUR-ENTERPRISE
```

This will:
- Discover all orgs via GraphQL
- Check each org for the `veracode` repo and `veracode-workflow-app`
- Write `out/orgs.txt` - one org per line, ready to pass to `--orgs-file` or trim down for a targeted apply
- Write `out/missing_veracode_repo.csv`, `out/missing_workflow_app.csv`, `out/manual_install_links.csv`
- Write `out/teams_map.csv` - one row per org with a blank `teams` column, ready to fill in
- If `--set-secrets` is also passed, checks and reports which orgs already have secrets configured


#### Preparing for apply

After the dry-run, fill in `out/teams_map.csv` before running apply with `--set-teams-file`:

```
"org","teams"
"acme-dev","security,devops"
"acme-staging","platform"
"acme-prod","security"
"acme-archive",""          <- leave blank to skip this org
```

The `teams` column maps to Veracode Platform teams that receive scan results. Accepts a single name or a comma-separated list.

---

### Phase 2 - Apply (Makes Changes)

Each flag is independent - run all together or only what you need.

#### Full rollout

```bash
export GITHUB_TOKEN="your_github_token"
export VERACODE_API_ID="admin_api_id"
export VERACODE_API_KEY="admin_api_key"
export VERACODE_SA_API_ID="service_account_api_id"
export VERACODE_SA_API_KEY="service_account_api_key"

python script.py --apply \
  --enterprise YOUR-ENTERPRISE \
  --import-repo \
  --set-teams-file out/teams_map.csv \
  --install-app \
  --app-client-id YOUR_APP_CLIENT_ID \
  --set-secrets
```

Per org this will:
1. Create the `veracode` repo if missing and mirror-import from the Veracode template
2. Inject the customized `veracode.yml` onboarding configuration
3. Inject `teams: "..."` into policy and sandbox scan workflow files from `teams_map.csv` (blank rows skipped)
4. Install `veracode-workflow-app` via enterprise API, or fall back to manual install link
5. Create a Veracode workspace, generate a unique agent token, and set `VERACODE_API_ID`, `VERACODE_API_KEY`, and `VERACODE_AGENT_TOKEN` as org-level Actions secrets

#### Auto teams - no CSV needed

If all orgs should use their org name as the team value:

```bash
python script.py --apply --enterprise YOUR-ENTERPRISE --import-repo --set-teams-auto --install-app --app-client-id YOUR_APP_CLIENT_ID --set-secrets
```

#### Teams injection only - repos already exist

```bash
# Per-org teams from CSV:
python script.py --apply --enterprise YOUR-ENTERPRISE --set-teams-file out/teams_map.csv

# Org name as team for all orgs:
python script.py --apply --enterprise YOUR-ENTERPRISE --set-teams-auto
```

Both are idempotent - workflow files that already have a `teams:` parameter are skipped.

---

## Requirements

```bash
pip install requests
pip install pynacl       # required for --set-secrets
git --version            # required for --import-repo
```

**Python 3.8+** required

---

## Credentials

Two separate credential pairs are required when using `--set-secrets`:

| Variable | Purpose |
|----------|---------|
| `VERACODE_API_ID` | Admin credentials - used by the script to call the Veracode API (create workspaces, generate agent tokens) |
| `VERACODE_API_KEY` | Admin credentials |
| `VERACODE_SA_API_ID` | Service account credentials - stored as `VERACODE_API_ID` in each org's GitHub Actions secrets |
| `VERACODE_SA_API_KEY` | Service account credentials - stored as `VERACODE_API_KEY` in each org's GitHub Actions secrets |

The admin credentials are never stored anywhere. The service account credentials are what gets deployed to org secrets and used by workflows at scan time.

---

## GitHub Token Permissions

### Minimum (Dry-Run)
```
read:org
repo
```

### With --import-repo
```
read:org, repo, workflow    # workflow required to push workflow files
```

### With --enterprise
```
read:org, repo, read:enterprise (or admin:enterprise)
```

### With --set-secrets
```
read:org, repo, admin:org
```

### With --install-app
```
admin:enterprise, admin:org, repo, workflow
```

### All features
```
repo, workflow, admin:org, admin:enterprise, read:enterprise
```

**Token security:** `GITHUB_TOKEN` is passed in the git remote URL for import operations. Store it as an environment variable or use a GitHub Actions secret in CI.

---

## Features

### Repository Management
- Creates `veracode` repo in each org if missing
- Mirror-imports content from `github.com/veracode/github-actions-integration` via git CLI
- Injects customized `veracode.yml` onboarding configuration
- Falls back to manual import instructions if git is unavailable

### Team Parameter Injection

**`--set-teams-auto`** - injects `teams: "<org-name>"` for every org. No configuration needed.

**`--set-teams-file FILE`** - injects per-org team values from a CSV. `teams_map.csv` is generated automatically on every dry-run - fill it in and pass it back with `--apply --set-teams-file`. Comma-separated team names are supported. Blank rows are skipped.

Both modes are idempotent - files that already have `teams:` are left unchanged.

### Secrets Management

Sets three org-level GitHub Actions secrets per org:
- `VERACODE_API_ID` - from `VERACODE_SA_API_ID` (service account)
- `VERACODE_API_KEY` - from `VERACODE_SA_API_KEY` (service account)
- `VERACODE_AGENT_TOKEN` - unique per-org, auto-generated from the org's Veracode workspace

---

## Veracode Repository Configuration

The injected `veracode.yml` is pre-configured for onboarding:

```yaml
analysis_on_platform: true
break_build_policy_findings: false   # don't fail pipelines during onboarding
break_build_invalid_policy: false
break_build_on_error: true           # still fail on scan errors
policy: 'Omnicom Base Policy'
issues:
  trigger: true
  commands:
    - "Veracode All Scans"           # added per scan type to trigger all scans via issue comments.
```

Once onboarding is complete, re-enable gating by setting `break_build_policy_findings: true` and `break_build_invalid_policy: true` if desired.

The `teams` parameter is injected into both workflow files:

```yaml
- uses: veracode/uploadandscan-action@v0.1.6
  with:
    teams: "org-name-or-custom-value"
```

---

## Command-Line Flags

### Action Flags (require `--apply`)
| Flag | Description |
|------|-------------|
| `--import-repo` | Create and populate the `veracode` repository |
| `--set-teams-auto` | Inject `teams` parameter using the org name |
| `--set-teams-file FILE` | Read `teams_map.csv` and inject per-org team values into workflow files. `teams_map.csv` is generated automatically on every dry-run. Teams column accepts comma-separated names. |
| `--install-app` | Install `veracode-workflow-app` (enterprise API, falls back to manual) |
| `--set-secrets` | Set `VERACODE_API_ID`, `VERACODE_API_KEY`, `VERACODE_AGENT_TOKEN` in each org |

### Configuration
| Flag | Description |
|------|-------------|
| `--enterprise SLUG` | GitHub Enterprise slug for org discovery |
| `--app-client-id ID` | GitHub App client ID (required for `--install-app`) |
| `--orgs-file FILE` | Text file with one org name per line (alternative to `--enterprise`) |
| `--api-base URL` | GitHub API base URL (GHES: `https://github.company.com/api/v3`) |
| `--web-base URL` | GitHub web base URL (GHES: `https://github.company.com`) |
| `--out DIR` | Output directory (default: `./out`) |
| `--git-timeout SEC` | Timeout in seconds for each git clone/push operation (default: 300) |
| `--skip-to ORG` | Skip all orgs before this one |
| `--continue` | Resume from last checkpoint (`out/checkpoint.json`) |

### Environment Variables
| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Always | GitHub personal access token |
| `VERACODE_API_ID` | `--set-secrets` | Admin API credentials for Veracode API calls |
| `VERACODE_API_KEY` | `--set-secrets` | Admin API credentials for Veracode API calls |
| `VERACODE_SA_API_ID` | `--set-secrets` | Service account credentials stored in org secrets |
| `VERACODE_SA_API_KEY` | `--set-secrets` | Service account credentials stored in org secrets |

---

## Output Files

| File | Description |
|------|-------------|
| `orgs.txt` | Generated on every dry-run - one org per line, ready to pass to `--orgs-file` or trim for a targeted apply |
| `audit_report.json` | Per-org execution report, written incrementally (crash-safe) |
| `teams_map.csv` | Generated on every dry-run - fill in the `teams` column and pass back with `--apply --set-teams-file` |
| `missing_veracode_repo.csv` | Orgs missing the veracode repository |
| `missing_workflow_app.csv` | Orgs missing the workflow app |
| `manual_install_links.csv` | Clickable app install links |

### Audit Report Example

```json
{
  "org": "acme-dev",
  "veracode_repo": {
    "present": true,
    "status": "repo_created_and_imported",
    "import_method": "git_cli_auto",
    "veracode_yml_injected": "created",
    "teams_injection": "teams_added_to_2_files"
  },
  "workflow_app": {
    "installed": true,
    "status": "already_installed",
    "installation_id": 12345678
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

---

## Organization Discovery

1. **`--enterprise SLUG`** - GraphQL enterprise API, requires `read:enterprise` or `admin:enterprise`
2. **No flag** - falls back to `/user/orgs` (all orgs your token can access)
3. **`--orgs-file FILE`** - explicit list, one org per line, `#` for comments

```
# One org name per line
# Lines starting with # are treated as comments

acme-dev
acme-staging
acme-prod
acme-archive
```

---

## Platform Examples

### GitHub Enterprise Cloud (GHEC)

```bash
export GITHUB_TOKEN="..."
export VERACODE_API_ID="..."  VERACODE_API_KEY="..."
export VERACODE_SA_API_ID="..."  VERACODE_SA_API_KEY="..."

python script.py --apply --import-repo --set-teams-file out/teams_map.csv --install-app --set-secrets \
  --enterprise your-enterprise-slug \
  --app-client-id Iv1.xxxxx
```

### GitHub Enterprise Server (GHES)

```bash
export GITHUB_TOKEN="..."
export VERACODE_API_ID="..."  VERACODE_API_KEY="..."
export VERACODE_SA_API_ID="..."  VERACODE_SA_API_KEY="..."

python script.py --apply --import-repo --set-teams-file out/teams_map.csv --set-secrets \
  --enterprise your-enterprise-slug \
  --api-base https://github.company.com/api/v3 \
  --web-base https://github.company.com
```

**Differences from GHEC:**

- **`--enterprise`** - enterprise org discovery via GraphQL works on GHES. Requires `read:enterprise` or `admin:enterprise` scope. You can still use `--orgs-file` as an alternative if preferred.
- **No automated app installation** - `--install-app` relies on the GHEC enterprise API and will not work on GHES. Use `out/manual_install_links.csv` to install the app org by org.
- **Outbound access to GitHub.com required for `--import-repo`** - the Veracode template repository lives at `github.com/veracode/github-actions-integration`. The script clones from there and pushes to your GHES instance. If your GHES environment does not allow outbound connections to GitHub.com, the import step will fail. In that case, mirror the template repo internally first and point the script at your internal copy, or pre-populate the `veracode` repos manually before running without `--import-repo`.
- **Everything else works the same** - teams injection, secrets management, checkpoint/resume, and audit reporting all behave identically.

---

## Console Output

```
============================================================
MODE: APPLY
============================================================
  Import missing repos  : YES
  Set teams in workflows: YES
  Install missing apps  : YES
  Set Veracode secrets  : YES
    Enterprise          : acme-corp
    App Client ID       : Iv1.xxxxx
    VERACODE_API_ID     : SET  (admin - for API calls)
    VERACODE_API_KEY    : SET  (admin - for API calls)
    VERACODE_SA_API_ID  : SET  (service account - stored in orgs)
    VERACODE_SA_API_KEY : SET  (service account - stored in orgs)
============================================================

[teams-map] Loaded 15 org->teams mappings from out/teams_map.csv
[OK] Found 15 orgs via GraphQL

[1/15 (6.7%)] Processing: acme-dev
[acme-dev] Repo: +  App: +  Secrets: + (set 3)

[2/15 (13.3%)] Processing: acme-staging
[acme-staging] Repo: + (teams_added_to_2_files)  App: x  Secrets: + (all exist)
```

**Status indicators:** `+` success / `x` missing or failed / `(teams_added_to_2_files)` injected / `(teams_already_present)` skipped / `(set 3)` new secrets / `(all exist)` no changes needed

---

## Security Notes
- Admin credentials (`VERACODE_API_ID/KEY`) are used only for API calls and never stored
- Service account credentials (`VERACODE_SA_API_ID/KEY`) are encrypted via GitHub's public key API before being set
- Agent tokens are unique per organization
- All credentials passed via environment variables - never hardcode in source
- Default mode is read-only (dry-run); all changes require explicit `--apply`

---

## Support

Supported: GitHub.com · GitHub Enterprise Cloud (GHEC) · GitHub Enterprise Server (GHES) 

For issues provide `out/audit_report.json`, platform type, and command used.

> This is a community tool and is not officially supported by Veracode.
