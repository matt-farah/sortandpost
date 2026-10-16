# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through
GitHub: go to the repository's **Security** tab → **Report a vulnerability**. Include what
you found, how to reproduce it, and which version or commit you tested.

You should get a reply within a week. Once a fix is released, you'll be credited in the
release notes unless you'd rather not be.

## Supported versions

Only the latest release gets security fixes.

## Threat model in short

sortandpost.photos is a single-user app that runs on your own computer. The things worth
protecting are:

- **Who can reach the web server.** By default it only listens on this computer
  (`127.0.0.1`). Opening it to a network requires a password (`SORTANDPOST_PASSWORD`), and
  over a network it should sit behind HTTPS. Host and Origin checks guard against DNS
  rebinding and cross-site requests.
- **Which files it writes or moves.** It only ever writes rating / keyword metadata into
  your photos, and moves files only when you explicitly delete them (into a recoverable
  trash). Anything that could make it write elsewhere under your photo folders is a bug
  worth reporting.
- **Secrets.** Posting-service API keys live in the OS keychain (or an env var under
  Docker), never in a file under the app's data folders and never in the browser.

The shared sync folder is **trusted**: anyone who can write to it can change your library.
Only share it with your own devices.

The full review, with each finding and its status, is in
[docs/security-review.md](docs/security-review.md).
