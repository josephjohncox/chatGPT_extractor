# Get ChatGPT session tokens

Get credentials from the browser where you are signed in to the ChatGPT account you want to export. Save them only in the repository's private `mise.local.toml`. Offline import and `mise run index` do not need credentials.

## Prepare the local file

If `mise.local.toml` does not exist, run this from the repository on macOS/Linux:

```sh
cp -n mise.local.toml.example mise.local.toml
chmod 600 mise.local.toml
```

If it already exists, keep it. Edit the matching keys under its existing `[env]`; do not add another table or duplicate keys.

## Choose a method

| What you have | Method |
|---|---|
| One `__Secure-next-auth.session-token` cookie | Copy its value into `CHATGPT_SESSION_TOKEN` |
| Cookies ending in `.0` and `.1` | Copy each value into `CHATGPT_SESSION_TOKEN_0` and `_1` |
| A browser session response containing `accessToken` | Use the helper or copy that value into `CHATGPT_ACCESS_TOKEN` |

Use one method at a time. An OpenAI Platform API key (`sk-…`), project ID, and workspace ID are not session tokens.

## 1. Open the browser's cookie view

1. Open [chatgpt.com](https://chatgpt.com/) in the browser profile where you are signed in. A new private/incognito window will not reuse that login.
2. Sign in to the account you want to export. If you use multiple workspaces, select the intended workspace in ChatGPT.
3. Open Developer Tools:
   - **macOS, Chrome/Edge:** press **Command–Option–I**.
   - **Windows/Linux, Chrome/Edge:** press **Ctrl–Shift–I** or **F12**.
4. Select **Application**. If that tab is hidden, use the **»** tab menu.
5. In the left sidebar, expand **Storage → Cookies**, then select **https://chatgpt.com**. Depending on the browser version, Cookies may appear directly under Application.
6. Filter the cookie table for **session-token**.

In Firefox, open Developer Tools and use **Storage → Cookies → https://chatgpt.com** instead. The cookie names and configuration mapping below are the same.

## 2. Copy the matching cookie values

In the cookie table, use the **Name** column to identify the cookie and the **Value** column to copy it. Some versions also show the selected cookie's full value in a details pane. Copy the complete value, not the truncated text displayed in a narrow cell.

Copy **only the value**. Do not include the cookie name, `Cookie:`, `Bearer `, expiry date, or the rest of a request header. Do not concatenate split cookies, decode their contents, or add spaces/line breaks.

All snippets below belong under the **existing `[env]`** in `mise.local.toml`. Replace the matching keys rather than adding duplicate keys or another `[env]` table. Keep your output directory, project settings, and other unrelated configuration.

### If there is one cookie

Find the exact name:

```text
__Secure-next-auth.session-token
```

Copy its value between the quotes for `CHATGPT_SESSION_TOKEN`. Clear the other authentication fields:

```toml
CHATGPT_ACCESS_TOKEN = ""
CHATGPT_SESSION_TOKEN = "single-cookie-value"
CHATGPT_SESSION_TOKEN_0 = ""
CHATGPT_SESSION_TOKEN_1 = ""
```

Replace `single-cookie-value` with the actual value. A single cookie in `_0` is also supported, but the dedicated field above is clearer.

### If there are two cookie parts

Match the names to the configuration exactly:

| Browser cookie name | Configuration key |
|---|---|
| `__Secure-next-auth.session-token.0` | `CHATGPT_SESSION_TOKEN_0` |
| `__Secure-next-auth.session-token.1` | `CHATGPT_SESSION_TOKEN_1` |

Copy each complete value into its own field. Clear both the bearer token and the single-cookie field:

```toml
CHATGPT_ACCESS_TOKEN = ""
CHATGPT_SESSION_TOKEN = ""
CHATGPT_SESSION_TOKEN_0 = "value-from-cookie-ending-.0"
CHATGPT_SESSION_TOKEN_1 = "value-from-cookie-ending-.1"
```

Replace the two example values. These are parts **0 and 1**, not parts 1 and 2. The exporter sends them as two named cookies; it does not join them into one value.

If you find only `.1`, refresh ChatGPT and check that your filter has not hidden `.0`. If there are more than two parts or different authentication-cookie names, use the bearer-token method below; this tool's cookie configuration supports the exact single-cookie name or the `.0`/`.1` pair shown here.

Cookies marked **HttpOnly** are visible in the browser's Developer Tools but unavailable to page JavaScript. The helper cannot automatically extract those cookie values.

## 3. Set the workspace account ID, if needed

For a personal/default account, start with `CHATGPT_WORKSPACE_ID = ""`.

For a Business/Team/Enterprise workspace:

1. Keep the intended workspace selected in ChatGPT.
2. In Developer Tools, open **Network** and choose **Fetch/XHR** if available.
3. Reload the ChatGPT page, then filter requests for **backend-api**.
4. Select a request such as **conversations**.
5. Open **Headers → Request Headers** and find **ChatGPT-Account-ID**; header names may appear lowercase.
6. Copy only that header's value into `CHATGPT_WORKSPACE_ID` in your private file.

Do not use a `g-p-…` project ID or paste a URL into this field. If no account header is present, leave it blank and check the probe result rather than guessing an ID. Use a separate output directory per workspace.

## Match the browser's User-Agent

The exporter uses `CHATGPT_USER_AGENT` for both session exchange and backend requests. Its default is a desktop Chrome string. The browser helper copies your actual `navigator.userAgent` into the downloaded TOML automatically.

To set it manually, evaluate this in Developer Tools → Console on the signed-in ChatGPT tab:

```js
navigator.userAgent
```

Copy that string value, without the console's surrounding quotes, into `CHATGPT_USER_AGENT` under the existing `[env]` in your private file. Do not include the `User-Agent:` header name. This field may contain semicolons and spaces, but not line breaks.

A controlled live test changed only this header: the old generic `Mozilla/5.0` received a 403 challenge, the desktop Chrome value received 200, and restoring the generic header restored 403. Matching the User-Agent resolved that failure; it does not reproduce the browser's entire network context or guarantee access in every environment.

## 4. Check authentication without exporting

Save `mise.local.toml`. From the repository on macOS/Linux, run:

```sh
chmod 600 mise.local.toml
mise trust mise.local.toml
mise run probe
```

The probe makes read-only requests; it does not start a bulk export. The CLI reports `"authentication": "ok"` once a backend endpoint responds successfully. Check the conversation-listing result as well: success on another endpoint is not proof that conversations are accessible. `token_source` shows `configured_bearer` or `session_exchange` without exposing the token. A bearer token with no successful backend response remains `unverified`; a completed cookie exchange without backend success is `session_ok_backend_unverified`. An unavailable endpoint can make the command exit nonzero even when another endpoint succeeds.

Do not paste a token, full session response, cookie-table screenshot, credential file, or raw network capture into an issue or chat. The helper's redacted diagnostic report is separate from the downloaded credential file.

## Alternative: get a bearer token

A bearer token avoids copying session-cookie parts. It can expire independently; the exporter does not fall back from an expired configured bearer token to cookies.

### Use the helper

1. Open the repository's [`scripts/probe-chatgpt.js`](../scripts/probe-chatgpt.js) in your editor and copy the entire script. On macOS, `pbcopy < scripts/probe-chatgpt.js` copies it from a terminal in the repository.
2. In your signed-in **https://chatgpt.com** tab, open Developer Tools and select **Console**.
3. Paste and run the script. It requests `/api/auth/session` using that browser session; it does not read HttpOnly cookies.
4. Enter the optional workspace account ID, or leave it blank for the personal/default account.
5. Accept the credential-file download and enter the export output directory. The browser saves `mise.local.toml` through its normal download mechanism.
6. If your repository already has `mise.local.toml`, merge the downloaded keys into its existing `[env]`. Keep unrelated settings and any intentional index-generation preference. On a fresh checkout, use the downloaded file as the local file.
7. Run the authentication check above. Delete any redundant downloaded credential file after saving the values locally.

The download also copies `navigator.userAgent` into `CHATGPT_USER_AGENT`. Its comments document output/index settings and the manual cookie alternatives. The console report stays redacted. Generating a token in the browser does not prove it will work outside that browser.

### Copy a bearer token manually

1. In the signed-in browser, open **https://chatgpt.com/api/auth/session**.
2. Find the JSON field named **accessToken**. Copy only its string value, without the JSON quotes. Do not copy the whole response or a different token field.
3. Put it between the TOML quotes below and clear the cookie fields:

```toml
CHATGPT_ACCESS_TOKEN = "accessToken-field-value"
CHATGPT_SESSION_TOKEN = ""
CHATGPT_SESSION_TOKEN_0 = ""
CHATGPT_SESSION_TOKEN_1 = ""
```

Replace `accessToken-field-value` with the actual value, save the file, and run `mise run probe`.

## If the check fails

- **No session cookie or no `accessToken`:** confirm that this browser profile is signed in to ChatGPT, refresh the tab, and repeat the copy steps. Do not substitute unrelated cookies.
- **Missing `.0` or invalid characters:** copy the complete Value cell again. Keep cookie names, header prefixes, whitespace, and screenshots out of the TOML values.
- **An old bearer token still takes precedence:** clear `CHATGPT_ACCESS_TOKEN` before testing cookies. If a single-cookie value remains, clear it before testing split cookies.
- **HTTP 401 or expired credentials:** sign in again if necessary, then replace all values for the chosen method from the same current browser session. There is no fixed lifetime assumed by the exporter.
- **HTTP 403 or “Just a moment…”:** this can be browser/Cloudflare gating, a workspace/access restriction, or rejected credentials. It does not by itself prove expiry. Check `CHATGPT_USER_AGENT` against `navigator.userAgent` first, then sign-in and the account header. Use the helper from the normal signed-in tab for a browser-side diagnostic. A fresh token is not a guaranteed fix.

### If the CLI detects a Cloudflare browser challenge

The CLI reports this only when the server sends `cf-mitigated: challenge`, Cloudflare's [documented challenge-page indicator](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/). It is a browser-challenge response, not a verdict that the split cookie values are invalid. Correct cookie names and values alone may not let a standalone Python request complete the browser challenge.

1. Run `scripts/probe-chatgpt.js` in your normal signed-in ChatGPT tab, as described above.
2. Check the redacted report. The authentication fields include `status`, `browser_challenge`, and `has_access_token`; backend entries also include `browser_challenge`.
3. Accept the credential-file download and merge `CHATGPT_USER_AGENT` into the existing private file. Keep cookie authentication if preferred. Copying `CHATGPT_ACCESS_TOKEN` as well selects bearer mode and skips the cookie exchange, but is not required for the User-Agent correction.
4. Run `mise run probe` again. Both cookie-only and bearer modes passed the live checks after the header correction. If a challenge persists with a matching header and current credentials, share the redacted report for a browser-versus-CLI comparison. A different transport may be needed in some environments, but the challenge header alone does not prove that.

Share only the inspected, redacted report if further diagnosis is needed, never the downloaded TOML or session response. The User-Agent setting changes only that HTTP header. No browser automation, TLS impersonation, challenge solver, or automatic credential fallback is applied.

You can still browse previous exports with `mise run index` or import a downloaded archive without web authentication.
