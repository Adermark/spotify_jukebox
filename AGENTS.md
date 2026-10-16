# Agent and contributor rules

## Branches

| Branch | Purpose |
|---|---|
| `main` | Release branch. Only receives merges from `develop` at release time. Every commit on it is a released version. |
| `develop` | Working branch. All feature and fix branches start here and merge back here. |
| `feature/<name>`, `fix/<name>` | Short-lived branches for one change each. |

Never commit directly to `main` or `develop`. Every change to `develop` goes through a pull request.
`main` only changes at release time, see below.

## Day-to-day work

1. Branch off an up-to-date `develop`:
   ```bash
   git switch develop && git pull
   git switch -c feature/<name>    # or fix/<name>
   ```
2. Commit, push and open a pull request **into `develop`**.
3. Merge the PR once tests pass (`pytest`), then delete the branch.

## Releasing

1. Run `pytest` on an up-to-date `develop` and make sure it passes.
2. Merge `develop` into `main`. A pull request is optional here, since every change was already
   reviewed on its way into `develop`. Either open one from `develop` into `main`, or merge
   directly. Always use `--no-ff`, so each release is its own merge commit on `main`:
   ```bash
   git switch main && git pull
   git merge --no-ff develop
   git push
   ```
3. Tag the merge commit on `main` with the new version, using [semantic versioning](https://semver.org)
   (`vMAJOR.MINOR.PATCH`):
   ```bash
   git tag -a v1.2.0 -m "v1.2.0"
   git push origin v1.2.0
   ```
4. Publish a GitHub release for the tag with release notes listing the changes since the previous
   version:
   ```bash
   gh release create v1.2.0 --title "v1.2.0" --generate-notes
   ```
   Edit the generated notes so they describe the changes in plain words for users.
5. Merge `main` back into `develop` so the branches stay in sync:
   ```bash
   git switch develop && git pull
   git merge main
   git push
   ```
