# UCSD Grad Life Calendar

One auto-updating Apple Calendar feed that merges the UCSD event sources that
matter to me — Bioengineering, GEPA, Career Center, GPSA — into a single
chronological calendar, plus a browsable dashboard.

No servers, no API keys, no cost. A GitHub Action rebuilds the feed every 6
hours and publishes it via GitHub Pages; Apple Calendar re-syncs it
automatically on Mac and iPhone.

```
GitHub Actions (cron every 6h)
        │  runs aggregate.py
        ▼
  fetch feeds ─┬─ Bioengineering  (public Google Calendar .ics × 2)
               ├─ GEPA            (Localist JSON API, group 49959617111603)
               ├─ Career Center   (Localist JSON API, group 50577698587808)
               └─ GPSA            (Localist JSON API, group 50331338399926)
        │  normalize → de-dupe → sort → write
        ▼
  docs/combined.ics + per-quarter .ics + events.json + index.html
        │  commit to /docs → GitHub Pages
        ▼
  webcal://<you>.github.io/ucsd-grad-calendar/combined.ics
        ▼
  Apple Calendar (Mac + iOS auto-sync)
```

## One-time setup

1. **Create a GitHub repo** named `ucsd-grad-calendar` and push these files:
   ```bash
   git init && git add . && git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/ucsd-grad-calendar.git
   git push -u origin main
   ```
2. **Set your Pages URL** in `sources.yaml` → `site.base_url`:
   `https://YOUR-USERNAME.github.io/ucsd-grad-calendar`
3. **Enable GitHub Pages**: repo **Settings → Pages → Source = Deploy from a
   branch → Branch = `main` / folder = `/docs`** → Save.
4. **Enable Actions write access**: **Settings → Actions → General → Workflow
   permissions → Read and write permissions** → Save. (Lets the scheduled job
   commit the refreshed feeds.)
5. **Run it once**: **Actions tab → Build UCSD Grad Calendar → Run workflow**.

Your dashboard is then live at
`https://YOUR-USERNAME.github.io/ucsd-grad-calendar/`.

## Subscribe on Apple Calendar

- **Mac**: open the dashboard, click **Subscribe in Apple Calendar** (a
  `webcal://` link). Or Calendar → File → New Calendar Subscription → paste
  `https://YOUR-USERNAME.github.io/ucsd-grad-calendar/combined.ics`. Set
  auto-refresh to *Every hour* or *Every day*.
- **iPhone**: subscribe once on the Mac with the calendar stored in **iCloud**
  and it appears on your phone automatically. (Or iPhone → Settings → Calendar
  → Accounts → Add Account → Other → Add Subscribed Calendar → paste the URL.)

## Editing sources

Everything is driven by [`sources.yaml`](sources.yaml) — toggle `enabled`,
add a Localist `group_id`, or add another public Google Calendar. No code
changes needed. To find a Localist group's numeric id:

```bash
curl -s "https://calendar.ucsd.edu/api/2/groups?pp=100&page=1" | python3 -m json.tool | grep -A1 -i "name"
```

## Run locally

```bash
pip install -r requirements.txt
python aggregate.py          # writes everything into docs/
open docs/index.html
```

## Notes on the other sources from my list

- **GEPA job bulletin** and **Student Orgs directory** are *not* event
  calendars (a job list and an org list have no dates), so they don't belong in
  a chronological feed. They'd be better served by a separate "watch for
  changes" digest — a future add-on, not part of this calendar.
- **Sun God Archery** is a Wix site with no machine-readable feed and no
  updates since 2025; it's disabled in `sources.yaml`. It would require HTML
  scraping to include.

*Not affiliated with UC San Diego. Aggregates publicly available event feeds.*
