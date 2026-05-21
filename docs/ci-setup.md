# CI / Coverage Setup

The project uses GitHub Actions for CI and [Codecov](https://codecov.io)
for coverage reporting.  Codecov uploads use the **`CODECOV_TOKEN`
repository secret** — this is Codecov's recommended universal path and
works for both public and private repos.  (OIDC tokenless is also
supported but requires explicitly activating the repo on Codecov first,
which is an extra manual step.)

This document explains the workflow design and the one-time manual
setup required to activate the Codecov badge.

## Workflow overview (`.github/workflows/ci.yml`)

Four jobs run on every push/PR to `main`:

| Job | Python | Purpose | Blocking |
|-----|--------|---------|----------|
| **pre-commit (all files)** | 3.12 | runs the full `.pre-commit-config.yaml` hook suite (whitespace, ruff, gitleaks, …) — guards against `git commit --no-verify` | ✅ |
| **Lint & Type Check** | 3.12 | `ruff check` + `ruff format --check` + `mypy --strict` + `pyright` (strict) | ✅ |
| **Test** (matrix) | 3.10 / 3.11 / 3.12 / 3.13 | `uv run pytest tests/unit` with coverage (≥ 90 % gate from `pyproject.toml`) | ✅ |
| **Build distribution** | 3.12 | `uv build` (sdist + wheel) — proves packaging still works | ✅ |

Additional advisory steps:
- `pylint` runs with `continue-on-error: true` — its exit code is
  surfaced as a workflow annotation only.
- Codecov coverage + test-results uploads (Python 3.12 only, via OIDC).

All external action pins use full commit SHAs for supply-chain safety.
Heavy `pre-commit` hooks (`pyright`, `pytest-unit`) are skipped in the
`pre-commit` CI job via `SKIP=pyright,pytest-unit` because the dedicated
`lint` and `test` jobs already cover them more thoroughly.

## Codecov setup (one-time)

### 1. Link the repository at Codecov

1. Go to <https://app.codecov.io/>.
2. Sign in with GitHub.
3. If the repo doesn't appear, click **Resync** at the top of the org
   page; if it's still missing, open **"Codecov's GitHub app"** from
   the banner and make sure `sp-rtk-base` is included under
   "Repository access".
4. Click **Configure** next to `sp-rtk-base` — Codecov's setup page
   will display the repository **upload token** (a UUID).  Copy it.

### 2. Store the upload token as a GitHub Actions secret

```bash
# From the repo root, with gh CLI authenticated:
echo "<paste-token-here>" | gh secret set CODECOV_TOKEN
```

Or in the GitHub UI: **Settings → Secrets and variables → Actions →
New repository secret**, name `CODECOV_TOKEN`, value = the UUID from
step 1.

The workflow references it via `token: ${{ secrets.CODECOV_TOKEN }}`
on both Codecov steps (coverage and test-results).

### 3. Grab the badge token

On the Codecov repo page, open **Settings → Badges & Graphs**.  Copy
the Markdown snippet — it contains a read-only "badge token" in the
URL query string, e.g.:

```markdown
[![codecov](https://codecov.io/gh/rodenj1/sp-rtk-base/branch/main/graph/badge.svg?token=XXXXXXXXXX)](https://codecov.io/gh/rodenj1/sp-rtk-base)
```

Paste that into `README.md`.  The badge token is safe to commit — it
only grants read-only access to the coverage SVG for this branch.

### 4. (Optional) Configure Codecov behaviour

Create `codecov.yml` at the repo root to customise coverage
thresholds, component groupings, PR comments, etc.  Sensible starter:

```yaml
coverage:
  status:
    project:
      default:
        target: auto          # compare against the parent commit
        threshold: 1%         # allow 1 % drops without failing
    patch:
      default:
        target: 90%           # new code should be ≥ 90 % covered
comment:
  layout: "reach, diff, flags, files"
  require_changes: true       # only comment when coverage changed
```

### Switching to OIDC tokenless (optional)

If you prefer OIDC tokenless upload (no `CODECOV_TOKEN` secret to
rotate; **public repos only**):

1. In `ci.yml`, replace `token: ${{ secrets.CODECOV_TOKEN }}` with
   `use_oidc: true` on both Codecov steps.
2. Add `id-token: write` back to the `test` job's `permissions:`
   block.
3. Visit <https://app.codecov.io/gh/rodenj1/sp-rtk-base> at least
   once to activate the repo for Codecov.  Without this, OIDC
   uploads return `{"message":"Repository not found"}`.
4. (Optional) Delete the `CODECOV_TOKEN` secret — no longer needed.

## What the workflow uploads to Codecov

Only the **Python 3.12** matrix leg uploads, to keep the Codecov
dashboard clean.  Two artifacts are sent, both via
`codecov/codecov-action@v6`:

1. **Coverage** (`coverage.xml`) — default `report_type: coverage`.
2. **Test results** (`pytest-junit.xml`) — second invocation with
   `report_type: test_results` for Codecov's flaky-test + failure
   analytics.  (The previously separate `codecov/test-results-action`
   is deprecated in favour of this pattern.)

Both calls use `fail_ci_if_error: false`, so a Codecov outage never
blocks CI.

## Local parity

Run the exact same checks locally before pushing:

```bash
# Lint + format
uv run ruff check .
uv run ruff format --check .

# Strict type checks
uv run mypy src
uv run pyright src

# Full test suite with coverage (90 % gate enforced)
uv run pytest tests/unit

# Pre-commit on every file
uv run pre-commit run --all-files
```

Coverage HTML report: `htmlcov/index.html` (generated by pytest).
