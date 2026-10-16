# Agent and contributor rules

## Branches

| Branch | Purpose |
|---|---|
| `main` | Release branch. Only receives merges from `develop` at release time. Every commit on it is a released version. |
| `develop` | Working branch. All feature and fix branches start here and merge back here. |
| `feature/<name>`, `fix/<name>` | Short-lived branches for one change each. |

Never commit directly to `main` or `develop`. Every change goes through a pull request.

## Day-to-day work

1. Branch off an up-to-date `develop`:
   ```bash
   git switch develop && git pull
   git switch -c feature/<name>    # or fix/<name>
   ```
2. Commit, push and open a pull request **into `develop`**.
3. Merge the PR once tests pass (`pytest`), then delete the branch.

## Releasing

1. Open a pull request from `develop` **into `main`** and merge it.
2. Tag the merge commit on `main` with the new version, using [semantic versioning](https://semver.org)
   (`vMAJOR.MINOR.PATCH`):
   ```bash
   git switch main && git pull
   git tag -a v1.2.0 -m "v1.2.0"
   git push origin v1.2.0
   ```
3. Publish a GitHub release for the tag with release notes listing the changes since the previous
   version:
   ```bash
   gh release create v1.2.0 --title "v1.2.0" --generate-notes
   ```
   Edit the generated notes so they describe the changes in plain words for users.
4. Merge `main` back into `develop` so the branches stay in sync:
   ```bash
   git switch develop && git pull
   git merge main
   git push
   ```
