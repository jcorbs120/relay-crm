# Relay CRM: Administrator Guide

This guide is for the person who runs Relay for the team.

## 1. Where Relay runs
Relay is one server process that holds one database. Pick a machine that is **always on** and that your team can reach: an office Windows server, a small VM (Azure Government, AWS GovCloud or on-prem), or an always-on desktop for a very small team. For about 25 people, 2 vCPU, 2–4 GB RAM and a few GB of disk is plenty.

| Install method | Data folder |
|---|---|
| Windows installer (service) | `C:\ProgramData\RelayCRM` |
| `run.py` / launcher scripts | `./data` next to `run.py` (or `--data-dir`) |
| systemd unit | `/var/lib/relay-crm` |
| Docker | the `relay-data` volume (`/data` in the container) |

The data folder contains:

| File | What it is |
|---|---|
| `relay.db` | The database: every contact, email, note, user and setting |
| `secret.key`, `encryption.key` | Session signing key and the encryption key for mailbox tokens and the client secret. **Back these up with the database.** Without `encryption.key`, people simply reconnect their mailboxes and the admin re-enters the client secret. |
| `relay.conf` | Server settings: host, port, HTTPS certificate, proxy |
| `relay.log`, `service.log` | Logs, which rotate automatically |
| `backups/` | Daily automatic database snapshots |

The Windows installer restricts the data folder so only SYSTEM and Administrators can read it. On Linux the unit runs as a dedicated `relay` user with `UMask=0077`.

## 2. HTTPS (do this for anything beyond a trusted office LAN)
Choose one:
- **Certificate on Relay itself.** Get a certificate for the server name from your IT team or internal CA (for example `relay.yourfirm.local`) as PEM files. Then set `cert =` and `key =` in `relay.conf` and change the port to 443 or 8443 if you like. Restart Relay.
- **Reverse proxy.** IIS with ARR, nginx, Caddy or an Azure Application Gateway handles HTTPS and forwards to `http://127.0.0.1:5055`. Set `behind_proxy = yes` in `relay.conf`, and set host to `127.0.0.1` so Relay is only reachable through the proxy.

Either way, Relay switches to secure cookies and sends HSTS. Then set **Admin → Settings → Microsoft 365 → Server address** to the HTTPS URL and update the redirect URI in Entra ID.

## 3. Users
**Admin → Users**
- **Add user.** Enter name, work email (it should match their Microsoft 365 address) and role. Then pick how they sign in:
  - *One-time password:* Relay shows it **once**. Pass it on securely. They must change it at first sign-in.
  - *Microsoft only:* they click "Sign in with Microsoft". This needs the client secret and server address to be set.
- **Auto-provisioning** (*Settings → Sign-in*). Anyone who signs in with Microsoft from the listed domain(s) gets a *User* account automatically. Combine it with "Assignment required" in Entra ID to control exactly who can get in.
- **Other email addresses** (aliases). Add any extra addresses a person sends from, so Relay recognises them as a colleague and not a contact.
- **Changing a role or deactivating** takes effect immediately and ends the person's open sessions.
- **Reset password, Unlock, Sign out everywhere, Disconnect mailbox** are on each user's page.
- **Leavers:** *deactivate* them, which keeps their name on history. Reassign their open work with the bulk "Assign owner" action on Contacts, or delete the user and choose who inherits their contacts, opportunities and open tasks.
- Relay always keeps at least one active administrator. You can't demote, deactivate or delete the last one.

Sync from a former employee's mailbox stops as soon as they are deactivated. Their past emails stay on the contact timelines.

## 4. Settings worth reviewing
| Setting | Recommendation |
|---|---|
| Sign-in → password / Microsoft | Once everyone can use Microsoft sign-in, consider turning **passwords off**. Administrators can still use a password as a break-glass route. |
| Session timeout | 480 minutes by default. Shorten it for shared workstations. |
| Minimum password length | 12 by default. Passphrases of 16+ characters are accepted without complexity rules. |
| Permissions | Whether users may export CSV, import, and delete their own records. Read-only users can never change data. |
| Internal domains | Add partner or teaming domains whose people should never become contacts. Your own team's domains are already excluded. |
| Contact creation | Auto-create from sent and received mail, sent mail only, or off. Received-only contacts are flagged *needs review*. |
| Follow-up rules | Days before "Awaiting reply" and "Reply needed" tasks are created. Tasks go to the teammate involved. |
| Pipeline | Stage names and win probabilities. |

## 5. Backups and restore
- Relay takes a consistent snapshot every day and keeps them for *backup_keep_days* (14 by default). **Admin → Backups** lists them and lets you download any of them, take one now, or download the live database.
- **Also back up the data folder** with your normal server backup, or copy `backups/` off the machine on a schedule. A backup that sits on the same disk is not disaster recovery.
- **Restore:** stop Relay (`net stop RelayCRM`, or `systemctl stop relay-crm`). Then replace `relay.db` with the backup file, renaming it to `relay.db`, and delete any `relay.db-wal` and `relay.db-shm` files. Start Relay again.
- Backups contain email content. Store downloaded copies as carefully as the server.

## 6. Audit log
**Admin → Audit log** records sign-ins (including failures and lockouts), creates, edits, deletes, ownership changes, exports, imports, settings changes, user management, backups and mailbox connect/disconnect, with time, person and IP address. You can filter by person, action, record type, date or text, and export to CSV for your records or an auditor.

## 7. Upgrades
- **Windows:** run the newer installer. It stops the service, replaces the program files, keeps the data folder and restarts the service.
- **Other installs:** stop Relay, replace the program files (keep `data/`), then start Relay. The launchers update Python packages automatically.
- Database changes apply automatically at startup. Take a backup first anyway.

## 8. Code signing (Windows)
Unsigned installers trigger SmartScreen ("Windows protected your PC"). For a smooth rollout, sign them with your organisation's code-signing certificate:
- **Local build:** set `SIGN_PFX` (path to a .pfx) and `SIGN_PFX_PASSWORD`, then run `build_windows.ps1`. Both the exe and the installer are signed with timestamping.
- **GitHub Actions:** add repository secrets `SIGN_PFX_BASE64` (base64 of the .pfx) and `SIGN_PFX_PASSWORD`.
- If you don't have a certificate, IT can still approve the file hash in Defender / Intune, or users can click *More info → Run anyway*.

## 9. Ports and network
- Inbound: TCP **5055** by default (the installer adds a firewall rule for Domain and Private networks only), or whatever port you configure.
- Outbound: HTTPS to Microsoft sign-in and Graph (see `AZURE_SETUP.md`). Nothing else.
- Health check: `GET /healthz` returns `{"ok": true, "version": "2.0.0"}`. Use it for monitoring.

## 10. Troubleshooting
| Symptom | What to check |
|---|---|
| Team can't reach `http://SERVER:5055` | Is the service running (`services.msc` → Relay CRM)? Is the firewall rule present? Is `host = 0.0.0.0` set in `relay.conf`? |
| "Your form expired (security token)" | The session timed out or the server restarted. Reload the page and try again. |
| Someone is locked out | Wait 15 minutes, or go to **Admin → Users → (person) → Unlock**. |
| A mailbox stopped syncing | **Sync history** shows the error. Usually the refresh token was revoked or expired (password reset, Conditional Access). Ask the person to reconnect from *My profile*. |
| Colleague shows up as a contact | Add their address under that user's *Other email addresses*, or add the domain under *Internal domains*. Then delete the contact. |
| Everything is slow | Look at `relay.log`. Very large first syncs (long look-back) take a while. Later syncs only fetch new mail. |
| Logs | `relay.log` in the data folder. On Windows, also `service.log`. |
