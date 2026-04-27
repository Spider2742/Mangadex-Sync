# MangaDex Sync

Export **and import** your MangaDex library to/from **MyAnimeList** and **AniList** — with scores, statuses, and optional chapter progress — all from a single clean app.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)
![Flask](https://img.shields.io/badge/Flask-3.0-lightgrey?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)
![PyPI](https://img.shields.io/pypi/v/mangadex-sync?style=flat-square)
![Downloads](https://img.shields.io/pypi/dm/mangadex-sync?style=flat-square)

---

## What is this?

MangaDex Sync is a desktop app that lets you:

- **Export** your entire MangaDex reading library to MAL/AniList XML or a local JSON backup
- **Import** that library back into MangaDex from a JSON, MAL, or AniList file
- **Convert** between formats in one click

It runs as a local web app and opens in a native desktop window (via pywebview) or falls back to your browser automatically.

---

## Repo Structure

```
mangadex-sync/
├── standalone/          # Single-file version — just run python mangadex_sync.py
└── pypi pkg/            # Installable PyPI package — pip install mangadex-sync
```

Both versions are identical in functionality. Use **standalone** if you just want to run it directly. Use the **PyPI package** if you want `pip install` and a system-wide command.

---

## Quickstart

**Via pip:**
```bash
pip install mangadex-sync
mangadex-sync
```

**Via standalone:**
```bash
pip install -r standalone/requirements.txt
python standalone/mangadex_sync.py
```

Opens at `http://localhost:7337` — also accessible from any browser on your network.

---

## Features

| Feature | Description |
|---|---|
| **Export** | Fetch your full library from MangaDex with scores and statuses |
| **Import** | Restore from a JSON backup or MAL/AniList XML file |
| **Convert** | Generate MAL XML, AniList XML, and compressed `.gz` from your export |
| **Fast mode** | Export titles + status in minutes |
| **Deep mode** | Also fetches last read chapter per manga |
| **Resume** | Picks up from checkpoint if interrupted |
| **Dry run** | Preview what would happen without making changes |
| **Scores** | Carries your MangaDex ratings across to MAL/AniList |
| **History** | Logs every export with timestamp and file list |
| **Native window** | Desktop app via pywebview, falls back to browser |

---

## Requirements

- Python 3.10+
- A MangaDex account
- A [MangaDex API Personal Client](https://mangadex.org/settings) — free to create, approved instantly

---

## Getting your API credentials

1. Go to [mangadex.org/settings](https://mangadex.org/settings)
2. Scroll to **API Clients** → click **Create**
3. Name it anything, set type to **Personal**
4. Copy your **Client ID** and **Client Secret** into the app

---

## Exporting

1. Go to the **Export** tab
2. Enter your credentials and choose a save folder
3. Pick **Fast** or **Deep** mode
4. Click **⚡ Extract Entire Library**

| Mode | Speed | Includes chapter progress |
|---|---|---|
| Fast | ⚡ Minutes | No |
| Deep | 🐢 15–60+ min | Yes |

---

## Importing

Use the **Import** tab to restore your library from:
- `mdex_*.json` — a backup exported by this app
- `mal_*.xml` — a MAL export file
- `anilist_*.xml` — an AniList export file

Browse to your file, optionally enable **Import Scores**, and click **Start Import**.

---

## Output Files

| File | Description |
|---|---|
| `mdex_{status}_{timestamp}.json` | Full JSON backup, used by Import tab |
| `mdex_{status}_{timestamp}.xlsx` | Raw export data, used by Convert tab |
| `mal_{status}_{timestamp}.xml` | MAL import file |
| `mal_{status}_{timestamp}.xml.gz` | Compressed MAL file (also accepted by MAL) |
| `anilist_{status}_{timestamp}.xml` | AniList import file |

---

## Troubleshooting

**"Authentication failed"** — Check your Client ID, Secret, username and password. Make sure the API client is approved on MangaDex (not in pending state).

**"Failed to set status" during import** — Your API client was likely deleted or is not approved. Check [mangadex.org/settings](https://mangadex.org/settings).

**Native window doesn't open** — The app falls back to your browser automatically. On Linux, the app will print the exact system package command needed for your distro.

**Manga missing after MAL import** — Those titles had no MAL ID on MangaDex and are listed in the Skipped section. Add them to MAL manually.

---

## Credits

Based on the original export scripts by [Seriousattempts](https://github.com/Seriousattempts/MangaDex). Rewritten and extended with a web UI, Fast/Deep mode, import support, resume, score export, and AniList output.

---

## License

MIT