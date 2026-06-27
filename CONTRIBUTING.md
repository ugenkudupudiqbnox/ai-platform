# Contributing

Thanks for your interest in improving the Enterprise AI Platform! This guide
covers how to set up a local environment, the standards we follow, and how to get
changes merged.

## Quick start (contributors)

```bash
# 1. Fork & clone
git clone git@github.com:<your-username>/ai-platform.git
cd ai-platform

# 2. Create a feature branch
git checkout -b feat/short-description

# 3. Install the local validation tools
sudo apt-get update && sudo apt-get install -y shellcheck yamllint jq gettext-base
pipx install yamllint 2>/dev/null || pip install --user yamllint   # if apt's is too old

# 4. Make your change, then validate locally (see "Validation" below)
make validate

# 5. Commit and push
git commit -m "feat: short description"
git push -u origin feat/short-description

# 6. Open a Pull Request against main
```

To actually run the stack while developing, use a throwaway VM/host (Ubuntu
24.04) and `sudo ./install.sh --domain <test-domain> --email you@example.com`.
Never run the installer against a host you can't afford to reset.

## Repository layout

| Path | Purpose |
|------|---------|
| `docker-compose.yml` | The full service topology |
| `install.sh`, `upgrade.sh`, `uninstall.sh`, `healthcheck.sh` | Lifecycle entry points |
| `scripts/` | Shared helpers (`common.sh`), secret generation, certs, backup/restore |
| `docker/<service>/` | Per-service Dockerfiles, entrypoints and config (templates end in `.tmpl`) |
| `monitoring/` | Prometheus, Grafana, OTel and the monitoring compose overlay |
| `docs/` | Operator and architecture documentation |
| `.github/workflows/` | CI: lint, compose validation, security scanning |

## Validation

Every change must pass these before review. `make validate` runs the core set;
the full battery mirrors CI:

```bash
# Shell scripts
shellcheck -x --source-path=SCRIPTDIR install.sh upgrade.sh uninstall.sh \
  healthcheck.sh scripts/*.sh docker/postgres/init/*.sh docker/langflow/*.sh

# Compose (base + monitoring overlay)
docker compose --env-file .env config -q
docker compose -f docker-compose.yml -f monitoring/docker-compose.monitoring.yml config -q

# YAML
yamllint -d "{extends: relaxed, rules: {line-length: disable}}" \
  docker-compose.yml monitoring/ .github/

# JSON (render the realm template first, then validate)
jq empty monitoring/grafana/provisioning/dashboards/platform-overview.json
```

> No `.env`? `cp .env.example .env && sed -i 's/__GENERATED__/placeholder/g' .env`
> for validation only — never commit a populated `.env`.

## Coding standards

- **Shell**: `bash`, `set -euo pipefail`, source `scripts/common.sh` for logging
  and helpers, keep it `shellcheck`-clean (justify any `disable` with a comment).
- **Compose/YAML**: 2-space indent; reuse the `x-logging` / `x-security` /
  `x-restart` anchors; every service needs a healthcheck, `restart` policy and
  `logging` config.
- **Config**: anything host- or secret-specific belongs in `.env` / a `.tmpl`
  rendered at install — never hard-code secrets or domains.
- **Docs**: update the relevant file under `docs/` and any affected tables when
  you change behaviour or add a service.

## Security

- Never commit secrets, a populated `.env`, or rendered `realm.json` /
  `librechat.yaml` (they are git-ignored — keep it that way).
- Report vulnerabilities privately to the maintainers rather than opening a
  public issue.
- New services should run non-root where possible, drop capabilities, mount
  config read-only, and avoid default credentials. See [docs/security.md](docs/security.md).

## Commit & PR conventions

- Use clear, imperative commit subjects, ideally [Conventional Commits]
  (`feat:`, `fix:`, `docs:`, `ci:`, `refactor:`…).
- Keep PRs focused; describe what changed, why, and how you validated it.
- Ensure CI (`build` and `security` workflows) is green before requesting review.

[Conventional Commits]: https://www.conventionalcommits.org/
