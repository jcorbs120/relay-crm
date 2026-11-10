# Microsoft 365 setup for IT (one time, about 5 minutes)

Relay CRM is a desktop app. It reads and sends mail **only for the user who signs in**, using delegated Microsoft Graph permissions. It has no client secret, no application permissions and no server. Tokens are stored only on the user's PC.

## 1. Register the app

1. Open the Entra admin center (formerly Azure Active Directory):
   * Commercial and GCC: https://entra.microsoft.com
   * GCC High and DoD: https://entra.microsoft.us
2. Go to **Identity → Applications → App registrations → New registration**.
   * **Name:** `Relay CRM`
   * **Supported account types:** *Accounts in this organizational directory only*
   * **Redirect URI:** leave empty
   * Click **Register**.
3. Copy the **Application (client) ID** and the **Directory (tenant) ID** from the Overview page.

## 2. Allow device sign-in

Go to **Authentication → Advanced settings → Allow public client flows → Yes**, then click **Save**.

## 3. API permissions (delegated)

1. Go to **API permissions → Add a permission → Microsoft Graph → Delegated permissions**.
2. Add:
   * `User.Read`
   * `Mail.ReadWrite` (needed to read mail and to create the draft that is then sent)
   * `Mail.Send`
3. Optional: click **Grant admin consent** so users aren't prompted to consent. Some tenants require this.

## 4. Give the user

* the Directory (tenant) ID,
* the Application (client) ID,
* which cloud they're on: Commercial, GCC, GCC High or DoD.

The user enters these in Relay CRM under **Settings → Microsoft 365** and signs in with the device code. Conditional Access, MFA, PIV and CAC are all honoured, because sign-in happens on Microsoft's own page.

## Security notes

* **Endpoints** (cloud: sign-in host / Graph host):
  * Commercial and GCC: `login.microsoftonline.com` / `graph.microsoft.com`
  * GCC High: `login.microsoftonline.us` / `graph.microsoft.us`
  * DoD: `login.microsoftonline.us` / `dod-graph.microsoft.us`
* **Data at rest:** a SQLite file plus an MSAL token cache in `%LOCALAPPDATA%\RelayCRM`, protected by the user's Windows profile. Turn on BitLocker or your standard disk encryption.
* **Network:** the UI is served on `127.0.0.1` only. It rejects requests that don't come from localhost and requires a CSRF token on every change.
* **Revoking access:** remove the user's consent, or delete the app registration.
