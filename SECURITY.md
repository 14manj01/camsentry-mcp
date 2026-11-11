# Security

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** button (Security tab → Advisories) so the report stays
private. Do not open a public issue for security problems, and never paste tokens, passwords, phone
numbers or camera images into an issue.

## What is protected, and how

### Credentials and contact details

| Secret | Where it goes |
| --- | --- |
| Wyze password, API Key ID, API Key, 2FA code | Typed into `camsentry-mcp setup` or `login`, sent to Wyze over TLS once, then discarded. Never stored, never accepted from environment variables, files or MCP tool arguments. |
| Wyze access and refresh tokens | OS keychain only (Windows Credential Manager, macOS Keychain, Secret Service). Rotated tokens are written back to the keychain. |
| Your phone number, SMS key, ntfy topic, webhook URL | OS keychain only. They are sent to the notification service you chose, and nowhere else. |
| Your mailbox address and its app password (email alerts) | OS keychain only. The password is sent only to your mail provider's server, over an encrypted connection, to sign in. Use an app password, never your main mail password: it can be revoked on its own. |

- The project never writes a secret to disk itself, so there is no `.env`, token cache or config file that
  could end up in a commit, a backup or a bug report. An MCP client config for this server contains no secrets.
- If the only available keychain backend is missing or stores plaintext, the server and CLI refuse to run
  rather than degrade.
- `camsentry-mcp logout` revokes the session at Wyze and deletes the tokens. `camsentry-mcp notify off`
  deletes every notification destination.
- If you believe a token leaked, run `camsentry-mcp logout`, delete the API key in the Wyze developer
  console, and change your Wyze password.

### Camera images

- Images are held in memory only and are sent to exactly one place: the vision endpoint you configured.
  They are never stored and never attached to notifications.
- The vision endpoint must be a loopback address (`127.0.0.0/8`, `::1`, `localhost`). Anything else is
  rejected at startup unless `CAMSENTRY_ALLOW_REMOTE_VISION=1` is set. Requests to the vision endpoint
  ignore proxy environment variables.
- `view_camera` returns an image to the MCP client. If that client forwards conversations to a cloud model,
  the image goes with it. Use a local model, or do not call `view_camera`.

### What is remembered

- A local SQLite database in your user data folder keeps, for 30 days, what the vision model said about each
  still: camera name, time, counts, a one-sentence description, alerts. It also keeps your watch rules, the
  arming mode and per-camera alert choices.
- It holds no credentials, no image URLs and no images. On Linux and macOS the file is created readable by
  you only. It is not encrypted: anyone who can read your user profile can read a text diary of what your
  cameras saw.
- `camsentry-mcp forget` deletes the remembered stills.

### What leaves your machine

| Data | Goes to | When |
| --- | --- | --- |
| Session tokens, requests for events and stills | Wyze | Always. |
| Alert text: camera name, time, the model's one-sentence description | ntfy.sh (or your ntfy server), your mail provider, textbelt.com, or your webhook | Only for the channels you set up. |
| Your phone number | textbelt.com | Only if you set up SMS. |
| Camera images | Nowhere | Unless you opted in to a remote vision endpoint. |

An ntfy topic is a shared secret, not an account: anyone who learns the topic name can read your alerts.
The topic is generated with 144 bits of randomness and kept in the keychain.

### The model is not trusted

A language model drives this server, and what a camera sees can contain attacker-written text (a sign held
up to the lens). The design assumes the model may be manipulated:

- **Read-only towards Wyze.** No tool can change a Wyze device or account.
- **A model can message only you.** `notify_user` has no recipient argument. It sends to the destination in
  your keychain, at most 6 times an hour.
- **A model cannot silence alerts unnoticed.** The tools that change what you are alerted about
  (`set_armed`, `set_camera_alerts`, `remove_watch_rule`) send a message to your phone saying what changed.
  A manipulated model can still make those changes; you will know that it did.
- **No URLs or paths in tool arguments.** Tools accept camera names, event ids, short rule texts and search
  words. Image URLs come only from the Wyze API response, must be `https`, and are size-capped and checked
  to be real images.
- **Scrubbed output.** Tool errors are fixed messages; raw Wyze responses, tokens, pre-signed URLs and
  notification destinations are never included. Unexpected exceptions reach the model only as a generic error.
- **Scene text is data.** The vision prompt instructs the model not to follow text in images, and
  descriptions are length-capped with control characters removed. This reduces, but cannot eliminate, prompt
  injection against the chat model that reads them. Do not connect this server to an agent that also holds
  tools you would not want triggered by a stranger with a sign.
- **Watch rules reach the vision model.** A rule's text is put into the prompt for the local vision model.
  Whoever can add rules (you, or your chat model) can influence what that model reports.

### Transport and background work

The server speaks MCP over stdio only. It opens no network port. Its background watcher makes outgoing
requests to Wyze, to your local model server and to your notification service, and nothing else.

`camsentry-mcp autostart on` writes one file into your per-user login-items folder (the Startup folder on
Windows, `~/Library/LaunchAgents` on macOS, `~/.config/autostart` on Linux) that runs the watcher as you.
It needs no administrator rights, installs no system service, and `autostart off` deletes the file.

### Logging

Two libraries would otherwise write secrets to the server's stderr, and both are silenced:

- `httpx` logs every request URL at INFO level. Wyze image URLs are pre-signed, and ntfy topics are passwords.
- `wyze-sdk` logs entire device records, including camera P2P keys, at WARNING level when it meets a
  device model it does not recognise.

## Repository hygiene

- gitleaks scans the full history on every push and pull request (`.github/workflows/ci.yml`).
- `.gitignore` blocks `.env*`, key files and virtual environments.
- Tests use fake accounts, fictional phone numbers and an in-memory keychain. No test needs, reads or writes
  real credentials or contact details.
- Maintainers: enable GitHub secret scanning and push protection on forks.

## Known limits

- Wyze's API is unofficial and reverse engineered by the community. A session token grants the same access
  as the Wyze app, including device control, even though this project never uses it that way. Protect your
  OS user account accordingly.
- Any process running as your OS user can read your keychain entries and the memory database on most platforms.
- Image downloads identify themselves like the Wyze Android app, because Wyze's image host refuses other clients.
- Alerts are a small model's reading of low-resolution stills. They can be wrong in both directions; see
  [docs/ACCURACY.md](docs/ACCURACY.md).
