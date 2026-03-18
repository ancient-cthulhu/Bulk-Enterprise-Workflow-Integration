# Veracode Bulk GitHub Workflow Integration

Deploys Veracode security scanning across GitHub Enterprise organizations at scale. Handles repository creation, workflow configuration, team assignments, app installation, and secrets management with audit trails and checkpoint/resume support.

---

## How It Works

For each organization, the script can:

1. Create the `veracode` integration repository and mirror-import the Veracode workflow template
2. Inject a customized `veracode.yml` onboarding configuration
3. Inject `teams:` parameter into workflow files
4. Create a Veracode SCA workspace, generate a unique agent token, and set GitHub Actions secrets

All operations are idempotent - safe to re-run.

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

python rollout_helper.py --enterprise YOUR-ENTERPRISE
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

python rollout_helper.py --apply \
  --enterprise YOUR-ENTERPRISE \
  --import-repo \
  --set-teams-file out/teams_map.csv \
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

Two credential pairs are required for `--set-secrets`:

| Variable | Purpose |
|----------|---------|
| `VERACODE_API_ID` | Admin - used by the script to call Veracode APIs (create workspaces, generate tokens) |
| `VERACODE_API_KEY` | Admin |
| `VERACODE_SA_API_ID` | Service account - stored as `VERACODE_API_ID` in each org's Actions secrets |
| `VERACODE_SA_API_KEY` | Service account - stored as `VERACODE_API_KEY` in each org's Actions secrets |

Admin credentials are never stored. Service account credentials are what gets deployed to orgs and used by workflows at scan time.

---

## GitHub Token Permissions

| Operation | Required Scopes |
|-----------|----------------|
| Dry-run | `read:org`, `admin:org` |
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
| `--set-secrets` | Set `VERACODE_API_ID`, `VERACODE_API_KEY`, `VERACODE_AGENT_TOKEN` per org. Always overwrites - safe to re-run for credential rotation. |
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

`teams_map.csv` is generated automatically on every dry-run. Fill in the `teams` column (comma-separated names accepted) and pass it back on apply. Files that already have `teams:` are left unchanged.

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

To push a new `veracode.yml` to all orgs after initial onboarding - for example to update the policy name, change scan triggers, or enable build gating across the fleet:

```bash
# Fetch veracode.yml from the upstream integration repo (default)
python rollout_helper.py --apply --enterprise YOUR-ENTERPRISE --update-veracode-yml

# Use a custom local file instead
python rollout_helper.py --apply --enterprise YOUR-ENTERPRISE --update-veracode-yml /path/to/veracode.yml
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

python rollout_helper.py --apply --enterprise YOUR-ENTERPRISE --set-secrets
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
    "veracode_yml_injected": "created",
    "teams_injection": "teams_added_to_2_files"
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

`veracode_yml_update.action` values: `updated_with_backup`, `created`, `repo_not_found`, `repo_empty`, `put_failed:<status_code>`.

---

## Platform Notes

### GitHub Enterprise Cloud (GHEC)

All features supported.

```bash
python rollout_helper.py --apply --import-repo --set-teams-file out/teams_map.csv \
  --set-secrets \
  --enterprise your-enterprise-slug
```

### GitHub Enterprise Server (GHES)

```bash
python rollout_helper.py --apply --import-repo --set-teams-file out/teams_map.csv --set-secrets \
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
python rollout_helper.py --apply --enterprise YOUR-ENTERPRISE --import-repo --set-secrets

# Resume after interruption
python rollout_helper.py --apply --enterprise YOUR-ENTERPRISE --import-repo --set-secrets --continue
```

Checkpoint state is saved to `out/checkpoint.json` after each org. The `--continue` flag skips the confirmation prompt - confirmation was already given on the initial run.

Use `--skip-to ORG` to jump to a specific org without needing a checkpoint file.

---

## Security Notes

- Veracode admin credentials are used only for API calls and never stored anywhere
- Service account credentials are encrypted via GitHub's public key API before being written to secrets
- Agent tokens are unique per organization and regenerated on each `--set-secrets` run
- All credentials are passed via environment variables and never hardcoded in source
- Default mode is read-only; all changes require explicit `--apply`

---

## Support

Supported platforms: GitHub.com · GitHub Enterprise Cloud · GitHub Enterprise Server

For issues, provide `out/audit_report_<timestamp>.json`, your platform type, and the command used.

> This is a community tool and is not officially supported by Veracode.
