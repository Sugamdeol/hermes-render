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
