# Emby channel logo vault

Back up, preview and restore Emby live TV channel logos across all three Emby
channel image slots. Built for IPTV setups where a provider or EPG refresh can
silently replace your channel artwork.

Single Python file, standard library only, no pip install. Python 3.8 or newer.

## Why

Emby has no export for channel logos. If an M3U or EPG source overwrites them,
they are gone. This keeps a local copy, tells you what changed, and pushes the
good version back.

## Emby's three logo slots

| Slot | Used on | Notes |
|---|---|---|
| `Primary` | light backgrounds | the historic channel logo, usually dark artwork |
| `LogoLight` | dark backgrounds | plain light artwork |
| `LogoLightColor` | dark backgrounds | the default on most clients |

Primary is dark art for light backgrounds and the LogoLight pair is light art
for dark backgrounds. That inversion is why the interface has a background
switcher: you cannot judge a logo without seeing it on the background it will
actually appear on.

## Install

```bash
git clone https://github.com/Deekerman/Emby-Logo-Vault.git
cd Emby-Logo-Vault
chmod +x emby_logo_vault.py
```

There is nothing to install. The script imports only the standard library.

## Setup

Get an API key from Emby: Dashboard > Advanced > API Keys > New API Key.

```bash
export EMBY_URL="http://192.168.1.10:8096"
export EMBY_API_KEY="your_key_here"
export EMBY_LOGO_VAULT="/opt/logovault/vault"

./emby_logo_vault.py
```

Open `http://your-host:8077`.

First run: press **Deep scan**, confirm the logo columns populate, then
**Back up everything**.

## Web interface

**Preview background** switches every tile between white, near black and a
transparency checker. The checker catches artwork with a baked in white box
instead of a real alpha channel.

**Filters** narrow to All, Changed, Not saved, Edited or Excluded, plus a
channel name search. Every bulk action respects the current filter.

**Selection** bar offers Every cell, All primary, All logo light, All logo light
color, and Clear. Each is a toggle. There is a per channel checkbox too. Empty
slots are never selected. The footer buttons show how many of your selection
each action can actually act on, since pushing needs vault artwork and saving
needs Emby artwork.

**Per cell actions** appear on hover:

- **Copy to** duplicates that slot's artwork into either of the other two
- **From vault** picks artwork already held, folded by content hash so a logo
  shared by six channels appears once
- **Upload** takes a file from your computer, or drag one onto the cell
- **Forget** drops it from the vault

Everything writes to the vault only. Nothing reaches Emby until you push.

**Groups** lets you exclude channels from bulk operations. Emby imports M3U
`tvg-group` values as tags, so Dispatcharr group titles appear automatically. If
they do not, use the name patterns box instead: one rule per line, matched
anywhere in the channel name, case insensitive, or prefix a line with `re:` for
a regular expression.

## Command line

```bash
./emby_logo_vault.py backup             # save every logo to the vault
./emby_logo_vault.py status             # drift report, changes nothing
./emby_logo_vault.py restore --preview  # what a push would change
./emby_logo_vault.py restore            # push the vault back
./emby_logo_vault.py restore --force    # push everything regardless

./emby_logo_vault.py fill                              # copy Primary into empty slots
./emby_logo_vault.py fill --source LogoLightColor      # copy a different slot
./emby_logo_vault.py fill --overwrite                  # replace filled slots too

./emby_logo_vault.py groups             # list groups and exclusions
./emby_logo_vault.py exclude "PPV" "VOD"
./emby_logo_vault.py include "Sports"
```

`serve` is the default when no subcommand is given. It takes `--host`, `--port`
and `--open`.

## How it decides what changed

Matching is by normalized channel name, not Emby item ID. A tuner rebuild or an
M3U regeneration assigns new IDs, which would orphan an ID keyed backup.

Drift detection compares Emby's image tags first, falling back to a sha256 byte
comparison when tags are unavailable. So a source that swaps in its own artwork
is caught, rather than only missing artwork being noticed.

**Deep scan** exists because some Emby builds omit LogoLight tags from the
channel list response. It queries each image endpoint directly. Slower, so it is
a button rather than the default, but the CLI `backup` always deep probes.

## Vault layout

```
vault/
  manifest.json              current state
  manifest.<timestamp>.json  previous snapshots, kept automatically
  exclusions.json            group and name pattern rules
  images/                    the artwork itself
```

Backups snapshot the old manifest before overwriting, so running a backup after
a bad source has already clobbered things does not destroy your good copy.

## Running as a service

A unit file and an environment file template are in [`contrib/`](contrib).

```bash
sudo useradd --system --home /opt/logovault --shell /usr/sbin/nologin logovault
sudo mkdir -p /opt/logovault/vault
sudo cp emby_logo_vault.py /opt/logovault/
sudo chown -R logovault:logovault /opt/logovault

sudo cp contrib/logovault.service /etc/systemd/system/
sudo cp contrib/logovault.env.example /etc/logovault.env
sudo chmod 600 /etc/logovault.env
sudo editor /etc/logovault.env          # fill in EMBY_URL and EMBY_API_KEY

sudo systemctl daemon-reload
sudo systemctl enable --now logovault
```

A scheduled backup keeps the vault current, but consider whether you want one.
If a bad source overwrites your logos on Saturday, Sunday's run bakes the bad
version in as the new backup. The manifest snapshots give you a way back, but
you would have to go looking. For a frozen known good state, run backups by hand.

## Cautions

There is no authentication on the interface and the API key sits in the
environment, so keep the port on your LAN.

The vault directory holds the only copy of your logos. Put it somewhere your
existing backups already cover.

Copying Primary into a logo slot is often useful, since many providers ship only
one logo, but it is not universally safe. Primary is dark artwork, and a black
wordmark on a dark guide is invisible. Check the dark preview before pushing.

## Development

The tests use only `unittest` from the standard library and never touch a real
Emby server.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q emby_logo_vault.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[MIT](LICENSE).
