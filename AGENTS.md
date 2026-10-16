# Agent and contributor rules

The commands below are the same on Windows and macOS. Run them one line at a time: chaining with
`&&` doesn't work in Windows PowerShell 5.1. Anything that differs between platforms, such as how
to run the project's Python, is in [README.md](README.md#development).

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
   git switch develop
   git pull
   git switch -c feature/<name>    # or fix/<name>
   ```
2. Commit, push and open a pull request **into `develop`**.
3. Merge the PR once the tests pass, then delete the branch.

## Releasing

1. Switch to an up-to-date `develop` and run the tests with the project's Python (see
   [Development](README.md#development)). They must pass.
2. Merge `develop` into `main`. A pull request is optional here, since every change was already
   reviewed on its way into `develop`. Either open one from `develop` into `main`, or merge
   directly. Always use `--no-ff`, so each release is its own merge commit on `main`:
   ```bash
   git switch main
   git pull
   git merge --no-ff develop
   git push
   ```
3. Tag the merge commit on `main` with the new version, using [semantic versioning](https://semver.org)
   (`vMAJOR.MINOR.PATCH`):
   ```bash
   git tag -a v1.2.0 -m "v1.2.0"
   git push origin v1.2.0
   ```
4. Build the zip by running `make_dist.py` with the project's Python. It prints the path of the zip
   it built, e.g. `dist/jukebox-2026-10-16.zip`.
5. Publish a GitHub release for the tag with the zip attached and release notes listing the
   changes since the previous version. Use the path that `make_dist.py` printed:
   ```bash
   gh release create v1.2.0 dist/jukebox-2026-10-16.zip --title "v1.2.0" --generate-notes
   ```
   Edit the generated notes so they describe the changes in plain words for users.
6. Merge `main` back into `develop` so the branches stay in sync:
   ```bash
   git switch develop
   git pull
   git merge main
   git push
   ```
