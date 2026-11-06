# Architecture

The project is a modular application with boundaries between coordination, integrations, and presentation. It does not use a dependency injection container or an event bus.

## Responsibilities

- `cli.py`: validates arguments with argparse, assembles dependencies, displays errors, and translates results into exit codes.
- `config.py`: typed options converted from the arguments validated by the CLI.
- `inputs.py`: reads the links file and extracts and deduplicates IDs.
- `workshop.py`: resolves metadata, expands collections, and preserves item order.
- `models.py`: `WorkshopItem` holds immutable metadata; `DownloadResult` holds execution state and results; `DownloadJob` associates the two. Each job belongs to one worker at a time.
- `orchestrator.py`: selects download methods, handles fallback and existing files, manages thread pools, balances sessions, and finalizes items.
- `backends/steam_api.py`: HTTP requests to the Steam API, per-thread sessions, and retries.
- `backends/http.py`: transfers with Range-based resumption, retries, and atomic replacement of the destination file.
- `backends/steamcmd.py`: batch execution, output parsing, inactivity timeouts, and cleanup of processes and temporary files.
- `storage.py`: file names, destination paths, and filesystem helpers.
- `ports.py`: the `Observer` contract for messages, progress, and completion, without Rich markup.
- `presentation/terminal.py`: implements the contract using Rich, including progress bars and the summary.
- `runtime.py`: session dependencies, without argparse or Rich types, a shared queue, or a global lock.

## Flow

CLI → input → item resolution → concurrent HTTP downloads → items deferred to SteamCMD → batched sessions → summary and exit code.

HTTP downloads receive configuration, cancellation, and the observer explicitly. SteamCMD sessions receive the execution context and a completion callback; they do not depend on the orchestrator. The orchestrator combines errors from both download methods before notifying the presentation layer.

Each SteamCMD invocation logs in once and processes up to 40 items. A session may make multiple invocations if it exceeds this limit. Authenticated accounts are limited to one concurrent session. The existing threading model and options have been preserved.

On interruption, the pool waits for workers, and remaining jobs are marked as cancelled. HTTP `.part` files are preserved; the SteamCMD temporary directory is still removed when the batch ends.

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

The suite covers method selection, HTTP resumption and restart, retries and permanent failures, overwrite behavior, metadata and collections, input validation, SteamCMD discovery, batched downloads, progress, process cleanup, cancellation with active workers, filesystem errors, terminal rendering, entry points, and exit codes.

Tests use the standard library's `unittest`, simulated API responses, temporary files, real worker threads, local simulated SteamCMD executables, and a real HTTP server bound to `127.0.0.1` on an ephemeral port. The test environment must allow local sockets and subprocesses. No tests access Steam or require credentials; compatibility with the live Steam service and an installed SteamCMD is outside this suite. Full measured coverage does not guarantee every possible input or concurrency ordering has been tested.

CLI smoke checks:

```bash
uv run steam-wsdl --help
uv run python -m steam_wsdl --version
```
