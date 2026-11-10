# Relay CRM

A desktop CRM for public-sector business development that **updates itself from your Outlook / Microsoft 365 email**. It works like ACT!, but without the manual data entry.

Everything runs on your own computer. Your data is kept in one file in `%LOCALAPPDATA%\RelayCRM` and is never sent to a cloud service. The only outside connection is to Microsoft 365, to read and send *your own* mail.

---

## Install (Windows 10/11)

1. Run **`RelayCRM-Setup-x.y.z.exe`**.
   * No admin rights are needed. It installs for your Windows user only.
   * If Windows shows **"Windows protected your PC"**, click **More info → Run anyway**. This appears because the app isn't code-signed yet.
2. Open **Relay CRM** from the desktop icon or the Start menu. It opens in its own window.

If installers are blocked on your PC, use **`RelayCRM-Portable-x.y.z.zip`** instead. Unzip it anywhere and double-click `RelayCRM.exe`.

> Relay CRM uses Microsoft Edge WebView2 to draw its window. WebView2 is built into Windows 10 and 11. If it's missing, Relay CRM opens in your web browser instead and works the same way.

## First run: demo mode

The CRM opens with sample agencies, contacts and opportunities. Click **⟳ Sync email** to watch it work against a simulated mailbox. It will:

* log a reply from Maria Lopez on her timeline and close the "Awaiting reply" task,
* create **Daniel Okafor (DOT)** and **Priya Nair (State of Michigan)** as new contacts, with their organizations,
* file a forwarded email under the original sender (Tom Becker, Fairfax County),
* skip the newsletter, the no-reply sender and your colleague,
* open "Reply needed" / "Awaiting reply" follow-ups for threads that have gone quiet.

## Going live with your Outlook mailbox

1. **IT does a 5-minute app registration once** for your organization. The steps are in *Microsoft 365 Setup for IT* in the Start menu (`AZURE_SETUP.md`). IT gives you two IDs.
2. In Relay CRM, open **Settings → Microsoft 365**.
   * Pick your Microsoft cloud: Commercial, GCC, GCC High or DoD.
   * Paste in the **Directory (tenant) ID** and the **Application (client) ID**.
   * Click **Save IDs**.
3. Click **Sign in to Microsoft 365**.
   * A code appears. Open the link shown, enter the code, and sign in as usual. MFA, PIV and CAC all work.
   * Relay CRM never sees your password.
4. Click **Switch to live mode**. Leave "remove the sample data" ticked.
   * The first sync reads the last 30 days of Inbox and Sent Items. You can change this in Settings.
   * After that it syncs every 5 minutes while the app is open.

## What happens automatically

| When… | Relay CRM… |
|---|---|
| You email someone for the first time | Creates the contact. Creates their organization from the email domain and labels it Federal, State, Local, Education, Commercial or Nonprofit. |
| Someone new emails you | Creates the contact and flags it **for review** on the dashboard (Keep / Delete). |
| Any email to or from a known contact | Logs it on their timeline, links it to their open opportunity, and updates *last contacted*. |
| Email has gone both ways | Moves the contact from **Lead** to **Prospect**. |
| Your email gets no reply for 5 days | Opens an **Awaiting reply** task, which closes itself when they answer. |
| You haven't answered someone for 2 days | Opens a **Reply needed** task, which closes itself when you reply. |
| Newsletters, no-reply senders, colleagues at your own domain | Ignored. |

**BCC and forward capture.** BCC `you+crm@yourdomain` on an email, or forward an email to yourself. Then create an Outlook rule that moves those messages into a folder called **CRM**. Relay reads that folder too. Forwarded emails are filed under the *original* sender.

## Features

* **Contacts and organizations**, with timeline, notes, call and meeting logging, tags, search, and CSV export.
* **Pipeline board**: Lead → Qualified → Proposal → Submitted → Awarded / Lost. Drag cards between stages. Each opportunity holds a solicitation number, contract vehicle, value, probability and close date.
* **Tasks and reminders**: views for Today, Overdue, Upcoming and Email follow-ups, with snooze.
* **Write email**: sends from your Outlook account using templates (`{first_name}`, `{org}` and so on). It is logged automatically and can set a "remind me if no reply" task.
* **Import**: CSV or Excel from Outlook, ACT!, Salesforce, LinkedIn or event lists. Columns are matched automatically, there are no duplicates, and each batch can be tagged.
* **Reports** (filter by date and organization type, export to CSV, print):
  * Pipeline
  * Forecast
  * Win/loss
  * Activity
  * Contact engagement
  * Stale contacts
  * New contacts
  * Organization summary
  * Tasks
* **Backup and restore** in one click (Settings → Your data), plus an optional password lock.

## Troubleshooting

* **Logs:** `%LOCALAPPDATA%\RelayCRM\relay.log`
* **"Sign-in expired":** go to Settings and click *Sign in again*.
* **"Allow public client flows":** IT needs to turn that on in the app registration (see AZURE_SETUP.md).
* **Moving to a new PC:** download a backup on the old PC, install on the new PC, then use Settings → Restore backup.
