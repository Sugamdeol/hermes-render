# Faster Colab startup

Open Hermes.ipynb and run its start cell. The Drive cache option mounts your Drive after consent and saves public UI build artifacts and package downloads under MyDrive/HermesRuntimeCache. Credentials, private chats, and /opt/data are excluded from this cache. Turn the option off to keep caches only in the temporary VM.

Small code updates reuse Python dependencies and built dashboards when the pinned upstream and runtime recipe match. Missing core packages are installed automatically. A fresh VM still needs system packages and Python installation; cached downloads and UI builds reduce repeat work. No fixed startup time is guaranteed.

Installed Python packages in the agent and notebook environment, manually installed apt packages, global npm packages, and Playwright browsers are inventoried every five minutes and before HERMES_COLAB.backup() or stop(). The recipe is included in the encrypted private state backup. Extra packages restore in the background so the dashboard can start sooner. Check HERMES_COLAB.dependencies() before a task requiring extras; ready must be true. Failed installs preserve the recipe and report errors.

System Python packages already supplied by Colab are not downgraded. Private package indexes, arbitrary downloaded executables, local editable projects, custom installers, and GPU drivers are not portable through this recipe. Package names and versions are saved; credentials and package index URLs are not. Previously recorded packages remain in the recipe until explicitly removed from it.

Before switching runtimes, call HERMES_COLAB.stop() and wait for a successful backup. A abruptly deleted VM cannot save packages installed after the last successful inventory and GitHub backup. Run only one host for the same Telegram bot and state repository.

For an already-running older notebook, run update-startup.py in the same notebook. It verifies downloads, saves existing packages and chats, stops after a successful backup, and restarts with the new launcher. Drive permission is required once when persistent caching is enabled.
