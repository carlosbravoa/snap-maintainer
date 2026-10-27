# snap-maintainer

Keeps track of the snaps you publish and (eventually) rebuilds and releases them
when their upstream changes.

## Milestones

1. **Discovery** (done): list your registered snaps, their channel maps, and their
   source repos, saved in `inventory.json`.
2. **Update detection** (done): decide whether each snap needs a rebuild or a new
   release (`check`).
3. **Rebuild and release**: run a rebuild and release when you ask for it.
   - 3a (done): detect how each snap is built (`Builds via` in `discover`/`show`).
   - 3b: wait for the new revision and release it, the same for every builder.
   - 3c: trigger builds, one builder at a time.

## Usage

Requires `snapcraft` (logged in: `snapcraft login`) and `gh` (logged in: `gh auth login`).
`unsquashfs` (squashfs-tools) and `git` are optional: without them, `check` falls back to
inferred security results and skips upstream tag comparisons.

`discover`, `check` and `set-repo` check these first, and stop with the exact command to fix
anything missing. Exit codes: `0` success, `1` error, `2` a required tool or login is
missing, `130` interrupted. `--debug` shows a traceback for unexpected errors.

Output uses colours, a live progress bar and clickable links on a terminal, and plain text
when piped or when `NO_COLOR` is set.

```sh
pipx install --editable .              # or: PYTHONPATH=src python3 -m snap_maintainer ...
snap-maintainer discover               # all snaps; prompts for repos it can't find
snap-maintainer discover yatta paint98 # just these snaps
snap-maintainer discover --no-input    # never prompt (for cron/CI)
snap-maintainer set-repo <snap> <url>  # pin a repo by hand; discovery never overwrites it
snap-maintainer set-build <snap> <method> [--workflow FILE]  # pin how a snap gets rebuilt
snap-maintainer show [--json]
snap-maintainer check [snaps...] [-v]  # what needs a rebuild/release, and why
snap-maintainer check --refresh        # ignore today's cached archive/USN data
```

## How a repo is found

Candidates are tried in this order, and the first one that resolves is used:

1. A repo you set with `set-repo` or entered at the prompt.
2. The store's `links.source`.
3. GitHub URLs among the store's website, issues and contact links.
4. `github.com/<your gh login>/<snap name>`. This catches private snaps, which the
   public store API can't see.
5. An interactive prompt, when running on a TTY.

On GitHub, the tool checks that the repo exists (following renames, which are
recorded as `redirected_from`), records its default branch, finds `snapcraft.yaml`,
and lists its workflows. Repos on other hosts are recorded but not checked yet.

`status` is one of `ok`, `no-snapcraft-yaml`, `repo-unreachable`, `needs-repo` or
`unregistered`.

## How the builder is detected

`discover` records how each snap gets built, under `build` in `inventory.json`:

| Builder | Detected when | Preference |
|---|---|---|
| `github-workflow` | A workflow in `.github/workflows/` builds a snap (`snapcore/action-build`, or `snapcraft` / `snapcraft pack` in a `run:` step). It's recorded whether it also uploads (`snapcraft upload` or `snapcore/action-publish`), how it's triggered (tags, dispatch, release, push), and any channel it hard-codes. | 1st if it uploads, 3rd if it only builds |
| `launchpad` | A Launchpad snap recipe builds from the repo for this snap name, found with one `findByURLPrefix` query per GitHub owner. | 2nd if it uploads, 4th if not |
| `local` | Nothing above: build with snapcraft on this machine. | fallback |
| `manual` | Only set by you: rebuilds happen outside this tool. | — |

Other candidates are kept under `alternatives`. `set-build` overrides the detection, and
discovery keeps your choice; for `github-workflow` it defaults to the detected workflow
that uploads. `set-repo` re-runs detection unless you've set the builder yourself.

## How `check` decides

`check` looks at the most stable published revision per architecture on the
`latest` track, and compares it with:

| Signal | Verdict | How it's detected |
|---|---|---|
| security | `rebuild` | A USN fixes a package the snap stages, and the fixed version is in the public archive. If the published revision has a build manifest, its exact staged versions are compared with the fixed version (`[manifest]`). Otherwise the USN counts if it was issued after the upload (`[inferred]`), using the `stage-packages` for that arch plus their Depends closure, minus what the base snap ships. |
| source | `update-available` | Commits on the repo's default branch since the upload, or `version:` in `snapcraft.yaml` differs from the published one. |
| upstream | `update-available` | A part with a remote git source pins a `source-tag` that has newer releases, or tracks a branch (GitHub only) with new commits. |

Otherwise the verdict is `up-to-date` (or `unknown` / `unpublished`). Results are
stored under `update_check` in `inventory.json`.

Limitations:

- Exact security checks need `snap/manifest.yaml` in the published snap, which
  snapcraft writes only when building with `SNAPCRAFT_BUILD_INFO=1` (Launchpad and
  `snapcore/action-build` set it by default). Each public revision is downloaded once
  and its manifest cached in `~/.cache/snap-maintainer` permanently. Private snaps
  can't be downloaded, so they're always inferred.
- `check` refreshes each snap's channel map from the store, so it evaluates what's
  published now, not what was published at discovery time.
- USN fixes that exist only in Ubuntu Pro (ESM) are listed as notes, since a
  rebuild can't pick them up.
- Non-security archive updates, and dependencies bundled by language plugins
  (pip, npm, cargo, go), aren't checked.
- The base snap's packages are read from `/snap/<base>/current`. If that base
  isn't installed locally, results may include packages the base already ships.
- Ubuntu archive and USN data are cached for 24h in `~/.cache/snap-maintainer`,
  roughly 20 MB per series and architecture.
