# Security Policy

## Supported versions

Security fixes are released on the latest tagged version and the `main` branch. Users should update to the latest release before reporting a reproducible issue.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository:

https://github.com/DailenG/omartfy/security/advisories/new

Include the affected version, server/authentication mode, reproduction steps, impact, and any relevant sanitized logs. Never include real ntfy tokens, passwords, private topic names, notification payloads, or user state files.

If private reporting is unavailable, contact the repository owner through their GitHub profile and request a private channel. Do not open a public issue for an unpatched vulnerability.

## Security boundaries

Omartfy is an unsandboxed Omarchy shell plugin and runs with the current user's permissions. Its security-sensitive behavior is intentionally narrow:

- Configuration and state are stored locally with restrictive permissions.
- Credentials are sent only in HTTP authorization headers.
- TLS certificate verification is never disabled.
- Credentialed non-loopback plain HTTP requires explicit acknowledgement.
- Explicit, user-activated view and attachment destinations are restricted to validated HTTP(S) URLs.
- Publisher-supplied HTTP actions are disabled by default per server, require user activation, and do not follow redirects or retry.
- Automatic icon and attachment-preview requests, including every redirect, must retain the configured server's exact scheme, host, and effective port, unless the server's off-by-default **Load images from other hosts** option is enabled. Even then, other hosts must use HTTPS and resolve only to public addresses, so the option never reaches loopback or private-network services; each redirect is checked the same way. Size, format, dimension, redirect-count, and timeout limits apply either way, and credentials are only ever sent to the configured origin.
- Notification bodies render as plain text; Omartfy does not render remote HTML, Markdown, SVG, audio, or video.
- Native desktop toasts are opt-in, dispatch via static Omarchy summon commands, and never pass remote URLs or credentials in notification arguments; an image attachment is passed only as the path of its validated local cache file.

Topic confidentiality and authorization remain the ntfy server administrator's responsibility. An unprotected topic name can function as a public address.

## Disclosure

Please allow reasonable time for diagnosis and a release before public disclosure. Confirmed vulnerabilities will be acknowledged in release notes when doing so does not put unpatched users at additional risk.
