# Releasing

Publishing a GitHub Release publishes both packages. `.github/workflows/release.yml` builds them and
uploads them by **trusted publishing**. PyPI and npm each trust that one workflow file in this
repository and give the job a short-lived OIDC credential. Both uploads carry signed provenance
naming the commit they were built from. No PyPI or npm token is stored anywhere: not in the
repository, not in its secrets, not on a PC.

## One-time setup (the owner, by hand)

**Until all three steps below are done, publishing a release makes the workflow fail without
publishing anything.** The PyPI and npm jobs are refused a credential. The tests and the build
still run, and nothing reaches either registry.

1. **GitHub environment.** On github.com, open the repository, then **Settings → Environments →
   New environment**. Name it `pypi`, then **Configure environment**. Optional: under
   **Deployment branches and tags**, choose **Selected branches and tags** and add the tag rule
   `v*`.
2. **PyPI.** Sign in at pypi.org. Go to **Your projects → tool-guardian → Manage → Publishing**.
   Under **Add a new publisher**, choose **GitHub** and fill in:
   - Owner: `LuminariSoftwares`
   - Repository name: `tool-guardian`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   Then click **Add**.
3. **npm.** Sign in at npmjs.com. Open the package `dsh-tool-guardian`, then **Settings → Trusted
   publishing**. Choose **GitHub Actions** and fill in:
   - Organization or user: `LuminariSoftwares`
   - Repository: `tool-guardian`
   - Workflow filename: `release.yml`
   - Environment: leave empty

   Tick **Allow npm dist-tag**, then save. That box is off by default. Without it, a prerelease
   still publishes under `beta`, but the step that points `latest` at it fails. The fix is then
   to run `npm dist-tag add dsh-tool-guardian@<version> latest` from a logged-in PC, or tick the
   box and re-run the failed job.

Once publishing works, delete the old PyPI API token at **pypi.org → Account settings → API
tokens**. Delete any npm automation token at **npmjs.com → Access Tokens**. Optional: under the npm
package's **Settings → Publishing access**, choose **Require two-factor authentication and
disallow tokens**. Trusted publishing keeps working with that setting.

## Versions

PyPI and npm are independent version lines. For example, PyPI can be at `0.11.0` while npm is at
`0.1.0-beta.1`.

| | Where the version lives | When it is published |
|---|---|---|
| Release tag | `vX.Y.Z` = `v` + `pyproject.toml`'s `version`, exactly as written there (PEP 440, e.g. `v0.11.0`, `v1.0.0rc1`) | — |
| PyPI `tool-guardian` | `pyproject.toml` | when that version is not on PyPI yet |
| npm `dsh-tool-guardian` | `package.json`, unrelated to the tag | only when that exact version is not on npm yet (`npm view dsh-tool-guardian@<version> version` finds nothing); otherwise the npm job is skipped with a notice |

A release where neither version is new fails with "nothing to publish". A PyPI-only patch (bump
`pyproject.toml`, leave `package.json` alone) publishes PyPI and skips npm.

## Every release

1. **Bump the versions in a PR.** Bump `version` in `pyproject.toml`, in `package.json`, or in
   both. Also update the README line naming both versions and `server.json`. Move the CHANGELOG
   `## Unreleased` entries under a heading for the new versions. The `release` workflow runs on
   that PR as a dry run: it checks the versions against both registries, runs the tests, builds,
   runs `twine check` and runs `npm publish --dry-run`.
2. **Merge the PR** once CI is green.
3. **Create the GitHub Release.** On github.com, go to **Releases → Draft a new release**. Under
   **Choose a tag**, type `v` + the `pyproject.toml` version (e.g. `v0.11.0`) and pick **Create
   new tag on publish**, with target `main`.
   Paste the version's CHANGELOG section as the notes. Click **Publish release**.
4. **Watch the workflow.** Under **Actions → release**, the run has four jobs: `verify`, `build`,
   `pypi`, `npm`.
   - `verify` fails unless the tag equals `v` + the `pyproject.toml` version. It also fails if
     neither version is new, or if the tests fail.
   - `pypi` runs only when the PyPI version is new. `npm` runs only when the `package.json`
     version is new.
   - A non-prerelease npm version is published as `latest`.
   - A prerelease is published under `beta`, and then `latest` is moved to it.
5. **Check both registries.**
   - `https://pypi.org/project/tool-guardian/X.Y.Z/` shows the release. Under **Verified
     details**, its files carry an attestation naming this repository and `release.yml`.
   - `npm view dsh-tool-guardian dist-tags` shows the new version on `latest` (and on `beta` for
     a prerelease). The package page on npmjs.com shows a **Provenance** section that links to
     the workflow run.

### Dry run by hand

Go to **Actions → release → Run workflow** and leave **dry_run** ticked. This runs `verify` and
`build` and skips both publishing jobs. To re-publish a release by hand, run the workflow on its
`vX.Y.Z` tag with **dry_run** unticked. The workflow publishes only from a `v*` tag.
