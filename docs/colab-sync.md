# Colab file sync and startup

The private backup mirrors `/opt/data` (`HERMES_HOME`). In the Colab profile it includes project files, nested Git repositories, virtual environments, Node dependencies, managed tools, browser binaries, empty directories, symlinks targeting files inside the home and file permissions. Deletions still propagate.

Only runtime log churn (`logs/`, `gateway.log`, `gateway.log.1`), root pip/uv/npm download caches (`.cache/pip`, `.cache/uv`, `.cache/npm`), Python bytecode and non-regular filesystem objects are excluded. Project paths such as `workspace/logs/report.log` and `workspace/tmp/draft.tmp` are included. Files written elsewhere on the VM are outside this backup; save projects under `/opt/data/workspace`.

An unreadable source directory or file aborts the snapshot. Failed encryption retains the previous backup; failed decryption blocks startup and removes permission to publish the partial restore. Explicit `GIT_STATE_ENV_MODE=omit` still omits credentials and databases; Colab uses encrypted state.

Same-VM restarts preserve unsynced local edits and deletions when the remote revision has not advanced and the checkpoint belongs to the same home directory and VM. A fresh VM, changed remote or absent checkpoint requires a full restore. An unchanged remote is never evidence that an unrelated local directory is safe to adopt.

Fresh starts download the private state alongside public Python/UI installation, then wait for that download to finish before restoring. Warm starts avoid re-copying the home and re-hashing databases. Colab Git packing uses four threads and larger caches rather than Render's small-memory limits. First downloads and the first backup of newly included files can still take time; startup estimates use measured previous starts.

## Update an already-running Colab runtime

Run this in a new cell:

```python
import urllib.request
exec(urllib.request.urlopen(
    'https://raw.githubusercontent.com/Sugamdeol/hermes-render/main/update-storage.py',
    timeout=60,
).read())
```

The updater validates the running backup process, installs scripts from one immutable main revision, waits for the old uploader and its Git children to finish, and starts the replacement worker. It never runs restore, resets the state repository, or restarts Telegram, the dashboard or the tunnel. It also keeps the replacement worker attached to `HERMES_COLAB.stop()` and runs `HERMES_COLAB.backup()` for confirmation. If saving fails, keep Colab running and retry `HERMES_COLAB.backup()` after correcting the reported cause.

The automatically updated Start cell picks up startup improvements on the next launch. Files that never reached GitHub and are already gone from the VM cannot be recovered by changing backup rules.

## Active browser databases

SQLite snapshots pin a read transaction and have a bounded progress deadline (10 seconds by default, configurable with `GIT_STATE_SQLITE_TIMEOUT_SECONDS`). If a browser holds an exclusive lock, the worker captures a stable database plus WAL/rollback journal, checks that their metadata did not change during copying, validates the temporary image, and snapshots that copy. No live browser connection is closed or modified. An unstable image, failed integrity check or external multi-database super-journal retains the previous backup and reports the reason.

Encryption is identified from the manifest, with header-based compatibility for legacy backups. A filename ending in `.enc` alone is not evidence of backup encryption. Tcl encoding tables and user-owned encrypted artifacts keep their filenames and bytes. The running updater recovers missing ordinary `.enc` files from the local current/previous backup trees without replacing existing live files, then restarts only the uploader and confirms a save.

## Files deleted while a backup is running

A file or directory can disappear between enumeration and copying, especially when Chrome rotates Safe Browsing stores. The worker confirms that the source entry is absent while the home remains accessible, removes partial output for that entry, and records its deletion. A replaced/still-present source, a missing destination or a permission/read error still fails the snapshot and retains the prior backup. A home directory replaced during capture is rejected. The same rule handles SQLite databases removed before they can be opened.

## Upload changes immediately

Colab's Linux watcher now records the exact changed paths. Once the first reconciliation completes, a write wakes the uploader without a debounce or minimum interval. It updates only those files in the existing snapshot, records additions, renames and deletions, and folds WAL changes into a consistent SQLite database snapshot even while chats remain open. Edits made during an upload stay queued for the next upload. No chat completion is required.

A rollback journal protects the previous local snapshot if copying fails or the worker exits mid-update. Initial reconciliation, inotify overflow and watcher failure still require a full audit. An hourly audit (`GIT_STATE_FULL_AUDIT_SECONDS`, default 3600) also catches changes that bypass filesystem notifications. Ordinary saves do not rebuild or fingerprint the complete home. Git staging forces inclusion of ignored files, and an encoded backup path preserves nested `.git` directories and ordinary `.enc` filenames without Git's embedded-repository handling; restore reverses the encoding.

`update-storage.py` installs the changed-path worker while Dashboard and Telegram remain running. The first update builds its inventory once and can take longer than later saves. `GIT_STATE_STATUS_FILE` reports planning, snapshot, staging and uploading phases. Upload completion still depends on GitHub and network speed; “saved” is reported only after confirmation.
