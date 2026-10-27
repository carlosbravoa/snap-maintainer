# snap-maintainer

[![PyPI](https://img.shields.io/pypi/v/snap-maintainer)](https://pypi.org/project/snap-maintainer/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/carlosbravoa/snap-maintainer/blob/master/LICENSE)

Find out which of your published snaps need a rebuild or a new release, and why.

`snap-maintainer` reads your Snap Store account, finds the source repository behind
each snap, and checks it against three things:

- **Security fixes.** Ubuntu has fixed a package your snap stages, and a rebuild
  would pick up the fix.
- **Unreleased changes.** Your repo has commits, or a version bump, that aren't in
  the published revision.
- **Upstream releases.** A part pulls from a git repo that has moved on.

For every snap that needs work, it writes out the exact steps to rebuild and release
it, matched to how that snap is built: a GitHub Actions workflow, a Launchpad recipe,
or a local `snapcraft pack`.

It never builds, uploads or releases anything itself. Those steps need someone
watching, so the tool stops at a plan that a person, or an agent, can follow. Agents
can also use it directly through its built-in [MCP server](#use-it-from-an-agent-mcp).

## Requirements

- Python 3.10 or later
- [`snapcraft`](https://snapcraft.io/snapcraft), logged in to the Snap Store as the
  snaps' publisher: `snapcraft login`
- The [GitHub CLI](https://cli.github.com/) `gh`, logged in: `gh auth login`
- Optional:
  - `unsquashfs` (package `squashfs-tools`): reads build manifests from published
    snaps, for exact security checks.
  - `git`: compares pinned `source-tag`s with upstream tags.
  - The `mcp` Python package: only for the MCP server (`snap-maintainer mcp`).

The tool checks all of these when it starts, and tells you the command to fix anything
that's missing.

## Install

From [PyPI](https://pypi.org/project/snap-maintainer/), with pip or [pipx](https://pipx.pypa.io/):

```sh
pip install snap-maintainer        # or: pipx install snap-maintainer
```

To use it from an agent, install the MCP extra instead:

```sh
pip install 'snap-maintainer[mcp]'  # or: pipx install 'snap-maintainer[mcp]'
```

The latest development version comes straight from GitHub:

```sh
pipx install git+https://github.com/carlosbravoa/snap-maintainer.git
```

From a checkout, for development:

```sh
git clone https://github.com/carlosbravoa/snap-maintainer.git
cd snap-maintainer
pipx install --editable .
```

You can also run it without installing, from the checkout:
`PYTHONPATH=src python3 -m snap_maintainer <command>`.

## Quick start

```sh
mkdir ~/snaps && cd ~/snaps    # the inventory is saved in the current directory
snap-maintainer discover       # find your snaps, their repos and how they're built
snap-maintainer check          # decide which need a rebuild or release
snap-maintainer plan           # the steps to rebuild and release each of them
```

The first `check` downloads Ubuntu archive and security data and the published snaps'
manifests, so it takes a minute or two. Later runs use a cache.

## Commands

| Command | What it does |
|---|---|
| `discover [snaps...]` | Lists the snaps registered to your account, their channels, source repos and builders, and saves them to `inventory.json`. Prompts for a repo it can't find (`--no-input` to skip). |
| `check [snaps...]` | Gives each snap a verdict, `rebuild`, `update` or `up to date`, with the reasons. `-v` shows every notice, commit and note; `--refresh` re-downloads cached Ubuntu data. |
| `plan [snaps...]` | Shows the steps to rebuild and release every snap that needs it, or just the named ones. `--all` includes up-to-date snaps; `--markdown` and `--json` print it for an issue, an agent or a script. Runs nothing. |
| `show [--json]` | Prints the inventory without contacting anything. |
| `set-repo <snap> <url>` | Sets a snap's source repo by hand. |
| `set-build <snap> <method> [--workflow FILE]` | Sets how a snap is built: `github-workflow`, `launchpad`, `local` or `manual`. |
| `mcp` | Runs an MCP server so an agent can use the tool directly. See [Use it from an agent (MCP)](#use-it-from-an-agent-mcp). |

Repos and builders you set by hand are never overwritten by `discover`.

Global options go before the command: `-i FILE` uses another inventory file, and
`--debug` shows a traceback for unexpected errors. The `SNAP_MAINTAINER_INVENTORY`
environment variable sets the inventory file too, which is handy for always using the
same one.

Exit codes are `0` for success, `1` for an error, `2` when a required tool or login is
missing, and `130` when interrupted.

Output uses colours, a progress bar and clickable links in a terminal, and plain text
when piped or when `NO_COLOR` is set.

### Handing a plan to an agent

`plan --markdown` prints a self-contained checklist with the reasons, the target
channel and architectures, and copy-paste commands:

```sh
snap-maintainer plan --markdown > rebuild-plan.md
snap-maintainer plan --markdown hello-nginx | gh issue create -R you/hello-nginx \
  --title "Rebuild for security fixes" --body-file -
```

## Use it from an agent (MCP)

`snap-maintainer mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io/)
server over stdio, so an agent such as Claude Code can discover, check and plan rebuilds
directly. It needs the optional `mcp` package:

```sh
pip install 'snap-maintainer[mcp]'   # or, if already installed with pipx: pipx inject snap-maintainer mcp
```

Add it to Claude Code (`--scope user` makes it available in every project):

```sh
claude mcp add --scope user snap-maintainer -- snap-maintainer mcp
```

For other MCP clients, add this to their server configuration:

```json
{
  "mcpServers": {
    "snap-maintainer": { "command": "snap-maintainer", "args": ["mcp"] }
  }
}
```

The server provides these tools:

| Tool | What it does |
|---|---|
| `get_inventory` | Known snaps, their repos, builders and last verdicts. Read-only and fast. |
| `discover_snaps` | Same as `discover`. |
| `check_snaps` | Same as `check`, with progress updates. |
| `rebuild_plan` | Same as `plan`, as Markdown or JSON. Read-only. |
| `set_repo`, `set_build` | Same as the commands. |

It also provides a `maintain_snaps` prompt that walks through the whole flow.

Like the command line, the server never builds, uploads or releases anything. Its
instructions tell the agent to show you the plan, and to ask before running any upload,
release, tag push or workflow, one snap at a time.

The server keeps its inventory in `~/.local/share/snap-maintainer/inventory.json`. To
share one inventory with the command line, set `SNAP_MAINTAINER_INVENTORY` for both, or
pass `-i FILE` before `mcp`.

If `check_snaps` or `discover_snaps` says it can't reach the keyring, the MCP client
started the server without your desktop session's `DBUS_SESSION_BUS_ADDRESS`, so
snapcraft can't read your login. Either pass that variable through in the client's
server configuration, or give the server an exported login instead:

```sh
(umask 077; snapcraft export-login - > ~/.config/snap-maintainer-login)   # readable only by you
```

Then set `SNAPCRAFT_STORE_CREDENTIALS` to that file's contents in the server's
environment.

## How it works

### Finding the repo

For each snap, the first of these that points to a repo that exists is used:

1. A repo you set with `set-repo`, or typed at the prompt.
2. The store listing's source code link.
3. A GitHub URL among the listing's website, issues or contact links.
4. `github.com/<your GitHub login>/<snap name>`. This catches private snaps, which the
   public store API can't see.
5. A prompt, when running in a terminal.

On GitHub, renamed repos are followed (and noted), and the tool records the default
branch, the location of `snapcraft.yaml` and the workflow files. Repos on other hosts
are recorded but not inspected.

### Detecting the builder

| Builder | When |
|---|---|
| `github-workflow` | A workflow builds the snap (`snapcore/action-build`, or `snapcraft` / `snapcraft pack` in a step). The tool records whether it also uploads (`snapcraft upload`, `snapcore/action-publish`), what triggers it (tags, manual dispatch, releases, pushes), and any hard-coded channel. |
| `launchpad` | A Launchpad snap recipe builds this snap from the repo. |
| `local` | Neither of the above: build with `snapcraft pack` on your machine. |
| `manual` | Only when you set it: you rebuild the snap some other way. |

A workflow or recipe that uploads to the store is preferred over one that only
builds. The candidates that weren't chosen are kept in the inventory.

### Deciding what needs work

`check` looks at the most stable published revision of each architecture on the
`latest` track.

| Signal | Verdict | When |
|---|---|---|
| Security | `rebuild` | A [Ubuntu Security Notice](https://ubuntu.com/security/notices) fixes a package the snap stages, and the fix is in the public Ubuntu archive. |
| Source | `update` | The repo has commits since the revision was uploaded, or `snapcraft.yaml` declares a different version. |
| Upstream | `update` | A part's git source has a newer tag than its `source-tag`, or new commits on the branch it tracks (GitHub only). |

For security, the check is **exact** when the published snap contains a build
manifest (`snap/manifest.yaml`). That manifest records the version of every staged
package, so a notice counts only if the snap has an older version than the fix.

Without a manifest, the check is **inferred**: it works out which packages the snap
stages (its `stage-packages` plus their dependencies, minus what the base snap
provides), and counts any notice issued after the revision was uploaded.

snapcraft writes the manifest only when building with `SNAPCRAFT_BUILD_INFO=1`.
Launchpad and `snapcore/action-build` set it automatically; for local builds, the
plans include it in the build command.

## Limitations

- Private snaps can't be downloaded, so their security checks are always inferred.
- Fixes that exist only in Ubuntu Pro (ESM) are shown as notes, because a rebuild
  can't pick them up.
- Non-security Ubuntu updates, and dependencies bundled by language plugins (pip,
  npm, cargo, go), aren't checked.
- Repo inspection, workflow detection and branch checks only support GitHub.
- The inferred check reads the base snap's package list from `/snap/<base>/current`.
  If that base isn't installed locally, it may include packages the base already
  ships.

## Data and privacy

- `inventory.json` is written to the current directory, the file given with `-i`, or
  `SNAP_MAINTAINER_INVENTORY`. The MCP server uses
  `~/.local/share/snap-maintainer/inventory.json` unless one of those is set.
- Ubuntu archive and security data is cached for 24 hours in
  `~/.cache/snap-maintainer` (about 20 MB per Ubuntu release and architecture).
- Manifests of published revisions are cached there permanently. Each public
  revision is downloaded once.
- Your Snap Store and GitHub logins stay with `snapcraft` and `gh`; this tool never
  reads or stores them.

## Development

```sh
pip install -e '.[mcp]' pytest
python3 -m pytest
```

Without the `mcp` package, the MCP server tests are skipped.

## License

[MIT](https://github.com/carlosbravoa/snap-maintainer/blob/master/LICENSE)
