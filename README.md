# Relay CRM 2.0: Team Edition

Relay is a private, self-hosted CRM for business-development teams that work in Microsoft 365. It works like ACT!: contacts, organisations, a pipeline from Lead to Awarded, tasks, notes, call logs and reports. Relay also keeps itself up to date from your team's email. When someone on the team emails a client, Relay logs the email against the contact, creates the contact if it's new, files it under the right agency and opens or closes follow-up tasks. Nobody has to type it in.

Relay runs as **one shared server for the whole team**. Install it once on an office server, a VM or an always-on PC. Everyone then opens it in their browser and signs in with their own account.

---

## What the team gets

**Everyone sees everything, and every record has an owner.** Contacts, organisations, opportunities and tasks belong to a person. Each list has a *Mine / Team / Unassigned / a colleague* switch. Everyone's default view can be "Mine" or "Team" (they choose in *My profile*).

**Each person connects their own mailbox.** Relay reads each person's Inbox, Sent Items and an optional "CRM" folder using Microsoft's delegated permissions. Every logged email shows who on the team sent or received it. A contact page shows *who on the team knows them*. Email between colleagues is never logged, and colleagues are never turned into contacts. If the same email sits in two colleagues' mailboxes, Relay logs it once.

**BCC / forward capture.** Anyone can BCC or forward an email to a shared address (for example `crm@yourfirm.com`) or drop it into their own Outlook "CRM" folder. Relay logs it and credits the person who sent or forwarded it.

**Roles.**

| | Administrator | User | Read-only |
|---|---|---|---|
| See all contacts, pipeline, tasks, reports | ✓ | ✓ | ✓ |
| Add / edit contacts, opportunities, tasks, notes, calls | ✓ | ✓ | |
| Send email from Relay, connect own mailbox | ✓ | ✓ | own mailbox only |
| Export CSV | ✓ | if allowed in Settings | if allowed in Settings |
| Import CSV / Excel | ✓ | if allowed in Settings | |
| Delete records | ✓ | own records, if allowed | |
| Manage users, settings, backups, audit log | ✓ | | |

**Reports, overall or per person:** pipeline by stage, weighted forecast, win/loss, email and call activity, contact engagement, stale relationships, new contacts, organisation summary, open tasks, and *team activity by person*. Every report exports to CSV.

**Security for government work.**
- Sign-in uses Microsoft (MFA, PIV/CAC and Conditional Access all apply) or Relay passwords. Passwords are hashed with PBKDF2-SHA256 at 600,000 iterations.
- Accounts lock after repeated failed attempts. Idle sessions time out. A session can be ended remotely by its owner or by an admin.
- Every form has CSRF protection, and Relay sends strict browser security headers (CSP, frame-deny, HSTS on HTTPS).
- Mailbox tokens and the client secret are encrypted at rest.
- An audit log records sign-ins, changes, exports and admin actions.
- Relay takes daily automatic database backups.
- Delegated Graph permissions only. No data leaves your server except calls to Microsoft 365.

---

## Install

### Windows server or PC (recommended)
1. Run **`RelayCRM-Setup-2.0.0.exe`** as an administrator. Leave the defaults ticked. That installs Relay as a Windows service that starts with the machine, opens the firewall port (5055) and creates shortcuts.
2. The browser opens **http://localhost:5055**. Create the first administrator account.
3. Tell your team the address: `http://SERVERNAME:5055`. The console window also shows the IP address.

The database, keys, logs and backups are stored in `C:\ProgramData\RelayCRM`, which only Administrators and SYSTEM can read.

> **Building the installer.** On a Windows machine with Python 3.11 and Inno Setup 6, run
> `powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows.ps1`. You can also push the project to GitHub and run the **Build Windows installer** action. Either way, the installer appears as a downloadable artifact. See *Code signing* in the Admin Guide to avoid SmartScreen warnings.

### Without the installer (any OS, Python 3.10+)
Double-click **`Start Relay CRM (Windows).bat`** or **`start_relay_mac_linux.command`**. On first run it sets up a private Python environment, which takes about a minute. It then starts the server and opens your browser. Keep the window open while people are using Relay.

```
python run.py                 # team server on port 5055 (default)
python run.py --local         # this computer only
python run.py --cert c.pem --key k.pem      # HTTPS
python run.py --behind-proxy  # behind IIS / nginx / Azure App Gateway
python run.py --data-dir D:\RelayData
```
The same options can be set in `relay.conf` inside the data folder.

### Linux server / Docker
- **systemd:** see `deploy/linux/relay-crm.service`. The install steps are in the file header.
- **Docker:** run `docker compose up -d`. Data lives in the `relay-data` volume.

---

## First steps after installing
1. **Admin → Settings → General.** Enter your firm name. Relay starts in **demo mode** with sample contacts and two sample teammates, so you can explore safely. When you're ready, switch to **live mode**. The demo data is removed and your real data starts empty.
2. **Admin → Settings → Microsoft 365.** Register Relay in Entra ID (about 10 minutes; see `AZURE_SETUP.md`) and paste in the IDs. Add the client secret and the server address to switch on **Sign in with Microsoft**.
3. **Admin → Users → Add user** for each colleague. Choose either a one-time password or "Sign in with Microsoft only". You can also switch on *auto-provisioning*, so anyone from your email domain who signs in with Microsoft gets a User account.
4. Each person opens **My profile & mailbox → Connect**. Relay starts logging their email within minutes.
5. *(Optional)* Create a shared mailbox such as `crm@yourfirm.com` and set it under **Settings → BCC capture** for team-wide BCC logging.

Already used Relay 1.x on your own? Copy your old `data/relay.db` into the new data folder before first start. The setup wizard detects it and gives every existing record to the administrator you create.

---

## Day-to-day
- **Dashboard:** your tasks due, contacts needing review, pipeline value, recent email, and a *team at a glance* leaderboard.
- **Contacts:** search, filter by owner, status, organisation, tag or "needs review", and bulk-assign owners. A contact page shows the full email, call and note timeline with who on the team was involved.
- **Pipeline:** drag cards between stages. Each card shows its owner. Probabilities drive the weighted forecast.
- **Tasks:** assign them to anyone. "Awaiting reply" and "Reply needed" tasks are created and closed automatically from email.
- **Compose:** send from your own mailbox using templates with mail-merge fields. The email is logged automatically.
- **Import:** CSV or Excel, with column mapping, duplicate handling and an owner for the imported records.

## Files
| Path | What |
|---|---|
| `run.py` | Starts the server |
| `crm/` | Application code |
| `templates/`, `static/` | Web pages, styles and scripts (Chart.js is bundled, so no CDN is needed) |
| `deploy/windows/` | PyInstaller spec, Inno Setup script, build script |
| `deploy/linux/` | systemd unit |
| `Dockerfile`, `docker-compose.yml` | Container deployment |
| `tests/` | Automated tests (`python -m pytest -q tests`) |
| `ADMIN_GUIDE.md` | Running Relay for a team: users, security, backups, upgrades, troubleshooting |
| `AZURE_SETUP.md` | Microsoft Entra ID app registration (for IT) |
