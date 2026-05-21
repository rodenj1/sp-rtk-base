# Release Process

`sp-rtk-base` is published to [PyPI](https://pypi.org/project/sp-rtk-base/)
automatically by the `.github/workflows/release.yml` workflow whenever a
**GitHub Release** is published.

Authentication uses **PyPI Trusted Publishing (OIDC)** — there is **no
API token** stored in repository secrets.  Every upload is authorized
by a short-lived signed token that GitHub Actions mints at runtime,
scoped to this repository + workflow + environment.

---

## One-time setup

These two steps must be completed **before** the very first release.
After that, every release is fully automated.

### 1. Register the Trusted Publisher on PyPI

PyPI needs to know that this repo + workflow + environment is allowed
to publish `sp-rtk-base`.

1. Sign in to <https://pypi.org>.
2. Go to **Your account → Publishing → Add a new pending publisher**
   (use this for the *first* release, before the project exists on
   PyPI; afterwards use **Manage → Publishing** on the project page).
3. Fill in **exactly**:

   | Field                | Value                          |
   | -------------------- | ------------------------------ |
   | PyPI project name    | `sp-rtk-base`                  |
   | Owner                | `rodenj1`                      |
   | Repository name      | `sp-rtk-base`                  |
   | Workflow filename    | `release.yml`                  |
   | Environment name     | `pypi`                         |

4. Save.  PyPI now trusts uploads from this exact workflow/environment.

The first successful run promotes the pending publisher into a real
publisher and creates the project on PyPI.

### 2. Create the `pypi` GitHub environment

The `publish-pypi` job pins itself to an environment named `pypi` so
that:
- PyPI's trusted publisher rule matches exactly,
- you can optionally add manual-approval / wait-timer / branch
  protection rules later.

1. Repo → **Settings → Environments → New environment**.
2. Name it `pypi` (lowercase, exact).
3. Leave protection rules empty for now (or add yourself as a
   *required reviewer* if you want a manual confirmation gate before
   every publish).

That's it — no secrets to add.

---

## Per-release checklist

For every release (e.g. `0.2.0`, `0.3.0`, …):

1. **Update the version** in `pyproject.toml`:
   ```toml
   [project]
   version = "0.2.0"
   ```
   And in `src/sp_rtk_base/__init__.py`:
   ```python
   __version__ = "0.2.0"
   ```
   (`commitizen` will keep these in sync automatically if you use
   `uv run cz bump`.)

2. **Update `CHANGELOG.md`** with the user-visible changes (or let
   `cz bump` regenerate it from your Conventional Commits).

3. **Commit and push to `main`.**  Wait for the regular `CI` workflow
   to go green on the commit you intend to release.

4. **Tag the commit:**
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
   Tag format must be `vX.Y.Z` (matches the workflow's verifier; the
   leading `v` is stripped before comparing to `pyproject.toml`).

5. **Draft a GitHub Release:**
   - GitHub repo → **Releases → Draft a new release**.
   - Choose tag `v0.2.0`.
   - Title: `v0.2.0`.
   - Click **Generate release notes** (or paste from `CHANGELOG.md`).
   - Leave **"Set as a pre-release"** unchecked (the workflow refuses
     to publish pre-releases to PyPI by design).
   - Click **Publish release**.

6. **Watch the `Release` workflow run** under the **Actions** tab.
   It will:
   1. Verify the tag matches `pyproject.toml`.
   2. Run lint + the full test matrix (Python 3.10 / 3.11 / 3.12 / 3.13).
   3. Build sdist + wheel, validate metadata with `twine check`.
   4. Upload to PyPI via OIDC.
   5. Sign the artifacts with sigstore (PEP 740 attestations).
   6. Attach the artifacts + signatures to the GitHub Release page.

   End-to-end ~5–8 minutes.

7. **Verify:**
   - <https://pypi.org/project/sp-rtk-base/> shows the new version.
   - `pip install --upgrade sp-rtk-base` works.
   - The GitHub Release page now has `.tar.gz`, `.whl`, and
     `.sigstore` attestation files attached.

---

## Troubleshooting

### "Tag 'v0.2.0' (version '0.2.0') does not match pyproject.toml version '0.1.0'"

You tagged before bumping `pyproject.toml`.  Fix:
```bash
# Bump pyproject.toml + __init__.py to 0.2.0, commit, push
git push origin main

# Delete the bad tag locally + remotely
git tag -d v0.2.0
git push --delete origin v0.2.0

# Retag the correct commit
git tag v0.2.0
git push origin v0.2.0

# Then re-publish the GitHub Release (or use workflow_dispatch to
# re-run release.yml with input `tag = v0.2.0`).
```

### "GitHub Release is marked as pre-release"

By design `release.yml` only publishes stable releases to PyPI.  If
you want to publish a pre-release, either:
- uncheck the pre-release box on the GitHub Release and re-publish, or
- (future) extend the workflow to push pre-releases to TestPyPI on a
  separate trigger.

### PyPI upload fails with "no trusted publisher matches"

The PyPI pending-publisher fields must match the workflow *exactly*:
repo, workflow filename, environment name, and ref (PyPI checks that
the workflow is running from the configured repository).  Re-read the
"Register the Trusted Publisher" section and double-check each field.

### Tests fail in the release workflow but passed in CI

CI runs only on push to `main`.  If you cherry-picked, rebased, or
tagged a different commit, the release workflow may be testing a
different tree.  Fix the code on `main`, bump the version (you cannot
re-use a version on PyPI), and retag.

### Need to re-run after a transient failure (e.g. PyPI 5xx)

Use `Actions → Release → Run workflow`, supply the tag (`v0.2.0`).
This re-runs the whole pipeline including the version check.  The
PyPI upload step is idempotent for *new* versions; it will fail if
the version has already been uploaded successfully (PyPI does not
allow re-uploading the same `X.Y.Z`).  In that case, bump to
`X.Y.Z+1` and release again.
