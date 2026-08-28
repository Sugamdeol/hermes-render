# Repo-managed environment variables

Two files live here, and the split matters:

| File | Contents | Committed? |
|---|---|---|
| `common.env` | Non-secret configuration shared by Render and local runs | Yes, in plaintext |
| `secrets.enc.env` | API keys and tokens, values encrypted with SOPS + age | Yes, encrypted |

`secrets.enc.env` does not exist until you create it:

```bash
./run-local.sh secrets init     # generate an age key, write .sops.yaml
./run-local.sh secrets edit     # opens $EDITOR on the decrypted file
```

`secrets init` prints the private key you must paste into Render's Environment
tab as `SOPS_AGE_KEY` so the deployed service can decrypt too. Without that
variable the service still boots — it just falls back to whatever is in
Render's Environment tab, and logs that decryption was skipped.

## Precedence at boot

1. **The process environment** — Render's Environment tab, or your shell.
   Always wins; the repo never overrides a live deploy knob.
2. **An existing `$HERMES_HOME/.env`** — written by the dashboard's API Keys
   tab or restored from GoFile. Preserved, so a key set from the UI is not
   reverted to the committed one.
3. **`secrets.enc.env`** — fills in whatever is still missing.

Set `RENDER_TOOLS_SECRETS_FORCE=1` to swap 2 and 3, which is what you want
after rotating a key in git.

## Never commit

The age private key, `~/.config/sops/age/keys.txt`, or any decrypted copy of
`secrets.enc.env`. `.gitignore` covers the obvious filenames, but the
protection that actually matters is not creating plaintext copies inside the
repo in the first place.
