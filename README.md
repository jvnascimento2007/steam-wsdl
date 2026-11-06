# steam-wsdl

A command-line downloader for **Steam Workshop** mods, with concurrent downloads, resumable transfers, and a rich terminal interface.

You give it links (one item, several items, or a `.txt` file) and it downloads everything to a folder in parallel, showing the progress of each item.

## Translations

[Português](README.pt.md) | [Español](README.es.md)

## Table of contents

- [What it's for](#what-its-for)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Usage](#usage)
- [Options](#options)
- [Link file format](#link-file-format)
- [Output folder structure](#output-folder-structure)
- [Exit codes](#exit-codes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## What it's for

- Download many mods at once from a list of links.
- Download a single item by its link (or ID).
- Expand Workshop **collections** and download every item they contain.
- Resume interrupted downloads and automatically retry on network failures.
- Generate a list of the links that failed, so you can try them again later.

## How it works

The program queries Steam's public API (no key required) to get each item's title, game, and size, and then picks one of two methods:

| Method | When it's used | Notes |
|---|---|---|
| **Direct** | The item exposes a direct download URL (common in older games, such as Garry's Mod). | Plain HTTP, with several downloads in parallel (`-w`), resuming via `Range`, and retries with increasing delays. |
| **`steamcmd`** | The item has no direct URL (common in modern games). | Requires `steamcmd` to be installed. Each session logs in **only once** and downloads several items in sequence; multiple sessions run in parallel. |

If a direct download fails and `steamcmd` is available, the item automatically falls back to `steamcmd`.

> **Heads-up:** some games only allow Workshop items to be downloaded with an account that owns the game. In those cases the anonymous `steamcmd` login fails, and the item is reported as failed with a message explaining why (see `--steam-user`).

## Installation

### Requirements

- Python 3.9 or newer
- [uv](https://docs.astral.sh/uv/) (recommended)
- `steamcmd` (optional, but required for items without a direct download)

### Recommended method: system-wide `uv tool install`

From the project folder (the one containing `pyproject.toml`), run:

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install .
```

This installs the program into an isolated environment in `/opt/steam-wsdl` and creates the `steam-wsdl` command in `/usr/local/bin`, which is already on your PATH. Afterwards, from any folder:

```bash
steam-wsdl --help
```

Important notes about this command:

- `"$(which uv)"` is resolved **before** `sudo` runs, so it works even if `uv` is not on root's PATH.
- `uv tool install .` **copies** the code into the environment. You can move or delete the project folder without affecting the installed command.
- The environment is created as root and uses whichever Python `uv` picks. If `uv` downloads a Python into root's home folder, other users may be unable to run the command. To use the system Python, add `--python /usr/bin/python3` (it must satisfy the `requires-python` in `pyproject.toml`):

  ```bash
  sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install --python /usr/bin/python3 .
  ```

**Update** (after changing the code):

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install --force .
```

**Uninstall:**

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool uninstall steam-wsdl
```

### Install for your user only

Without `sudo`, the command goes to `~/.local/bin`:

```bash
uv tool install .
uv tool update-shell   # if the command isn't found, run this and then open a new terminal
```

### Without installing anything

```bash
uv run steam-wsdl -u "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789"
```

### Installing `steamcmd`

Only needed for items without a direct download.

- **Debian/Ubuntu:** `sudo apt install steamcmd` (may require enabling the `multiverse` repository and the `i386` architecture).
- **Arch Linux:** the `steamcmd` package from the AUR.
- **Other systems / Windows / macOS:** follow Valve's instructions at <https://developer.valvesoftware.com/wiki/SteamCMD>.

The program looks for `steamcmd`, `steamcmd.sh`, or `steamcmd.exe` on your PATH. If it's somewhere else, use `--steamcmd PATH`.

## Usage

A single item:

```bash
steam-wsdl -u "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789" -o ./mods
```

The `-u` argument also accepts just the numeric ID and can be repeated:

```bash
steam-wsdl -u 123456789 -u 987654321 -o ./mods
```

A list in a file (one link per line):

```bash
steam-wsdl -f mods.txt -o ./mods -w 8
```

Combining both and saving the failures so you can retry them:

```bash
steam-wsdl -f mods.txt -u 987654321 --failed-file failures.txt
steam-wsdl -f failures.txt          # retry only the ones that failed
```

Items that were already downloaded are **skipped**, so running the same command again is safe. Use `--overwrite` to download them again.

Interrupting with **Ctrl+C** shuts down gracefully. Partial files (`.part`) are kept and resumed on the next run.

## Options

### Input (at least one is required)

| Option | Description |
|---|---|
| `-u`, `--url LINK` | Link (or ID) of a Workshop item. Can be repeated. |
| `-f`, `--file FILE.txt` | Text file with one link per line. |

### General

| Option | Default | Description |
|---|---|---|
| `-o`, `--output FOLDER` | `workshop_downloads` | Output folder (created if it doesn't exist). |
| `-w`, `--workers N` | `4` | Simultaneous downloads, from 1 to 32. Also sets the number of `steamcmd` sessions. |
| `-r`, `--retries N` | `3` | Extra attempts on network failures, from 0 to 20. |
| `-t`, `--timeout SEC` | `30` | Network timeout in seconds, from 5 to 600. |
| `--overwrite` | off | Downloads items that already exist again. |
| `--failed-file FILE.txt` | — | Writes the links that failed to this file. |
| `-V`, `--version` | — | Shows the version. |
| `-h`, `--help` | — | Shows the help. |

### `steamcmd`

| Option | Default | Description |
|---|---|---|
| `--steamcmd PATH` | searches the PATH | Path to the `steamcmd` executable. |
| `--no-steamcmd` | off | Never uses `steamcmd`. Items without a direct download fail. |
| `--force-steamcmd` | off | Uses `steamcmd` for every item. |
| `--steam-user USER` | `anonymous` | Steam account with a login **already saved** in `steamcmd`. Forces 1 session, because the same account can't log in in parallel. |
| `--steamcmd-sessions N` | same as `-w` | Simultaneous `steamcmd` sessions (1 to 16). Each session logs in once and downloads several items in sequence. |
| `--steamcmd-timeout SEC` | `900` | Ends a session after SEC seconds with **no activity** (30 to 7200). |

Performance tip: each `steamcmd` session downloads **one item at a time**, so the parallelism equals the number of sessions. If you see odd failures with many sessions, lower it with `--steamcmd-sessions 2` or `3`.

## Link file format

One entry per line. Accepts full links or numeric IDs. Blank lines and lines starting with `#` or `;` are ignored, and only the first word of each line is used (the rest can serve as a note).

```text
# Server mods
https://steamcommunity.com/sharedfiles/filedetails/?id=123456789
https://steamcommunity.com/workshop/filedetails/?id=987654321
555444333        # just the ID works too
; whole collection
https://steamcommunity.com/sharedfiles/filedetails/?id=111222333
```

Invalid links don't stop the run: the program warns you with the line number and carries on with the rest. The file must be UTF-8.

## Output folder structure

Items are organized by game ID (App ID):

```text
workshop_downloads/
└── 108600/                                  # the game's App ID
    ├── My Mod [123456789].gma               # direct download: a single file
    └── Another Mod [987654321]/             # via steamcmd: a folder
        └── ...
```

Characters that are invalid in file names are replaced with `_`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All good. |
| `1` | At least one item failed (or there was an error querying the Workshop). |
| `2` | Usage error: invalid arguments, missing or unreadable link file, output folder without write permission. |
| `130` | Interrupted with Ctrl+C. |

This makes the program usable in scripts:

```bash
steam-wsdl -f mods.txt -o ./mods || echo "some items failed"
```

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `sem URL de download direto ... instale o steamcmd` (no direct download URL ... install steamcmd) | The item has no direct download and `steamcmd` wasn't found. Install it or use `--steamcmd PATH`. |
| `a Steam exige uma conta que possua o jogo` (Steam requires an account that owns the game) | The game doesn't allow anonymous downloads. Log in once in `steamcmd` with your account and use `--steam-user YOUR_USERNAME`. |
| `item não encontrado, removido ou privado` (item not found, removed, or private) | The item was deleted or isn't public. |
| `steamcmd ficou N s sem atividade` (steamcmd was idle for N s) | The session hung or the connection dropped. Run again (finished items are skipped) or raise `--steamcmd-timeout`. |
| Random failures with many sessions | Lower `--steamcmd-sessions`. |
| `steam-wsdl: command not found` | Check that `/usr/local/bin` (or `~/.local/bin`) is on your PATH. With `uv tool install` without `sudo`, run `uv tool update-shell`. |
| `Dependência ausente: rich` (missing dependency: rich) | The environment lacks its dependencies. Reinstall with `uv tool install --force .`. |
| `não foi possível usar a pasta de saída` (couldn't use the output folder) | No write permission on the folder given in `-o`. |

## Development

Module responsibilities and test instructions are documented in [Architecture](docs/architecture.md).

```bash
git clone <REPOSITORY-URL>
cd steam-wsdl
uv sync                          # creates the .venv with the dependencies
uv run steam-wsdl --help         # runs straight from the source code
uv tool install -e .             # optional: a command that reflects your edits instantly
```

Runtime dependencies: [`requests`](https://requests.readthedocs.io/) and [`rich`](https://rich.readthedocs.io/).

## Disclaimer

Only download content you have the right to use, and respect Steam's terms of service and each mod's license.