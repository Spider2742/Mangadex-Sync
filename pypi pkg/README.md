# MangaDex Sync

Export **and import** your MangaDex library to/from **MyAnimeList** and **AniList** — with scores, statuses, and optional chapter progress — all from a single clean app.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)
![Flask](https://img.shields.io/badge/Flask-3.0-lightgrey?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)
![PyPI](https://img.shields.io/pypi/v/mangadex-sync?style=flat-square)
![Downloads](https://img.shields.io/pypi/dm/mangadex-sync?style=flat-square)

---

## Features

- **Single app** — Export, Import, and Convert in one place, no switching between scripts
- **Import** — Restore your library from a MangaDex JSON backup or a MAL/AniList XML file
- **Fast mode** — Exports titles + status in minutes (recommended)
- **Deep mode** — Also fetches your last read chapter per manga (slower)
- **MAL XML** — Ready to import at `myanimelist.net/import.php` (includes `.gz`)
- **AniList XML** — Same format, ready to import at AniList
- **JSON backup** — Full local backup of your library data
- **Scores** — Fetches your MangaDex ratings and carries them over
- **Resume** — Saves a checkpoint so it can pick up if interrupted
- **Skipped list** — Shows exactly which manga had no MAL ID
- **Export history** — Logs every export with timestamp and file list
- **Dry run mode** — Simulate without writing any files
- **Native window** — Opens as a desktop app via pywebview (or falls back to browser)

---

## Requirements

- Python 3.10 or newer
- A MangaDex account
- A [MangaDex API Personal Client](https://mangadex.org/settings) (free to create)

---

## Installation

```bash
pip install mangadex-sync
```

Everything including the native desktop window is included — no extra steps needed.

---

## Usage

```bash
mangadex-sync
```

This opens a native desktop window. You can also open `http://localhost:7337` in any browser — useful for accessing it from another device on the same network.

---

## Step-by-step Guide

### 1. Get your MangaDex API credentials

1. Go to [mangadex.org/settings](https://mangadex.org/settings)
2. Scroll to **API Clients** → click **Create**
3. Give it any name, set type to **Personal**
4. Copy your **Client ID** and **Client Secret**

### 2. Export your library

1. Open the app and go to the **Export** tab
2. Fill in:
   - **Client ID** and **Client Secret** (from step 1)
   - **Username** and **Password** (your MangaDex login)
   - **Save folder** — where to save the output files
3. Choose **Fast** or **Deep** mode (Fast is recommended)
4. Click **⚡ Extract Entire Library**

> **MAL User ID and MAL Username are optional.** You do not need to fill them in to generate a working XML file. MAL identifies you by your login session when you upload — the ID and username fields in the XML header are just metadata and are ignored during import. Leave them blank if you don't know them.

### 3. Convert to XML

After export finishes, the **Convert** tab auto-fills with your exported files. Just:

1. Click **⚡ Generate XML Files**
2. Files are saved to your chosen folder

### 4. Import to MAL / AniList

- **MAL:** Go to [myanimelist.net/import.php](https://myanimelist.net/import.php) and upload the `mal_*.xml` file
- **AniList:** Go to [anilist.co/settings/import](https://anilist.co/settings/import) and upload the `anilist_*.xml` file

### 5. Import back to MangaDex

Use the **Import** tab to restore your library from:
- A `mdex_*.json` backup file (exported by this app)
- A MAL XML file (`mal_*.xml`)
- An AniList XML file (`anilist_*.xml`)

1. Browse or paste the path to your file
2. Optionally enable **Import Scores** to restore your ratings
3. Click **Start Import**

---

## Fast vs Deep Mode

| | Fast | Deep |
|---|---|---|
| Speed | ⚡ Minutes | 🐢 15–60+ min |
| Titles & status | ✓ | ✓ |
| Ratings/scores | ✓ | ✓ |
| Last read chapter | ✗ | ✓ |
| Recommended | ✓ | Only if you need progress |

Deep mode fetches every individual chapter you've ever read to find your last one per manga. This can mean 5,000–10,000+ API calls for a large library, which is why Fast mode exists.

---

## Output Files

For each status group (reading, completed, etc.) the app creates:

| File | Description |
|---|---|
| `mdex_{status}_{timestamp}.xlsx` | Raw export data — used by Convert tab |
| `mdex_{status}_{timestamp}.json` | Full JSON backup — used by Import tab |
| `mal_{status}_{timestamp}.xml` | MAL import file |
| `mal_{status}_{timestamp}.xml.gz` | Compressed version (also accepted by MAL) |
| `anilist_{status}_{timestamp}.xml` | AniList import file |

---

## Resume

If the export is interrupted (crash, network error, you stopped it), the app saves a checkpoint after each completed status group. Click **▶ Resume** on the Export tab to continue from where it left off.

To start fresh, go to **Settings → Clear Checkpoint**.

---

## Troubleshooting

**"Authentication failed"**
- Double-check your Client ID, Client Secret, username, and password
- Make sure your API client is set to **Personal** type and is **approved** on MangaDex
- Try the **✓ Test Credentials** button first

**"Failed to set status" during import**
- Make sure your API client exists and is approved at [mangadex.org/settings](https://mangadex.org/settings)
- Check that your credentials are correct

**Native window doesn't open**
- The app will fall back to your browser automatically with instructions on how to fix it
- On Linux you may need to install a GTK system package — the app will tell you the exact command for your distro

**Manga missing from MAL after import**
- Those titles are listed in the **Skipped Manga** section — they have no MAL ID on MangaDex
- Add them manually on MAL

**Slow export speed**
- The API enforces rate limits — the app already waits the minimum required between requests
- Deep mode is inherently slow for large libraries — use Fast mode if chapter progress isn't critical

---

## Dependencies

| Package | Purpose |
|---|---|
| `flask` | Local web server |
| `requests` | MangaDex API calls |
| `pandas` | Excel file read/write |
| `openpyxl` | Excel engine for pandas |
| `pywebview` | Native desktop window |
| `PyQt6` + `PyQt6-WebEngine` | Qt backend for pywebview |
| `qtpy` | Qt abstraction layer |

---

## Credits

Based on the original export scripts by [Seriousattempts](https://github.com/Seriousattempts/MangaDex). Rewritten and extended with a web UI, Fast/Deep mode, import support, resume, score export, and AniList output.
