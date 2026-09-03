# Security

## What this tool assumes

The web interface has **no authentication**. Anyone who can reach the port can
read your channel artwork and push images into your Emby server. The Emby API
key is read from the environment and appended to every request URL.

That design is deliberate for a LAN tool, but it means:

- Bind it to a trusted network. `--host 127.0.0.1` if you only use it locally,
  or put it behind a reverse proxy that authenticates if it must be reachable.
- Never expose port 8077 to the internet or forward it through a router.
- Keep the environment file at mode 600, owned by the service user.
- Treat the vault directory as sensitive only insofar as your artwork is; it
  holds no credentials.

## Reporting a vulnerability

Open a [security advisory](https://github.com/Deekerman/Emby-Logo-Vault/security/advisories/new)
rather than a public issue, and give it a few days for a reply.

Please include what an attacker would need — network position, whether they need
an existing session, which endpoint. Reports that amount to "the interface has no
login" are already covered above.

## Never in an issue or a PR

Your Emby API key, your server's public address, or a screenshot with either
visible. The key appears in every request URL, so redact URLs before pasting
logs.
