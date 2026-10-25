# Omartfy

[![Release](https://img.shields.io/github/v/release/DailenG/omartfy?sort=semver&style=flat-square)](https://github.com/DailenG/omartfy/releases/latest)
[![CI](https://img.shields.io/github/actions/workflow/status/DailenG/omartfy/ci.yml?branch=main&style=flat-square&label=tests)](https://github.com/DailenG/omartfy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-8da8ff?style=flat-square)](LICENSE)

A multi-server [ntfy](https://ntfy.sh/) client for the Omarchy bar. Omartfy combines notifications from ntfy.sh and self-hosted servers into one keyboard-friendly panel while keeping each server's authentication, connection state, and unread count separate.

**[Website](https://daileng.github.io/omartfy/)** · **[Latest release](https://github.com/DailenG/omartfy/releases/latest)** · **[Report a bug](https://github.com/DailenG/omartfy/issues/new)**

![Omartfy notification panel with safe demo notifications](preview.png)

> The screenshots use local fixture data. Omartfy follows the active Omarchy theme.

## Features

- One symbolic bar icon with unread, urgent, and connection indicators
- Merged chronological **All** inbox plus per-server tabs and quick filtering
- Multiple topics and credentials per server profile
- Bearer token, Basic, and unauthenticated connections
- Local read, delete, and clear state without publishing changes to ntfy
- Configurable header action to delete all notifications in the active view (with confirmation) or mark them all as read
- ntfy view, copy, and opt-in HTTP actions in an ntfy-style action row
- Timed or indefinite DND from the bar context menu
- Opt-in native Omarchy desktop toasts with global, per-server, duration, DND, and click-to-open controls
- Lazy icon and image-attachment previews with strict size, format, redirect, and origin checks
- Keyboard navigation, inline search, overflow tabs, and a dedicated server editor
- Durable cursor replay and event deduplication across shell restarts

## Requirements

- Omarchy with the Quattro shell plugin system
- Python 3
- `setpriv` from util-linux
- `wl-copy` from wl-clipboard
- `omarchy-launch-browser`
- `omarchy-notification-send`

No pip package, npm package, third-party ntfy SDK, SSE package, or WebSocket package is required.

## Install

```bash
omarchy plugin add https://github.com/DailenG/omartfy --enable
```

The widget defaults to the right bar section. Open the panel and select **Settings** to add an ntfy server and one or more comma-separated topics.

Omartfy intentionally creates no default subscription: an unprotected ntfy topic name can act as a public address.

### Update

```bash
omarchy plugin update dailen.omartfy
```

Restart the shell only if an update does not hot-reload:

```bash
omarchy restart shell
```

## Configure servers

![Omartfy server editor with safe local fixture profiles](docs/assets/settings.png)

Each profile has its own connection and trust boundary:

- **Label:** unique local name shown on tabs and notifications
- **Base URL:** `https://ntfy.sh` or a self-hosted HTTP(S) URL; reverse-proxy path prefixes are supported
- **Topics:** one or more comma-separated ntfy topic names
- **Authentication:** none, Bearer token, or Basic username/password
- **Allow publisher-supplied HTTP actions:** disabled by default; enable only for trusted publishers
- **Load images from other hosts:** disabled by default; lets icons and image attachments load from hosts other than the server, such as YouTube thumbnails from an RSS bridge. Those hosts see your IP address and when the image loads
- **Show toasts from this server:** enabled per profile by default; the global desktop-notification switch remains the master control
- **Allow credentials over insecure HTTP:** shown only when credentials would cross non-loopback plain HTTP

A blank secret while editing an existing profile preserves the stored secret. Selecting a connected server filters the inbox; use the pencil button in **Settings** to edit that profile.

## Desktop notifications

Native Omarchy toasts are disabled globally after installation and updates. Enable **Show native Omarchy toasts** in **Settings**, then disable individual noisy profiles with **Show toasts from this server**.

**Omarchy default** follows ntfy priority: low-priority messages use the native low lifetime, normal and high messages use the native normal lifetime, and urgent messages remain until dismissed. The explicit **8 seconds**, **15 seconds**, and **30 seconds** choices use that exact lifetime regardless of priority; **Until dismissed** requires manual close.

Initial cached history populates the Omartfy inbox silently. New messages after the established cursor, including reconnect catch-up messages, can create native toasts. Left-clicking a toast summons the Omartfy panel on the focused monitor; right-clicking dismisses it.

Omartfy DND and Omarchy's global notification DND both suppress popups while the durable Omartfy inbox continues collecting. Omarchy owns native notification history as presentation state; it is not synchronized with Omartfy read or delete state.

## Header action

In **Settings**, the **Header action** setting controls the panel header button:

- **Delete all (default):** Clears all notifications in the active view or server tab after confirmation.
- **Read all:** Immediately marks all unread notifications in the active view or server tab as read without removing them from the inbox.

## Keyboard controls

Omarchy plugins do not claim global compositor shortcuts. To open Omartfy directly with `Super+N`, add this user-level binding to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + N", "Omartfy notifications", "omarchy-shell shell toggle dailen.omartfy '{}'")
```

The panel focuses its first notification when opened from the shortcut.

Clicking a notification that has a click URL opens it and marks it read, as tapping one does on a phone. Use the ⌄ button beside ✓ and × (or `e`) to expand it instead. Notifications without a click URL expand when clicked.

| Key | Action |
| --- | --- |
| `j` / `k`, Up / Down | Select notification |
| `h` / `l`, Left / Right | Switch server tab |
| Enter / Space | Mark selected notification as read |
| `e` | Expand or collapse selected notification |
| `1`–`3` | Run the corresponding visible ntfy action |
| `o` | Open the notification click URL |
| `a` | Open the attachment URL |
| `d`, `x`, or Delete | Delete locally |
| `r` | Reconnect and reload |
| `s` | Open server settings |
| `/` | Search |
| Escape | Close search, nested UI, or panel |
| Tab / Shift-Tab | Switch adjacent Omarchy panels |

## Do Not Disturb

Right-click the Omartfy bar icon and choose **1 hour**, **4 hours**, **Until tomorrow**, or **Until turned off**. DND continues collecting notifications while suppressing the unread badge and urgent icon color. The setting survives shell restarts; use **Turn off DND** from the same menu to resume normal presentation.

## Data and security

Omarchy plugins run unsandboxed with the current user's permissions. Omartfy:

- Reads configuration from `${XDG_CONFIG_HOME:-~/.config}/omarchy/ntfy.json`
- Stores cursor, notification, deletion, DND, and media state under `${XDG_STATE_HOME:-~/.local/state}/omarchy/ntfy/`
- Writes configuration and state atomically with mode `0600`; the state directory uses mode `0700`
- Stores configured tokens and passwords locally in the mode-`0600` JSON configuration file; it does not use a keyring
- Sends credentials only in HTTP authorization headers, never URLs or process arguments
- Never disables TLS certificate verification
- Keeps HTTP actions disabled per server until explicitly enabled
- Runs publisher-provided HTTP actions only after user activation, without redirects or retries
- Launches validated HTTP(S) view URLs through `omarchy-launch-browser` and copies values through `wl-copy`
- Fetches notification icons and attachment previews only from the configured server's exact scheme, host, and effective port, unless **Load images from other hosts** is enabled for that server, in which case other hosts must use HTTPS and resolve only to public addresses; every automatic-media redirect is checked under the same per-server policy
- Keeps explicit, user-activated opening of validated HTTP(S) view and attachment URLs separate from automatic media fetching

Review topic access controls and publisher trust before enabling a server or HTTP actions. See [SECURITY.md](SECURITY.md) for the reporting policy and implementation boundaries.

## Troubleshooting

### A server stays “connecting”

Use the profile's **Test** button first. Confirm that the base URL is reachable, the topic exists, and the configured credentials can read it. Reverse proxies must allow long-lived HTTP streaming; if they buffer or close the stream, Omartfy automatically falls back to periodic polling.

### Messages or topics appear stale

Press `r` or use the ↻ header button to reconnect and reload configuration. Omartfy resumes from its persisted ntfy cursor and deduplicates replayed events.

### Actions are disabled

View and copy actions require a valid destination. HTTP actions additionally require **Allow publisher-supplied HTTP actions** on that server. Enable it only when every publisher on the subscribed topics is trusted.

### The panel does not open

Validate the installed plugin and restart the shell:

```bash
omarchy plugin validate ~/.config/omarchy/plugins/dailen.omartfy
omarchy restart shell
```

## Remove

```bash
omarchy plugin remove dailen.omartfy
```

Removal leaves configuration and local notification history in place so reinstalling does not lose state. To remove that data too, after removing the plugin run:

```bash
rm -rf ~/.config/omarchy/ntfy.json ~/.local/state/omarchy/ntfy
```

## Development

Validate and test from the repository root:

```bash
python3 -W error::ResourceWarning -m unittest discover -s test -p 'test_*.py'
node --test test/model.test.js
omarchy plugin validate "$PWD"
```

`test/mock_ntfy.py` provides a local ntfy-compatible HTTP fixture for stream, action, authentication, and media checks. Contribution setup and pull-request expectations are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Omartfy is licensed under the [MIT License](LICENSE). Copyright © 2026 Dailen Gunter.

The ntfy mask in `assets/ntfy-mask.svg` is copied from ntfy commit `4c2b69e0591b51d7ed7b2e71954f0f7be936b47f` and remains licensed under Apache-2.0. Its attribution and complete license text are preserved in the asset and [`third_party/ntfy-APACHE-2.0.txt`](third_party/ntfy-APACHE-2.0.txt).
