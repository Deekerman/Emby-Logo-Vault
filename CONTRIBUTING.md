# Contributing

## The two constraints

**One file, standard library only.** `emby_logo_vault.py` has to run on a bare
Python 3.8 interpreter with nothing installed, because it is meant to be dropped
onto a server next to Emby and started. The HTML, CSS and JavaScript live in the
`PAGE` string in that same file for the same reason. A change that needs pip is
a change that has to be argued for first.

Tooling is exempt: ruff runs in CI, and the tests are `unittest`, which ships
with Python.

## Running the checks

```bash
python3 -m unittest discover -s tests -v   # no network, no Emby needed
python3 -m compileall -q emby_logo_vault.py
ruff check .                               # pip install ruff
```

The tests stub `fetch_channels`, `fetch_image` and `upload_image`, so they never
reach a server. Anything you add that talks to Emby should be testable the same
way: keep the API calls in thin functions and the decisions out of them.

## Testing against a real server

There is no test Emby. If your change touches uploads, drift detection or the
deep scan, say in the PR which Emby version you tried it on. `restore --preview`
and `status` change nothing, so they are safe to run while you work.

Point `--vault` at a scratch directory rather than your real one:

```bash
./emby_logo_vault.py --vault /tmp/scratch-vault backup
```

## What to be careful with

- **Anything that writes to Emby.** `do_restore` and `upload_image` are the only
  paths that do. Everything else writes to the vault, and that separation is
  worth keeping: the interface lets you stage changes and look at them before
  they leave.
- **Channel matching.** Keys are the normalized channel name, not the Emby item
  ID, because IDs change when a tuner is rebuilt. Changing `norm` invalidates
  existing vaults.
- **The manifest.** `write_manifest` writes to a temporary file and renames, so
  an interrupted run cannot leave half a manifest behind. Keep that.

## Style

Match what is there: four spaces, `.format()` over f-strings in the API helpers
for 3.8 clarity, comments that say why rather than what. Line length 100.

## Reporting a security issue

See [SECURITY.md](SECURITY.md).
