# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Commit messages follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/);
the changelog can be regenerated automatically via `uv run cz bump`.

## [Unreleased]

### Added
- **CI / Release pipeline**:
  - `.github/workflows/ci.yml` — pre-commit, lint (ruff + ruff-format +
    mypy + pyright), test matrix (Python 3.10 / 3.11 / 3.12 / 3.13),
    Codecov uploads via OIDC (tokenless, public-repo), and packaging
    sanity build (`uv build`).
  - `.github/workflows/release.yml` — version verification, full
    lint + test gates, sdist + wheel build, PyPI publish via Trusted
    Publishing (OIDC, environment `pypi`), sigstore signing, and
    automatic GitHub Release asset attachment.
- **Pre-commit suite** (`.pre-commit-config.yaml`): trailing
  whitespace / EOF / YAML / TOML / large-file hooks, ruff lint +
  format, gitleaks, commitizen on `commit-msg`, and pre-push gates
  for pyright (strict) + pytest unit suite.
- **Gitleaks configuration** (`.gitleaks.toml`) — custom rules for
  the repository.
- **Commitizen configuration** in `pyproject.toml` — Conventional
  Commits 1.0.0 enforcement with `cz_customize` extending the
  Angular type list with `release` and `security`.
- **README badges** for CI status, Codecov coverage, PyPI version,
  Python versions, license, ruff, and Conventional Commits.
- **CI / Release documentation**:
  - `docs/ci-setup.md` — workflow design + Codecov setup runbook.
  - `docs/release-process.md` — per-release checklist + Trusted
    Publishing setup.
- **Project metadata** in `pyproject.toml` — added `keywords`,
  `classifiers`, `urls`, and improved `description` for PyPI.

### Changed
- **Tooling**: dropped `black` in favour of `ruff format`; bumped
  ruff lint ruleset to include `B`, `UP`, `N`, `SIM`, `RUF`.
- **Type checking**: pyright remains the canonical strict checker;
  mypy now also runs in strict mode with project-specific overrides
  for NiceGUI's dynamic UI types and pyright-only `# type: ignore`
  comments (`warn_unused_ignores = false`).
- **Coverage gate** raised to `--cov-fail-under=90` in
  `pyproject.toml`.  NiceGUI UI pages and the `cli/config_audit.py`
  CLI are excluded from coverage as they can't be meaningfully
  unit-tested.

### Security
- Initial gitleaks sweep of the working tree and full `git log -p`
  history — no live credentials remained at the time of this entry.
- All third-party GitHub Actions are pinned to **full commit SHAs**
  in both workflows for supply-chain safety.
- PyPI uploads use OIDC Trusted Publishing — no API tokens are
  stored as repository secrets.

## [0.1.0] — initial development version

Baseline release; not yet published to PyPI.
