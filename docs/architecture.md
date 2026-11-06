# Architecture

The project is a modular application with boundaries between coordination, integrations, and presentation. It does not use a dependency injection container or an event bus.

## Responsibilities

- `cli.py`: validates arguments with argparse, assembles dependencies, displays errors, and translates results into exit codes.
- `config.py`: typed options converted from the arguments validated by the CLI.
- `inputs.py`: reads the links file and extracts and deduplicates IDs.
- `workshop.py`: resolves metadata, expands collections, and preserves item order.
- `models.py`: `WorkshopItem` holds immutable metadata; `DownloadResult` holds execution state and results; `DownloadJob` associates the two. Each job belongs to one worker at a time.
- `orchestrator.py`: selects download methods, handles fallback and existing files, manages thread pools and a shared SteamCMD queue, and finalizes items.
- `backends/steam_api.py`: HTTP requests to the Steam API, per-thread sessions, and retries.
- `backends/http.py`: transfers with Range-based resumption, retries, and atomic replacement of the destination file.
- `backends/steamcmd.py`: persistent console execution, prompt/output parsing, per-item progress states, inactivity timeouts, and cleanup of process groups and staging directories.
- `storage.py`: file names, destination paths, and filesystem helpers.
- `ports.py`: the `Observer` contract for messages, progress, and completion, without Rich markup.
- `presentation/terminal.py`: implements the contract using Rich, including progress bars and the summary.
- `runtime.py`: session dependencies, without argparse or Rich types, a shared queue, or a global lock.

## Flow

CLI → input → item resolution → concurrent HTTP downloads → items deferred to SteamCMD → shared queue and persistent sessions → summary and exit code.

HTTP downloads receive configuration, cancellation, and the observer explicitly. SteamCMD sessions receive the execution context and a completion callback; they do not depend on the orchestrator. The orchestrator combines errors from both download methods before notifying the presentation layer.

The orchestrator puts deferred items into one thread-safe queue, larger items first, and starts at most the configured session limit or item count. Workers claim one item at a time after their stagger delay. A worker with no remaining queued work never starts SteamCMD. Authenticated accounts are limited to one concurrent session.

On POSIX systems, a worker opens a pseudoterminal and starts one SteamCMD process with its own controlling terminal and process group. A small Python exec helper acquires the terminal without using `preexec_fn` inside a threaded application. The worker logs in once, then sends a `workshop_download_item` command for each claimed item. The reader decodes output chunks, including prompts without newlines. The next `Steam>` prompt delimits a command; an explicit success for the expected item and its content directory are required before publication. Windows retains a dependency-free command-line fallback with one invocation/login per item; it uses the same shared queue, progress, and staging logic.

A process failure or inactivity timeout closes its process group and retries only its owned item, up to `--retries` times. Explicit item errors do not restart a healthy console. The remaining queue is neither consumed nor failed by a broken worker. Shutdown tries `quit`, then forcibly terminates the process group after a bounded wait, including shell-launcher children that could otherwise hold the terminal open.

Each process owns `.staging/session-*` inside the output filesystem. Success publishes an item using directory rename, never a cross-device copy fallback. Other sessions' directories are untouched. The empty `.staging` parent may remain.

The active progress row includes Starting, Downloading, Awaiting confirmation, and Finalizing. Progress is the monotonic maximum of the staging/content directory sizes, not their sum; it remains an estimate of logical file sizes. A full bar does not publish the item or advance its command. No automatic concurrency tuning is performed.

On interruption, the pool waits for workers, and remaining jobs are marked as cancelled. HTTP `.part` files are preserved; each worker's SteamCMD staging directory is removed when its process ends. Incomplete SteamCMD downloads are not retained across retries or cancellation.

## Verification

Install the development dependencies and run the test suite:

```bash
uv sync --dev
uv run python -W error::ResourceWarning -m unittest discover -s tests -v
```

Measure line and branch coverage and enforce the configured 100% threshold:

```bash
uv run coverage erase
uv run coverage run -m unittest discover -s tests -v
uv run coverage report
uv run coverage html
```

Open `htmlcov/index.html` to inspect coverage by module. Coverage configuration lives in `pyproject.toml`: it includes every module in `steam_wsdl`, enables thread and branch measurement, and fails the report command below 100%. Generated reports are ignored by Git. No application modules are omitted from measurement.

The suite covers method selection, HTTP resumption and restart, retries and permanent failures, overwrite behavior, metadata and collections, input validation, SteamCMD discovery, persistent sessions, dynamic scheduling, progress states, process cleanup, cancellation with active workers, filesystem errors, terminal rendering, entry points, and exit codes.

Tests use the standard library's `unittest`, simulated API responses, temporary files, real worker threads, local simulated SteamCMD executables, and a real HTTP server bound to `127.0.0.1` on an ephemeral port. The test environment must allow local sockets and subprocesses. Automated tests do not access Steam or require credentials. A separate manual smoke check on Linux verified anonymous login and two local console queries in one real SteamCMD process; live Workshop transfer and native Windows/macOS behavior are outside the automated suite. Full measured coverage does not guarantee every possible input or concurrency ordering has been tested.

CLI smoke checks:

```bash
uv run steam-wsdl --help
uv run python -m steam_wsdl --version
```
