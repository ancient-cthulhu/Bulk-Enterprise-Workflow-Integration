# Veracode Bulk GitHub Workflow Integration

Automated script to deploy Veracode GitHub Workflow integration across multiple GitHub organizations.

## Overview

Automates Veracode security scanning deployment across GitHub Enterprise organizations. Handles repository creation, workflow configuration, team assignments, app installation, and secrets management with detailed audit trails.

**Key capabilities:**
- GraphQL-based enterprise organization discovery
- Automatic repository import via git CLI
- Team parameter injection into workflow files
- Veracode workspace and GitHub Actions secrets automation
- Idempotent operations (safe to re-run)
- Comprehensive JSON audit reports
- Automatic rate limit handling with retry logic
- Checkpoint/resume for large deployments (100+ orgs)
- Progress tracking with execution summaries

## What It Does

For each GitHub organization:
1. Check/Create the `veracode` integration repository
2. Auto-import repository content from Veracode's template (via git CLI)
3. Inject customized `veracode.yml` with onboarding settings
4. Update workflow files with organization team name (via `--set-teams`)
5. Check/Install the `veracode-workflow-app` GitHub App
6. Set up secrets - Creates Veracode workspace, generates agent tokens, and configures GitHub Actions secrets

## Modes

- **DRY-RUN** (default): Read-only audit - reports status without changes
- **APPLY**: Makes changes when explicitly enabled with action flags

## Requirements

```bash
# Core dependencies
pip install requests

# For secrets management
pip install pynacl

# For auto-import (optional)
git --version  # Must be available in PATH
```

**Python 3.8+** required

## Quick Start

### 1. Audit Only (Safe - No Changes)
```bash
# Set GitHub token
export GITHUB_TOKEN="your_github_token"

python script.py --enterprise YOUR-ENTERPRISE
```

### 2. Create Repos + Auto-Import + Inject Teams
```bash
python script.py --apply --import-repo --set-teams --enterprise YOUR-ENTERPRISE
```

### 3. Full Automation (Repos + Teams + App + Secrets)

**Linux/Mac:**
```bash
export GITHUB_TOKEN="your_github_token"
export VERACODE_API_ID="your_veracode_api_id"
export VERACODE_API_KEY="your_veracode_api_key"

python script.py --apply --import-repo --set-teams --install-app --set-secrets \
  --enterprise YOUR-ENTERPRISE \
  --app-client-id YOUR_APP_CLIENT_ID
```

**Windows (PowerShell):**
```powershell
$env:GITHUB_TOKEN="your_github_token"
$env:VERACODE_API_ID="your_veracode_api_id"
$env:VERACODE_API_KEY="your_veracode_api_key"

python script.py --apply --import-repo --set-teams --install-app --set-secrets --enterprise YOUR-ENTERPRISE --app-client-id YOUR_APP_CLIENT_ID
```

**Windows (CMD):**
```cmd
set GITHUB_TOKEN=your_github_token
set VERACODE_API_ID=your_veracode_api_id
set VERACODE_API_KEY=your_veracode_api_key

python script.py --apply --import-repo --set-teams --install-app --set-secrets --enterprise YOUR-ENTERPRISE --app-client-id YOUR_APP_CLIENT_ID
```

## GitHub Token Permissions

### Minimum (Audit Mode)
```
read:org    # List organizations and check app installations
repo        # Access private repositories
```

### For Apply Mode with Repo Import (--import-repo)
```
read:org
repo
workflow    # REQUIRED - Push workflow files to repositories
```
**Critical:** The `workflow` scope is required when using `--import-repo` because the Veracode template includes workflow files. Without this scope, git push operations will be rejected.
 the
### For Enterprise Organization Discovery (--enterprise flag)
```
read:org
repo
workflow    # Required if also using --import-repo
read:enterprise  # OR admin:enterprise - Required to list orgs in enterprise
```
**Important:** When using `--enterprise` to discover organizations, your token MUST have `read:enterprise` or `admin:enterprise` scope. If these scopes don't appear in your token (check via API), the enterprise REST API will return 404. GraphQL will still work as a fallback.

### For Apply Mode with Secrets
```
read:org
repo
workflow    # Required if also using --import-repo
admin:org   # Set organization-level secrets
```

### For Enterprise App Installation (--install-app)
```
admin:enterprise  # Enterprise-wide app installation and org discovery
admin:org         # Organization management
repo              # Repository access
workflow          # Required if also using --import-repo
```

### Complete Token Scopes (All Features)
```
repo              # Full repository access
workflow          # Push workflow files
admin:org         # Manage organization secrets
admin:enterprise  # Enterprise management and org discovery
read:enterprise   # Read enterprise data (if available)
```

## Features

### Repository Management
- Creates `veracode` repo in each org if missing
- Automatically imports content from `github.com/veracode/github-actions-integration`
- Uses git CLI for fast, reliable mirroring
- Falls back to manual instructions if git unavailable
- **Automatically pushes customized `veracode.yml` configuration**
- Adds the **teams** option to the *veracode-policy-scan.yml* and *veracode-sandbox-scan.yml* in *veracode/.github
/workflows/*

### App Installation
- **Automated** (Enterprise only): Installs app via Enterprise API
- **Manual**: Generates clickable install links in CSV
- Pre-fills org information for one-click installation

### Secrets Management (NEW)
Automatically sets up three organization-level GitHub Actions secrets:

1. **VERACODE_API_ID** - Your Veracode API credentials
2. **VERACODE_API_KEY** - Your Veracode API credentials  
3. **VERACODE_AGENT_TOKEN** - Unique per-org agent token (auto-generated)

**How it works:**
- Creates a Veracode workspace per org (or uses existing)
- Generates a unique agent token for each workspace
- Encrypts and sets secrets via GitHub API
- Secrets are available to all workflows in the org

**Setup:**
```bash
# Set credentials
export VERACODE_API_ID="your_veracode_api_id"
export VERACODE_API_KEY="your_veracode_api_key"

# Run with --set-secrets
python script.py --apply --set-secrets
```

---

## Veracode Repository Configuration

When the script imports the `veracode` repository, it automatically injects a customized `veracode.yml` configuration file optimized for onboarding.

### Configuration Changes Applied

The injected `veracode.yml` includes the following onboarding-friendly settings:

#### 1. Platform Reporting Enabled
```yaml
analysis_on_platform: true
```
Ensures all scan results are sent to the Veracode Platform for centralized visibility.

#### 2. Break-Build Settings Disabled (Onboarding Mode)
```yaml
break_build_policy_findings: false
break_build_invalid_policy: false
break_build_on_error: true  # Only fails on scan errors, not policy violations
```
**Why:** Prevents pipelines from failing during initial testing and onboarding. Teams can verify scans work without disrupting development.

#### 3. Unified Issue-Based Trigger
```yaml
issues:
  trigger: true
  commands:
    - "Veracode SAST Scan"
    - "Veracode SCA Scan"
    - "Veracode IAC Scan"
    - "Veracode All Scans"
```
**Usage:** Comment on any GitHub issue with `Veracode All Scans [branch: main]` to trigger all scan types with a single command.

#### 4. Omnicom Base Policy Applied
```yaml
policy: 'Omnicom Base Policy'
```
Aligns all scans with the organization's baseline security policy.

### Complete Configuration Template

The injected `veracode.yml` includes configurations for three scan types:

1. **Static Analysis (SAST)** - `veracode_static_scan`
2. **Software Composition Analysis (SCA)** - `veracode_sca_scan`
3. **Infrastructure as Code & Secrets** - `veracode_iac_secrets_scan`

Each scan is configured to:
- Trigger on push to any branch
- Trigger on pull requests to default branch
- Report to Veracode Platform
- Allow manual triggering via GitHub Issues
- **Not break builds** during onboarding

### Post-Onboarding: Re-Enable Gating

After testing and verification, teams can enable break-build behavior by editing their `veracode.yml`:

```yaml
break_build_policy_findings: true
break_build_invalid_policy: true
break_build_on_error: true
```

### Repository List Control

The imported repo also includes `repo-list.yml` to control which repositories are scanned:

```yaml
# Include all repos by default
include_repos:
  - '*'

# Exclude specific repos if needed
exclude_repos:
  - 'veracode'
  # - 'archived-project'
```

This allows organizations to selectively enable scanning as teams onboard.

### Workflow File Updates

The script also automatically updates the Veracode workflow files to include the organization name as the team parameter:

**Files updated:**
- `.github/workflows/veracode-sandbox-scan.yml`
- `.github/workflows/veracode-policy-scan.yml`

**Change applied:**
```yaml
# Added to the Veracode Upload and Scan Action step
- name: Veracode Upload and Scan Action Step
  uses: veracode/uploadandscan-action@v0.1.6
  with:
    appname: ${{ inputs.profile_name }}
    # ... other parameters ...
    failbuild: ${{ inputs.break_build_policy_findings }}
    team: 'organization-name'  # <- Automatically added
```

**Why this matters:**
- Maps scan results to the correct Veracode workspace
- Uses the same team name as the Veracode workspace created by `--set-secrets`
- Ensures proper organization and reporting in the Veracode Platform

**Team name = Organization name = Workspace name**

This ensures all Veracode components (workspace, secrets, scan results) are properly linked to the GitHub organization.

---

## Command-Line Flags

### Action Flags (Require `--apply`)
| Flag | Description |
|------|-------------|
| `--import-repo` | Create and populate the veracode repository with template content |
| `--set-teams` | Inject team parameter into workflow files (uses org name as team) |
| `--install-app` | Attempt automated app installation (Enterprise API, may require manual) |
| `--set-secrets` | Configure Veracode secrets automatically (VERACODE_API_ID, VERACODE_API_KEY, VERACODE_AGENT_TOKEN) |

**Note:** `--set-teams` is typically used with `--import-repo` to inject the team parameter immediately after import.

### Configuration
| Flag | Description |
|------|-------------|
| `--enterprise SLUG` | GitHub Enterprise slug for org discovery (uses GraphQL API) |
| `--app-client-id ID` | GitHub App client ID (required for `--install-app`) |
| `--orgs-file FILE` | Text file with org names (one per line, alternative to --enterprise) |
| `--api-base URL` | GitHub API base (for GHES: `https://github.company.com/api/v3`) |
| `--web-base URL` | GitHub web base (for GHES: `https://github.company.com`) |
| `--skip-to ORG` | Skip to this organization name and continue from there |
| `--continue` | Resume from last checkpoint (out/checkpoint.json) |

### Environment Variables
| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub personal access token (required) |
| `VERACODE_API_ID` | Veracode API ID (required for `--set-secrets`) |
| `VERACODE_API_KEY` | Veracode API key (required for `--set-secrets`) |

## Output Files

All files created in `out/` directory:

| File | Description |
|------|-------------|
| `audit_report.json` | Complete execution report with all details |
| `missing_veracode_repo.csv` | Orgs missing the veracode repository |
| `missing_workflow_app.csv` | Orgs missing the workflow app |
| `manual_install_links.csv` | **Clickable install links** (open in Excel) |

### Audit Report Tracking

The `audit_report.json` file provides detailed tracking of all changes made to each organization:

**Example entry:**
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

**Tracked fields:**

**Repository:**
- `status`: `repo_exists`, `repo_created_and_imported`, `missing`, `error`
- `import_method`: `git_cli_auto`, `manual`
- `teams_injection`: `teams_added_to_2_files`, `teams_already_present`, `error`

**veracode.yml:**
- `veracode_yml_injected`: `created`, `updated_with_backup`, `already_exists`, `template_not_found`, `failed`

**Secrets:**
- Per secret status: `set`, `exists`, `failed`

**App:**
- `status`: `already_installed`, `installed_after_attempt`, `missing`
- `reason`: `auto_install_blocked`, `manual_install_required`

This detailed tracking enables:
- Auditing changes made to each organization
- Identifying organizations requiring manual intervention
- Safe re-runs without duplicating changes
- Compliance reporting
- Team parameter injection tracking

## Organization Discovery

The script discovers organizations using a GraphQL-first approach:

1. **Enterprise GraphQL API** (if `--enterprise` provided) - Primary method
   - Requires `read:enterprise` or `admin:enterprise` scope
   
2. **User API** (fallback when no `--enterprise` flag)
   - Lists all orgs your token has access to
   - Not filtered by enterprise
   - Requires `read:org` scope
   
3. **File** (if `--orgs-file` provided)
   - Manual org list from text file
   - One org name per line
   - Comments start with `#`

### Common Issues

**Enterprise API returns 0 organizations:**
- Verify enterprise slug is correct: `https://github.com/enterprises/YOUR-ENTERPRISE`
- Token must have `read:enterprise` or `admin:enterprise` scope
- Confirm you have access to the enterprise

**Authentication errors:**
- Verify token is set: `echo $GITHUB_TOKEN`
- Check token scopes at: https://github.com/settings/tokens
- Required scopes: `read:org`, `read:enterprise` (for --enterprise flag)

## GitHub Enterprise Cloud (GHEC)

```bash
export VERACODE_API_ID="your_api_id"
export VERACODE_API_KEY="your_api_key"

python script.py --apply --import-repo --install-app --set-secrets \
  --enterprise your-enterprise-slug \
  --app-client-id Iv1.xxxxx
```

## GitHub Enterprise Server (GHES)

```bash
export VERACODE_API_ID="your_api_id"
export VERACODE_API_KEY="your_api_key"

python script.py --apply --import-repo --set-secrets \
  --api-base https://github.company.com/api/v3 \
  --web-base https://github.company.com
```

## Console Output

```
============================================================
MODE: APPLY
============================================================
  Import missing repos: YES
  Set teams in workflows: YES
  Install missing apps: YES
  Set Veracode secrets: YES
    Enterprise: acme-corp
    App Client ID: Iv1.xxxxx
    Veracode API ID: SET
    Veracode API Key: SET
============================================================

Attempting to discover orgs via enterprise GraphQL API: enterprise(slug: "acme-corp")
[OK] Found 15 orgs via GraphQL API

[acme-dev] Repo: ✓  App: ✓  Secrets: ✓ (set 3)
[acme-staging] Repo: ✓ (teams_added_to_2_files)  App: ✗  Secrets: ✓ (all exist)
[acme-prod] Repo: ✓  App: ✓  Secrets: ✓ (set 2, existed 1)

Outputs written to: C:\Users\...\omnicom-gh-onboarder\out
 - audit_report.json
 - missing_veracode_repo.csv
 - missing_workflow_app.csv
 - manual_install_links.csv
```

### Status Indicators
- `✓` Present/Success
- `✗` Missing/Failed
- `(teams_added_to_2_files)` Teams parameter injected into workflow files
- `(all exist)` Secrets already existed, no changes needed
- `(set 3)` Three new secrets created
- `(set 2, existed 1)` Two secrets created, one already existed

## Manual App Installation

If automated installation fails or isn't available:

1. Open `out/manual_install_links.csv` in Excel
2. Click the install link for each org
3. Approve the installation
4. Done!

Links are pre-filled with org information for one-click installation.

## Security Notes
- Secrets are encrypted using GitHub's public key API
- Agent tokens are unique per organization
- No secrets are logged or stored locally
- Default mode is read-only (dry-run)
- All changes require explicit `--apply` flag

## Support

Supported platforms:
- GitHub.com
- GitHub Enterprise Cloud (GHEC)
- GitHub Enterprise Server (GHES)
- Enterprise Managed Users (EMU)

For issues, provide:
- `out/audit_report.json`
- Platform type (GHEC/GHES/EMU)
- Command used

Note: This is a community tool and is not officially supported by Veracode.
