#!/usr/bin/env python3
"""
UCSD Grad Life Calendar — aggregator.

Fetches events from every enabled source in sources.yaml, normalizes them into
one model, de-duplicates, sorts chronologically, and writes:

    docs/combined.ics        the single feed you subscribe to (webcal)
    docs/<QUARTER>.ics        per-quarter export files (e.g. FA26.ics)
    docs/events.json          machine-readable list (powers the dashboard)
    docs/index.html           a browsable dashboard with a Subscribe button

Run locally:   python aggregate.py
In CI:         invoked by .github/workflows/build.yml on a schedule.

No secrets or API keys are required — every source is a public feed.
"""

from __future__ import annotations

import hashlib
import html
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
from dateutil import parser as dtparser
from dateutil import tz
from icalendar import Calendar, Event as IcsEvent

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
UA = {"User-Agent": "Mozilla/5.0 (UCSD-Grad-Calendar aggregator; +https://github.com)"}
SESSION = requests.Session()
SESSION.headers.update(UA)
TIMEOUT = 30


# --------------------------------------------------------------------------- #
#  Normalized event model                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    uid: str
    summary: str
    start: datetime | date              # tz-aware datetime, or date for all-day
    end: datetime | date | None
    all_day: bool
    source: str
    color: str = "#333333"
    location: str = ""
    url: str = ""
    description: str = ""

    # ---- helpers ----
    @property
    def start_dt(self) -> datetime:
        """A tz-aware datetime for sorting (all-day -> local midnight)."""
        if isinstance(self.start, datetime):
            return self.start
        return datetime(self.start.year, self.start.month, self.start.day,
                        tzinfo=tz.gettz(LOCAL_TZ_NAME))

    def dedupe_key(self) -> str:
        title = "".join(ch for ch in self.summary.lower() if ch.isalnum())
        return f"{title}|{self.start_dt.astimezone(timezone.utc).isoformat()[:16]}"


LOCAL_TZ_NAME = "America/Los_Angeles"


# --------------------------------------------------------------------------- #
#  Source fetchers                                                             #
# --------------------------------------------------------------------------- #
def fetch_localist(src: dict, horizon_days: int) -> list[Event]:
    """UCSD Localist JSON API (calendar.ucsd.edu). Filter by group_id or type."""
    base = "https://calendar.ucsd.edu/api/2/events"
    params = {
        "days": horizon_days,
        "pp": 100,
        "hide_past": 1,
    }
    if src.get("group_id"):
        params["group_id"] = src["group_id"]
    if src.get("event_types"):
        params["type"] = src["event_types"]

    events: list[Event] = []
    page = 1
    while True:
        params["page"] = page
        r = SESSION.get(base, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("events", [])
        if not items:
            break
        for it in items:
            e = it.get("event", {})
            events.extend(_localist_event_to_models(e, src))
        pinfo = data.get("page", {})
        current = pinfo.get("current", page)
        total_pages = pinfo.get("pages", 1)
        if current >= total_pages:
            break
        page += 1
        if page > 50:  # safety valve
            break
    return events


def _localist_event_to_models(e: dict, src: dict) -> list[Event]:
    """A Localist event may have multiple instances (recurring); emit each."""
    out: list[Event] = []
    title = (e.get("title") or "").strip()
    if not title:
        return out
    loc = e.get("location_name") or e.get("room_number") or ""
    url = e.get("localist_url") or e.get("url") or ""
    desc = (e.get("description_text") or "").strip()
    if len(desc) > 600:
        desc = desc[:600].rsplit(" ", 1)[0] + "…"
    base_id = e.get("id")

    for inst_wrap in e.get("event_instances", []):
        inst = inst_wrap.get("event_instance", {})
        start_raw = inst.get("start")
        if not start_raw:
            continue
        all_day = bool(inst.get("all_day"))
        start = _parse_dt(start_raw, all_day)
        end_raw = inst.get("end")
        end = _parse_dt(end_raw, all_day) if end_raw else None
        uid = f"localist-{base_id}-{inst.get('id')}@ucsd-grad-calendar"
        out.append(Event(
            uid=uid, summary=title, start=start, end=end, all_day=all_day,
            source=src["name"], color=src.get("color", "#333"),
            location=loc, url=url, description=desc,
        ))
    return out


def fetch_gcal_ics(src: dict, horizon_days: int) -> list[Event]:
    """Public Google Calendar ICS feeds."""
    events: list[Event] = []
    horizon = datetime.now(timezone.utc) + timedelta(days=horizon_days)
    past_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    for cal_id in src.get("ids", []):
        enc = quote(cal_id, safe="")
        url = f"https://calendar.google.com/calendar/ical/{enc}/public/basic.ics"
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        cal = Calendar.from_ical(r.content)
        for comp in cal.walk("VEVENT"):
            ev = _ics_component_to_model(comp, src)
            if ev is None:
                continue
            # window filter
            s = ev.start_dt
            if s < past_cutoff or s > horizon:
                continue
            events.append(ev)
    return events


def _ics_component_to_model(comp, src: dict) -> Event | None:
    summary = str(comp.get("summary", "")).strip()
    if not summary:
        return None
    dtstart = comp.get("dtstart")
    if dtstart is None:
        return None
    start_val = dtstart.dt
    all_day = not isinstance(start_val, datetime)
    start = _ensure_aware(start_val)
    end = None
    if comp.get("dtend") is not None:
        end = _ensure_aware(comp.get("dtend").dt)
    uid_src = str(comp.get("uid", "")) or f"{summary}-{start}"
    uid = "gcal-" + hashlib.md5(uid_src.encode()).hexdigest()[:16] + "@ucsd-grad-calendar"
    loc = str(comp.get("location", "")).strip()
    desc = str(comp.get("description", "")).strip()
    if len(desc) > 600:
        desc = desc[:600].rsplit(" ", 1)[0] + "…"
    url = str(comp.get("url", "")).strip()
    return Event(
        uid=uid, summary=summary, start=start, end=end, all_day=all_day,
        source=src["name"], color=src.get("color", "#333"),
        location=loc, url=url, description=desc,
    )


# --------------------------------------------------------------------------- #
#  Datetime helpers                                                           #
# --------------------------------------------------------------------------- #
def _parse_dt(raw: str, all_day: bool):
    d = dtparser.parse(raw)
    if all_day:
        return d.date()
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz.gettz(LOCAL_TZ_NAME))
    return d


def _ensure_aware(val):
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=tz.gettz(LOCAL_TZ_NAME))
        return val
    return val  # a date -> all-day


# --------------------------------------------------------------------------- #
#  Quarter labels (UCSD is on the quarter system)                            #
# --------------------------------------------------------------------------- #
def quarter_label(d: datetime | date) -> str:
    m, y = d.month, d.year
    if m in (9, 10, 11, 12):
        q = "FA"
    elif m in (1, 2, 3):
        q = "WI"
    elif m in (4, 5, 6):
        q = "SP"
    else:
        q = "SU"
    return f"{q}{str(y)[2:]}"


# --------------------------------------------------------------------------- #
#  ICS writing                                                                #
# --------------------------------------------------------------------------- #
def build_calendar(events: list[Event], name: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//UCSD Grad Life Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", LOCAL_TZ_NAME)
    cal.add("method", "PUBLISH")
    for e in events:
        ie = IcsEvent()
        ie.add("uid", e.uid)
        ie.add("summary", f"{e.summary}")
        if e.all_day:
            ie.add("dtstart", e.start if isinstance(e.start, date) else e.start.date())
            end_d = e.end if e.end else (e.start_dt.date() + timedelta(days=1))
            if isinstance(end_d, datetime):
                end_d = end_d.date()
            ie.add("dtend", end_d)
        else:
            ie.add("dtstart", e.start_dt.astimezone(timezone.utc))
            end = e.end if isinstance(e.end, datetime) else (e.start_dt + timedelta(hours=1))
            ie.add("dtend", end.astimezone(timezone.utc))
        if e.location:
            ie.add("location", e.location)
        desc_parts = []
        if e.description:
            desc_parts.append(e.description)
        desc_parts.append(f"Source: {e.source}")
        if e.url:
            desc_parts.append(e.url)
        ie.add("description", "\n\n".join(desc_parts))
        if e.url:
            ie.add("url", e.url)
        ie.add("categories", [e.source])
        ie.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(ie)
    return cal.to_ical()


# --------------------------------------------------------------------------- #
#  Dashboard (index.html)                                                     #
# --------------------------------------------------------------------------- #
def build_dashboard(events: list[Event], cfg: dict) -> str:
    site = cfg.get("site", {})
    title = site.get("title", "UCSD Grad Life Calendar")
    base_url = site.get("base_url", "").rstrip("/")
    ics_url = f"{base_url}/combined.ics" if base_url else "combined.ics"
    webcal = ics_url.replace("https://", "webcal://").replace("http://", "webcal://")
    updated = datetime.now(tz.gettz(LOCAL_TZ_NAME)).strftime("%b %d, %Y · %I:%M %p %Z")

    # source legend
    src_colors = {}
    for s in cfg.get("sources", []):
        if s.get("enabled"):
            src_colors[s["name"]] = s.get("color", "#333")

    # group events by quarter then render rows
    rows = []
    last_q = None
    for e in events:
        q = quarter_label(e.start_dt)
        if q != last_q:
            rows.append(f'<h2 class="quarter">{q}</h2>')
            last_q = q
        if e.all_day:
            when = e.start_dt.strftime("%a %b %-d, %Y") + " · all day"
        else:
            when = e.start_dt.strftime("%a %b %-d, %Y · %-I:%M %p")
        color = e.color
        loc = f' · <span class="loc">{html.escape(e.location)}</span>' if e.location else ""
        titlecell = html.escape(e.summary)
        if e.url:
            titlecell = f'<a href="{html.escape(e.url)}" target="_blank" rel="noopener">{titlecell}</a>'
        rows.append(f"""
        <div class="event">
          <div class="tag" style="background:{color}">{html.escape(e.source)}</div>
          <div class="body">
            <div class="etitle">{titlecell}</div>
            <div class="meta">{when}{loc}</div>
          </div>
        </div>""")

    legend = " ".join(
        f'<span class="chip"><span class="dot" style="background:{c}"></span>{html.escape(n)}</span>'
        for n, c in src_colors.items()
    )
    events_html = "\n".join(rows) if rows else "<p>No upcoming events found.</p>"

    return TEMPLATE.format(
        title=html.escape(title),
        count=len(events),
        updated=updated,
        webcal=html.escape(webcal),
        ics_url=html.escape(ics_url),
        legend=legend,
        events=events_html,
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg:#f7f8fa; --card:#fff; --ink:#1a1d24; --muted:#6b7280; --line:#e5e7eb;
    --accent:#1b3a6b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0f1218; --card:#161b24; --ink:#e8eaed; --muted:#9aa4b2; --line:#252c38; --accent:#7aa2e3; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  header {{ background:var(--accent); color:#fff; padding:28px 20px; }}
  .wrap {{ max-width:820px; margin:0 auto; padding:0 16px; }}
  h1 {{ margin:0 0 4px; font-size:1.5rem; }}
  header .sub {{ opacity:.9; font-size:.9rem; }}
  .actions {{ margin-top:16px; display:flex; gap:10px; flex-wrap:wrap; }}
  .btn {{ display:inline-block; padding:10px 16px; border-radius:10px; font-weight:600;
    text-decoration:none; font-size:.92rem; }}
  .btn.primary {{ background:#fff; color:var(--accent); }}
  .btn.ghost {{ background:rgba(255,255,255,.15); color:#fff; border:1px solid rgba(255,255,255,.35); }}
  main {{ padding:22px 0 60px; }}
  .legend {{ display:flex; gap:14px; flex-wrap:wrap; margin:6px 0 20px; font-size:.82rem; color:var(--muted); }}
  .chip {{ display:inline-flex; align-items:center; gap:6px; }}
  .dot {{ width:10px; height:10px; border-radius:50%; display:inline-block; }}
  .quarter {{ font-size:1rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
    margin:26px 0 10px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
  .event {{ display:flex; gap:12px; background:var(--card); border:1px solid var(--line);
    border-radius:12px; padding:12px 14px; margin-bottom:8px; }}
  .tag {{ color:#fff; font-size:.68rem; font-weight:700; padding:3px 8px; border-radius:6px;
    height:fit-content; white-space:nowrap; }}
  .etitle {{ font-weight:600; line-height:1.3; }}
  .etitle a {{ color:var(--ink); text-decoration:none; }}
  .etitle a:hover {{ text-decoration:underline; }}
  .meta {{ color:var(--muted); font-size:.85rem; margin-top:3px; }}
  .loc {{ font-style:italic; }}
  footer {{ color:var(--muted); font-size:.8rem; text-align:center; padding:24px; }}
  code {{ background:var(--line); padding:2px 6px; border-radius:5px; font-size:.82rem; }}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>{title}</h1>
    <div class="sub">{count} upcoming events · updated {updated}</div>
    <div class="actions">
      <a class="btn primary" href="{webcal}">＋ Subscribe in Apple Calendar</a>
      <a class="btn ghost" href="combined.ics" download>⬇ Download .ics</a>
    </div>
  </div>
</header>
<main class="wrap">
  <div class="legend">{legend}</div>
  {events}
</main>
<footer>
  Auto-generated from public UCSD feeds · not affiliated with UC San Diego.<br>
  Subscribe URL: <code>{ics_url}</code>
</footer>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #
FETCHERS = {"localist": fetch_localist, "gcal_ics": fetch_gcal_ics}


def main() -> int:
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text())
    horizon = int(cfg.get("site", {}).get("horizon_days", 365))
    global LOCAL_TZ_NAME
    LOCAL_TZ_NAME = cfg.get("site", {}).get("timezone", LOCAL_TZ_NAME)

    all_events: list[Event] = []
    for src in cfg.get("sources", []):
        if not src.get("enabled"):
            continue
        fetcher = FETCHERS.get(src.get("type"))
        if fetcher is None:
            print(f"  ! skipping '{src['name']}' (type '{src.get('type')}' not supported)")
            continue
        try:
            got = fetcher(src, horizon)
            print(f"  ✓ {src['name']}: {len(got)} events")
            all_events.extend(got)
        except Exception as ex:  # one bad source must not kill the build
            print(f"  ✗ {src['name']}: FAILED — {ex}", file=sys.stderr)

    # de-duplicate
    seen: dict[str, Event] = {}
    for e in all_events:
        seen.setdefault(e.dedupe_key(), e)
    events = sorted(seen.values(), key=lambda e: e.start_dt)
    print(f"\nTotal after de-dupe: {len(events)} events "
          f"(from {len(all_events)} raw)")

    DOCS.mkdir(exist_ok=True)

    # combined feed
    (DOCS / "combined.ics").write_bytes(build_calendar(events, cfg["site"]["title"]))

    # per-quarter feeds
    quarters: dict[str, list[Event]] = {}
    for e in events:
        quarters.setdefault(quarter_label(e.start_dt), []).append(e)
    for q, evs in quarters.items():
        (DOCS / f"{q}.ics").write_bytes(build_calendar(evs, f"{cfg['site']['title']} — {q}"))

    # json
    def ser(e: Event):
        d = asdict(e)
        d["start"] = e.start_dt.isoformat()
        d["end"] = e.end.isoformat() if isinstance(e.end, (datetime, date)) else None
        d["quarter"] = quarter_label(e.start_dt)
        return d
    (DOCS / "events.json").write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(),
         "count": len(events), "events": [ser(e) for e in events]},
        indent=2, ensure_ascii=False))

    # dashboard
    (DOCS / "index.html").write_text(build_dashboard(events, cfg))

    print(f"Wrote docs/combined.ics, {len(quarters)} quarter file(s), "
          f"events.json, index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
