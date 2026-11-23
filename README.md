# CTFd Badgify Extractor

Extract participant data from your CTF and award **[Badgify](https://badgify.eu)** digital badges & certificates — straight from the CTFd admin panel.

A CTFd plugin by **[Hack'olyte](https://github.com/HACK-OLYTE)** that turns the end of your event into a two-click routine: filter the players you want, then either export them (CSV/ZIP) or award them a Badgify badge automatically through the Badgify public API.

> ✅ Compatible with the latest version of CTFd (Users **and** Teams mode).

---

## ✨ Features

- **Two workflows, one plugin**
  - **Manual export** — browse, filter and download participant data as **CSV** / **ZIP**, or copy `username,email` to the clipboard.
  - **Badgify API award** — pick a badge, reuse the exact same filters, and award it to the selected participants automatically (emails sent by Badgify).
- **Powerful filters** — global list, **0% fail**, **TOP N**, **No hint**, **solved a given challenge**, excluded users, excluded domains, admin/hidden exclusion, challenge filter.
- **Badgify integration**
  - Save your API token **server-side** (never shown again, never sent back to the browser).
  - **Test connection** (`/ping`) on page load: shows your **organisation**, **token expiry**, **exposed badges** and **rate limits**.
  - **Badge list** with live metrics (awarded / claimed / pending / revoked).
  - Awarding is **batched** (50 recipients per request) to respect API limits.
- **Bilingual UI** — French / English toggle, remembered per browser.
- **Native CTFd look** — dark, responsive, mobile-friendly, keeps the classic CTFd admin header.

---

## 🔌 Compatibility

- **CTFd**: latest release (3.8.x and up), Users or Teams mode.
- **Python**: 3.11+ (same as CTFd).
- No extra Python dependencies — uses CTFd's stack and the standard library only.

---

## 📦 Installation

```bash
# From your CTFd installation
cd CTFd/plugins
git clone https://github.com/HACK-OLYTE/ctfd-badgify-extractor.git
# then restart CTFd
```

Or with Docker, drop the folder into `CTFd/plugins/` and restart the stack:

```bash
docker compose restart ctfd
```

The plugin self-registers its database tables and assets on boot — nothing else to configure at the system level. It uses **no absolute paths**, so it works from any folder name on any CTFd install.

Open it from the CTFd admin panel at: **`/admin/badges_extraction`**

---

## ⚙️ Configuration (Badgify API)

1. Create / retrieve your API token on **[badgify.eu/org/api](https://badgify.eu/org/api)**
   *(your organisation must be granted API access beforehand).*
2. In the plugin, open **Export API Badgify → Connexion API**, paste the token (and optionally the API base URL, default `https://badgify.eu/api/v1/public`) and save.
3. The token is stored **server-side only** and is never returned to the UI. Click **Test the API** (or just reload — it auto-tests) to confirm.

> ⚠️ **Server IP allow-list** — Badgify requires calls to originate from an authorised site. All API calls are made **from the CTFd server**, so declare your server's **public outbound IP** in your Badgify authorised sites, otherwise you'll get a `403`.

---

## 🚀 Usage

### Manual export
`/admin/badges_extraction` → **Export à la main**. Pick a tab (Global, 0% Fail, TOP N, No Hint, Solved challenge…), apply filters, then **Download CSV/ZIP** or **Copy (username,email)**.

### Award a Badgify badge
`/admin/badges_extraction` → **Export API Badgify** → **Reload badges**, click **Award** on a badge. You land on a filtered view (same engine as the manual export) with a floating **"Award to badge « X » (via API)"** button that awards everyone currently matching your filters.

---

## 🔒 Security

- Every route is **`@admins_only`** (authenticated admin required).
- Database access is **SQLAlchemy ORM** only (no raw SQL); numeric parameters are integer-cast — no SQL injection surface.
- Per-user config is scoped to the **session user id** (no IDOR); exports are generated **in memory** (no filesystem path traversal).
- User- and API-supplied data is **HTML-escaped** before rendering (no stored/reflected XSS).
- The **Badgify token lives only in the backend database** and is never exposed in any API response or in the UI.
- **CSV / spreadsheet exports are hardened against formula injection** ([OWASP CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)): every cell beginning with `=`, `+`, `-`, `@`, tab or carriage-return is neutralized across all exports.
- The `badge_id` sent to the Badgify award endpoint is **validated against a strict charset** before use.

---

## 🏷️ About Badgify

[Badgify](https://badgify.eu) is a free platform for **Open Badges 2.0 & 3.0** — issue, store and share digital badges and certificates. Organisers import their participants and Badgify sends badges & certificates by email; recipients keep them in a personal portfolio. This plugin connects your CTFd event to Badgify's public API so winners get their badges automatically.

---

## 📄 License & Credits

- Author: **Hack'olyte** — <https://github.com/HACK-OLYTE>
- Badge platform: **Badgify** — <https://badgify.eu>
- Security: big thanks to **[@PercevalFox](https://github.com/PercevalFox)** for responsibly reporting the CSV formula-injection vulnerability ([issue #1](https://github.com/HACK-OLYTE/ctfd-badgify-extractor/issues/1)) — now fixed. 🙏
- Released under the MIT License.
