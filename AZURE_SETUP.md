# Relay CRM: Microsoft Entra ID app registration

*For your IT or Microsoft 365 administrator. About 10 minutes, done once for the whole team.*

## What Relay is and what it can access
Relay CRM is a **self-hosted team CRM** that runs on a server inside your organisation. It uses Microsoft Graph with **delegated permissions only**: each person connects their own mailbox, and Relay acts as that person. Relay can't read anyone's mailbox unless that person signs in and consents. The one exception is a shared mailbox that is explicitly configured and that the connecting person already has Full Access to.

| Permission (Microsoft Graph, **Delegated**) | Why |
|---|---|
| `User.Read` | Sign the person in and read their name and email address. |
| `Mail.Read` | Read Inbox, Sent Items and an optional "CRM" folder to log correspondence with external contacts. |
| `Mail.Send` | Send emails the person writes inside Relay. They appear in that person's Sent Items. |
| `Mail.Read.Shared` *(optional)* | Only if you use a shared "crm@" BCC/capture mailbox. |
| `offline_access` *(added automatically)* | Lets syncing continue between sign-ins using a refresh token. |

- **No application permissions.** Relay never has tenant-wide mailbox access.
- Refresh tokens are stored **encrypted** (Fernet / AES) in Relay's database on your server. The key file lives in the restricted data folder. People can disconnect at any time from *My profile*. Admins can disconnect anyone from *Admin → Users*. You can also revoke access centrally in Entra ID (*Enterprise applications → Relay CRM → Users and groups* or *Revoke sessions*).
- Logged email content stays in Relay's database on your server. Relay sends nothing to any third party.
- Sign-in happens on Microsoft's pages, so **MFA, PIV/CAC and Conditional Access policies apply as normal**.
- Relay ignores email between colleagues. It only logs mail exchanged with external addresses.

## Two ways people connect, and what each needs
| Feature | Needs |
|---|---|
| **Sign in with Microsoft** (single sign-on to Relay) and one-click **Connect with Microsoft** for mailboxes | A **Web** redirect URI and a **client secret** (steps 4–5) |
| **Connect with a code** (device sign-in), which works even without a secret or HTTPS | **Allow public client flows = Yes** (step 6) |

Doing both is recommended: SSO for convenience, and the code method as a fallback.

## Steps
1. Open the Microsoft Entra admin center:
   - Commercial / GCC: <https://entra.microsoft.com>
   - GCC High / DoD: <https://entra.microsoft.us>
2. **Identity → Applications → App registrations → New registration**
   - Name: `Relay CRM`
   - Supported account types: **Accounts in this organizational directory only (single tenant)**
   - Redirect URI: choose **Web** and enter `<Relay address>/auth/microsoft/callback`, for example
     `https://relay.yourfirm.com/auth/microsoft/callback` or `http://localhost:5055/auth/microsoft/callback`
     *(Microsoft only accepts `http://` for `localhost`. For team use, put Relay on HTTPS. See the Admin Guide.)*
   - Click **Register**.
3. From the **Overview** page, copy the **Application (client) ID** and the **Directory (tenant) ID**.
4. **Authentication.** Check that the Web redirect URI is listed. You can add more than one, for example both the HTTPS name and `localhost`.
5. **Certificates & secrets → New client secret.** Choose an expiry (for example 12 or 24 months) and copy the **Value** straight away. Put a reminder in the calendar to renew it before it expires.
6. **Authentication → Advanced settings → Allow public client flows: Yes → Save.** This enables the code method.
7. **API permissions → Add a permission → Microsoft Graph → Delegated permissions.** Tick `User.Read`, `Mail.Read`, `Mail.Send`, plus `Mail.Read.Shared` if you'll use a shared capture mailbox. Then click **Add permissions**.
8. **Grant admin consent for <your tenant>.** This is recommended, so users don't each see a consent prompt. It's required if your tenant blocks user consent.
9. *(Recommended)* **Enterprise applications → Relay CRM → Properties → Assignment required = Yes.** Then add the people or the group who should use Relay under *Users and groups*.

## In Relay (as a Relay administrator)
**Admin → Settings → Microsoft 365**
1. **Cloud:** Commercial, GCC, GCC High or DoD.
2. **Directory (tenant) ID** and **Application (client) ID.**
3. **Client secret** (the value from step 5). It's stored encrypted, and leaving the box blank later keeps the current secret.
4. **Server address**, exactly as people type it, for example `https://relay.yourfirm.com`. The page shows the redirect URI it expects. That URI must match the one in step 2 exactly.
5. Save. Then, under **Sign-in**, choose whether passwords, Microsoft, or both are allowed. Optionally enable **auto-provisioning** for your email domain(s).
6. Under **General**, switch Relay to **live mode**.

Each person then opens **My profile & mailbox → Connect with Microsoft** (or **Use a sign-in code instead**).

### Shared BCC / capture mailbox (optional)
1. Create a shared mailbox in Exchange, for example `crm@yourfirm.com`. Give **one** Relay user (often the admin) *Full Access*.
2. In Relay, go to **Settings → BCC capture**. Enter the address and pick that user as the *mailbox reader*.
3. That user disconnects and reconnects their mailbox once, so Relay can request `Mail.Read.Shared`.
4. Anyone on the team can now BCC or forward to `crm@yourfirm.com`. Relay credits the teammate who sent or forwarded each email.

## Cloud endpoints Relay contacts
Allow these through your proxy or firewall:

| Cloud | Sign-in authority | Graph endpoint |
|---|---|---|
| Commercial / GCC | `login.microsoftonline.com` | `graph.microsoft.com` |
| GCC High | `login.microsoftonline.us` | `graph.microsoft.us` |
| DoD | `login.microsoftonline.us` | `dod-graph.microsoft.us` |

## Troubleshooting
| Message | Fix |
|---|---|
| `AADSTS50011` redirect URI mismatch | The **Server address** in Relay plus `/auth/microsoft/callback` must exactly match a Web redirect URI in the app registration, including http/https, host name and port. |
| `AADSTS7000215` invalid client secret | The secret was copied wrong (copy the *Value*, not the *Secret ID*) or it has expired. Create a new one and paste it into Relay. |
| `AADSTS7000218` client_assertion or client_secret required | Turn on **Allow public client flows** (step 6) for the code method. |
| `AADSTS65001` consent required / "Need admin approval" | Grant admin consent (step 8). |
| `AADSTS50105` user not assigned | Assignment is required (step 9). Add the person or their group. |
| Sign-in works but nothing syncs | Make sure Relay is in **live mode**, the person's *Sync my mailbox* box is ticked, and look at **Sync history** for errors. |
