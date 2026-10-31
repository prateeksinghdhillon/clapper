# Security

Clapper runs inside ComfyUI and is meant for a computer you control, used by people you trust. Please read what it does and does not protect before you let anyone else reach it.

## What profiles protect

Profiles keep projects **private inside Clapper**: each profile sees only its own projects and chats, and every `/clapper/` API call and media file except the login page (and its scripts and styles) needs a logged-in profile.

Profiles are **not** a security boundary for the machine:

- **ComfyUI itself has no login.** Anyone who can reach ComfyUI's port (8188 by default) can use ComfyUI's own page and API: queue any job, read its queue, history and output folder, see its log, and install or run custom nodes through extensions such as ComfyUI-Manager.
- The CLI (`cli/clapper.py`) works on project folders directly and asks for no profile. It is meant for whoever runs the computer.

## Don't expose ComfyUI to the internet

Keep ComfyUI on `127.0.0.1` or your home network. To use Clapper from a phone or another place, use a private network or VPN (for example Tailscale or WireGuard) or an SSH tunnel, not a port opened on your router. Clapper serves plain HTTP; if you put it behind a reverse proxy, use HTTPS so passwords and session cookies are not sent in the clear.

## What is stored, and where

| File | Contents | Protection |
|---|---|---|
| `~/.clapper/profiles.json` | Profile names, project ownership, passwords | Salted scrypt hashes, never the password; file readable only by its owner (0600) |
| `~/.clapper/sessions.json` | Logged-in devices | A SHA-256 of each session token, never the token; the cookie is `HttpOnly`, `SameSite=Lax` |
| `~/.clapper/llm.json` | Assistant providers, **API keys** | File readable only by its owner (0600); keys are never sent to the browser (only a `…abcd` hint) |
| Project folders | Scenes, takes, references, chats | Plain files; Clapper only opens projects inside the home folder |

Failed logins are slowed down by one second each. Anyone with a shell on the computer running ComfyUI can read everything above.

With an online assistant provider, the project snapshot (scene texts, style text, story notes, reference definitions and cards) and any images the assistant looks at are sent to that provider. The local model sends nothing off the computer.

## Reporting a vulnerability

Please report security problems privately through the repository's private vulnerability reporting (*Security → Report a vulnerability*), not in a public issue. Include the Clapper and ComfyUI versions and steps to reproduce. You will get an answer as soon as a maintainer has looked at it.
