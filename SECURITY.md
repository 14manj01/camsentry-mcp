# Security

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** button (Security tab → Advisories) so the report stays
private. Do not open a public issue for security problems, and never paste tokens, passwords or camera
images into an issue.

## What is protected, and how

### Wyze credentials

| Secret | Where it goes |
| --- | --- |
| Wyze password, API Key ID, API Key, 2FA code | Typed into `wyze-cam-mcp login`, sent to Wyze over TLS once, then discarded. Never stored, never accepted from environment variables, files or MCP tool arguments. |
| Wyze access and refresh tokens | OS keychain only (Windows Credential Manager, macOS Keychain, Secret Service). Rotated tokens are written back to the keychain. |
| wyze-bridge username/password (optional) | OS keychain only, via `wyze-cam-mcp bridge-auth`. |

- The project never writes a secret to disk itself, so there is no `.env`, token cache or config file that
  could end up in a commit, a backup or a bug report. An MCP client config for this server contains no secrets.
- If the only available keychain backend is missing or stores plaintext, the server and CLI refuse to run
  rather than degrade.
- `wyze-cam-mcp logout` revokes the session at Wyze and deletes the keychain entries.
- If you believe a token leaked, run `wyze-cam-mcp logout`, delete the API key in the Wyze developer
  console, and change your Wyze password.

### Camera images

- Frames are held in memory only and are sent to exactly one place: the vision endpoint you configured.
- The vision endpoint must be a loopback address (`127.0.0.0/8`, `::1`, `localhost`). Anything else is
  rejected at startup unless `WYZE_CAM_ALLOW_REMOTE_VISION=1` is set. Requests to the vision endpoint and
  the bridge ignore proxy environment variables.
- `view_camera` returns a frame to the MCP client. If that client forwards conversations to a cloud model,
  the image goes with it. Use a local model, or do not call `view_camera`.

### The model is not trusted

A language model drives this server, and what a camera sees can contain attacker-written text (a sign held
up to the lens). The design assumes the model may be manipulated:

- **Read-only.** No tool can change a Wyze device or account. The only state a tool can change is the
  in-memory arming mode. The worst a manipulated model can do through this server is look at your own
  cameras and call `set_armed off`.
- **No URLs or paths in tool arguments.** Tools accept camera names and event ids. Image URLs come only from
  the Wyze API response, must be `https`, and are size-capped and checked to be real images.
- **Scrubbed output.** Tool errors are fixed messages; raw Wyze responses, tokens and pre-signed URLs are
  never included. Unexpected exceptions reach the model only as a generic error.
- **Scene text is data.** The vision prompt instructs the model not to follow text in images, and summaries
  are length-capped with control characters removed. This reduces, but cannot eliminate, prompt injection
  against the chat model that reads the summary. Do not connect this server to an agent that also holds
  tools you would not want triggered by a stranger with a sign.

### Transport

The server speaks MCP over stdio only. It opens no network port.

### Logging

`wyze-sdk` logs entire device records, including camera P2P keys, at WARNING level when it meets a device
model it does not recognise. This project raises that logger to ERROR.

## Repository hygiene

- gitleaks scans the full history on every push and pull request (`.github/workflows/ci.yml`).
- `.gitignore` blocks `.env*`, key files and virtual environments.
- Tests use fake accounts and an in-memory keychain. No test needs, reads or writes real credentials.
- Maintainers: enable GitHub secret scanning and push protection on forks.

## Known limits

- Wyze's API is unofficial and reverse engineered by the community. A session token grants the same access
  as the Wyze app, including device control, even though this project never uses it that way. Protect your
  OS user account accordingly.
- Any process running as your OS user can read your keychain entries on most platforms.
