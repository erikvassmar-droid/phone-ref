#!/usr/bin/env python3
"""nbech_cheatsheet.py - build the live NBECH field cheat-sheet page for Erik's phone.

A self-contained dark/OLED HTML page (brand green) served from the phone-ref GitHub Pages repo at a
FIXED URL (pin once, no re-pin). It refreshes through the day from Equipe: the running order with live
state (upcoming / running / done), the Swedish riders to cover per class WITH their result + placing as
scores land, the top-3 leaders, and team standings (best-3-of-4) for the team classes. The gold-winner
focus, shot checklist and field-logger link are always on.

A scheduled wrapper (nbech_cheatsheet_push.ps1) re-runs this and pushes phone-ref so the same URL updates.

    python nbech_cheatsheet.py build --meeting 80465 --out C:\\Users\\Erik\\phone-ref\\nbech\\index.html
    python nbech_cheatsheet.py selftest

The HTML builder (build_html) is a pure function covered by selftest; fetch_event needs the network.
Stdlib + equipe_api + team_standings.
"""
import argparse
import datetime
import html as _html
import io
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FIELD_LOGGER_URL = "https://erikvassmar-droid.github.io/phone-ref/field-logger/"
SKIP_PREFIXES = ("materlist", "masterlist", "mastelist", "riders inform", "event venue")


def _short(name):
    s = re.sub(r"\s+", " ", (name or "")).strip()
    s = s.replace("CH-N.Baltic-D GP", "Senior GP").replace(" - FEI Lagtävlansprogram ponny", "")
    s = s.replace("Competition", "").replace("  ", " ").strip()
    return s


def _rk(s):
    try:
        return int(s.get("rank"))
    except (TypeError, ValueError):
        return None


def rank_section(starts, member_total):
    """[(start, score, place)] ranked best-first. Placing prefers Equipe's official 'rank' (it's
    authoritative once results are entered, even when the feed's score field stays blank - e.g. the
    Senior GP Special); otherwise riders are ordered live by their score. Score may be None when only
    the rank is available."""
    starts = starts or []
    have_score = [(s, v) for s, v in ((s, member_total(s)) for s in starts) if v is not None]
    ranked_eq = [(s, _rk(s)) for s in starts if _rk(s) is not None]
    if ranked_eq and len(ranked_eq) >= len(have_score):     # official placings at least as complete
        ranked_eq.sort(key=lambda x: x[1])
        return [(s, member_total(s), r) for s, r in ranked_eq]
    have_score.sort(key=lambda x: -x[1])
    return [(s, v, i + 1) for i, (s, v) in enumerate(have_score)]


def _class_state(n_total, n_scored):
    if n_total and n_scored >= n_total:
        return "done"
    if n_scored > 0:
        return "running"
    return "upcoming"


def _is_nation(v):
    """True if `v` looks like an alpha nation code (SWE, FRA, GER...). At INTERNATIONAL meetings Equipe puts
    the nation in logo_id; at DOMESTIC (Swedish) meetings logo_id is instead a numeric CLUB id, so this is
    how we tell 'which Swedish riders to cover' (international) apart from 'everyone's Swedish, show clubs'."""
    v = (v or "").strip()
    return v.isalpha() and 2 <= len(v) <= 3


def _nat_or_club(s):
    """Display token for a start: the nation code at an international meeting, else the club name at a
    domestic one (so a leader reads 'Esther A (Falsterbo RK)' instead of the raw numeric club id)."""
    lid = (s.get("logo_id") or "").strip()
    if _is_nation(lid):
        return lid.upper()
    return (s.get("club_name") or "").strip() or lid


def fetch_event(meeting_id, fetch=None):
    """Pull the meeting into the cheat-sheet data model (network)."""
    import equipe_api
    import team_standings as ts
    fetch = fetch or (lambda u: equipe_api.safe_fetch(u) or {})
    base = equipe_api.BASE
    sch = fetch("%s/meetings/%s/schedule" % (base, meeting_id))
    classes = []
    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    _kept = lambda mc: not (mc.get("name") or "").lower().startswith(SKIP_PREFIXES)
    all_mc = [mc for mc in (sch.get("meeting_classes") or []) if _kept(mc)]
    todays = [mc for mc in all_mc if (mc.get("start_at") or "")[:10] == today_str]
    use_mc = todays or all_mc          # show only TODAY's classes; off-day -> fall back to all
    for mc in use_mc:
        nm = (mc.get("name") or "")
        arena = (mc.get("arena") or "").strip()          # which ring - the key fact at a multi-arena venue
        rider_sec, team_sec = None, None
        for s in (mc.get("class_sections") or []):
            sec = fetch("%s/class_sections/%s" % (base, s["id"]))
            st = sec.get("starts") or []
            if st and any(str(x.get("type", "")).lower() == "team" for x in st):
                team_sec = sec
            elif st and (rider_sec is None or len(st) > len(rider_sec.get("starts") or [])):
                rider_sec = sec
        # team standings (club/nation teams). team_name = club at a domestic show, nation at an int'l one.
        teams = []
        if team_sec is not None:
            teams = [{"rank": t.get("rank"), "nation": t.get("team_name") or t.get("nation"),
                      "total": t.get("total")}
                     for t in ts.standings(ts.parse_team_section(team_sec))]
        if not rider_sec:
            # a TEAM-only class (e.g. Folksam Ponnyallsvenska / Elitallsvenska) - surface it with its
            # standings rather than dropping it (the old code required an individual section and skipped it).
            if teams:
                done = sum(1 for t in teams if t.get("total") is not None)
                state = "done" if (done and done == len(teams)) else ("running" if done else "upcoming")
                classes.append({"name": _short(nm), "start_at": (mc.get("start_at") or "")[11:16],
                                "arena": arena, "type": "team", "state": state, "domestic": True,
                                "n": len(teams), "swe": [], "leaders": [], "teams": teams})
            continue
        starts = rider_sec.get("starts") or []
        ranked = rank_section(starts, ts.member_total)
        place_by_id = {id(s): (v, p) for s, v, p in ranked}
        # domestic show -> logo_id is a numeric CLUB id (not a nation); the SWE-nation lens doesn't apply.
        # Use a MAJORITY test, not any(): a single foreign visitor in an otherwise-Swedish amateur class
        # must not flip it to "international" (which would then wrongly report "No Swedish riders").
        nat = sum(1 for s in starts if _is_nation(s.get("logo_id")))
        intl = bool(starts) and nat * 2 >= len(starts)
        swe = []
        for s in starts:
            if (s.get("logo_id") or "").upper() != "SWE":
                continue
            vp = place_by_id.get(id(s))
            swe.append({"no": str(s.get("start_no") or ""), "rider": ts._member_name(s),
                        "horse": (s.get("horse_name") or "").strip(),
                        # per-rider scheduled ride time, read LIVE each regen so it tracks schedule drift
                        "start": (s.get("start_at") or "")[11:16],
                        "score": (round(vp[0], 2) if (vp and vp[0] is not None) else None),
                        "place": (vp[1] if vp else None)})
        swe.sort(key=lambda r: (r["place"] is None, r["place"] if r["place"] else 0,
                                int(r["no"]) if r["no"].isdigit() else 0))
        leaders = [{"place": p, "rider": ts._member_name(s), "nation": _nat_or_club(s),
                    "score": (round(v, 2) if v is not None else None)} for s, v, p in ranked[:3]]
        classes.append({"name": _short(nm), "start_at": (mc.get("start_at") or "")[11:16],
                        "arena": arena,
                        "type": ("team" if team_sec is not None or "team" in nm.lower() else "indiv"),
                        "state": _class_state(len(starts), len(ranked)), "domestic": not intl,
                        "n": len(starts), "swe": swe, "leaders": leaders, "teams": teams})
    classes.sort(key=lambda c: c["start_at"] or "99")
    day_no = (today - datetime.date(2026, 6, 22)).days + 1
    name = (sch.get("display_name") or "").strip()
    return {"meeting": name, "brand": name, "heading": name, "venue": "", "subtitle": "",
            "date": today_str if todays else "all", "day_no": day_no,
            "date_label": today.strftime("%a %d %b"),
            "generated": datetime.datetime.now().strftime("%H:%M"), "classes": classes}


# --------------------------------------------------------------------- merged (jumping + eventing)
def _evt_token(name, discipline):
    """Category/level token (delegates to the verified mapping); used to line a jumping masterlist up
    with its competition class. Safe if nbech_ingest is unavailable."""
    try:
        import nbech_ingest as ni
        return ni.token_for(name, discipline)
    except Exception:
        return re.sub(r"\s+", "", (name or "")) or "Class"


def _swe_roster(starts):
    """SWE rider rows from a section's starts (no result), de-duped by rider+horse."""
    out, seen = [], set()
    for x in (starts or []):
        if (x.get("logo_id") or "").upper() != "SWE":
            continue
        key = (x.get("rider_id") or (x.get("rider_name") or ""), x.get("horse_name") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"no": str(x.get("start_no") or ""), "rider": (x.get("rider_name") or "").strip(),
                    "horse": (x.get("horse_name") or "").strip(),
                    "start": (x.get("start_at") or "")[11:16], "score": None, "place": None})
    return out


def _masterlist_rosters(sch, fetch, base):
    """{token: [SWE roster]} from the jumping masterlists (the 'list' classes we normally skip), so a
    jumping category with no published start list yet still shows its Swedish riders."""
    out = {}
    for mc in (sch.get("meeting_classes") or []):
        if (mc.get("discipline") or "") != "list":
            continue
        tok = _evt_token(mc.get("name") or "", "list")
        for s in (mc.get("class_sections") or []):
            r = _swe_roster((fetch("%s/class_sections/%s" % (base, s["id"])).get("starts")) or [])
            if r:
                out.setdefault(tok, []).extend(r)
    return out


def _target_day_mc(all_mc, today_str):
    """Pick the classes a meeting contributes to the merged page: today's, else (pre-event) the
    earliest UPCOMING day's, else NONE - a meeting that is already FINISHED must not hijack a live
    day (its past classes are dated earlier, so they would sort to the top). Returns (classes, label-date)."""
    days = sorted({(mc.get("start_at") or "")[:10] for mc in all_mc if (mc.get("start_at") or "")})
    todays = [mc for mc in all_mc if (mc.get("start_at") or "")[:10] == today_str]
    if todays:
        return todays, today_str
    future = [d for d in days if d >= today_str]
    if future:
        return [mc for mc in all_mc if (mc.get("start_at") or "")[:10] == future[0]], future[0]
    return [], (days[-1] if days else today_str)        # finished -> contribute nothing today


def _event_tag(display_name):
    n = (display_name or "").lower()
    return "Jump" if "jump" in n else ("Event" if "event" in n else (display_name or "")[:5])


def fetch_merged(meeting_ids, fetch=None, today=None):
    """Merge several Equipe meetings (jumping 80632 + eventing 80567) into ONE time-sorted cheat-sheet
    model. Each class carries an 'event' tag; a jumping class with no start list yet falls back to its
    category masterlist roster so the Swedish riders still show. Live results/leaders/teams fill in as
    they land (same model as the dressage page)."""
    import equipe_api
    import team_standings as ts
    fetch = fetch or (lambda u: equipe_api.safe_fetch(u) or {})
    base = equipe_api.BASE
    today = today or datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    classes, names, tdates = [], [], set()
    for mid in meeting_ids:
        sch = fetch("%s/meetings/%s/schedule" % (base, mid))
        names.append((sch.get("display_name") or "").strip())
        tag = _event_tag(sch.get("display_name"))
        # real COMPETITION classes only (a meeting also lists masterlists + admin/info rows like
        # "Horse inspection" / "Show Office" / "CSI1*" entry list, several of which are dated the
        # arrival day and would otherwise hijack the day selection). Masterlists -> roster source below.
        all_mc = [mc for mc in (sch.get("meeting_classes") or [])
                  if (mc.get("discipline") or "") in ("show_jumping", "eventing", "dressage")
                  and not (mc.get("name") or "").lower().startswith(SKIP_PREFIXES)]
        rosters = _masterlist_rosters(sch, fetch, base)
        day_mc, tdate = _target_day_mc(all_mc, today_str)
        if day_mc:                       # only a meeting that actually contributes classes sets the day label
            tdates.add(tdate)
        for mc in day_mc:
            nm = (mc.get("name") or "")
            cdate = (mc.get("start_at") or "")[:10]
            rider_sec, team_sec = None, None
            for s in (mc.get("class_sections") or []):
                sec = fetch("%s/class_sections/%s" % (base, s["id"]))
                st = sec.get("starts") or []
                if st and any(str(x.get("type", "")).lower() == "team" for x in st):
                    team_sec = sec
                elif st and (rider_sec is None or len(st) > len(rider_sec.get("starts") or [])):
                    rider_sec = sec
            if not rider_sec:                                  # no start list yet -> masterlist roster
                roster = rosters.get(_evt_token(nm, mc.get("discipline")))
                if roster:
                    classes.append({"name": _short(nm), "start_at": (mc.get("start_at") or "")[11:16],
                                    "event": tag, "date": cdate, "type": "indiv", "state": "upcoming",
                                    "n": len(roster), "swe": roster, "leaders": [], "teams": []})
                continue
            starts = rider_sec.get("starts") or []
            ranked = rank_section(starts, ts.member_total)
            place_by_id = {id(s): (v, p) for s, v, p in ranked}
            swe = []
            for s in starts:
                if (s.get("logo_id") or "").upper() != "SWE":
                    continue
                vp = place_by_id.get(id(s))
                swe.append({"no": str(s.get("start_no") or ""), "rider": ts._member_name(s),
                            "horse": (s.get("horse_name") or "").strip(),
                            "start": (s.get("start_at") or "")[11:16],
                            "score": (round(vp[0], 2) if (vp and vp[0] is not None) else None),
                            "place": (vp[1] if vp else None)})
            swe.sort(key=lambda r: (r["place"] is None, r["place"] if r["place"] else 0,
                                    int(r["no"]) if r["no"].isdigit() else 0))
            leaders = [{"place": p, "rider": ts._member_name(s), "nation": (s.get("logo_id") or "").upper(),
                        "score": (round(v, 2) if v is not None else None)} for s, v, p in ranked[:3]]
            teams = []
            if team_sec is not None:
                teams = [{"rank": t.get("rank"), "nation": t.get("nation"), "total": t.get("total")}
                         for t in ts.standings(ts.parse_team_section(team_sec))]
            classes.append({"name": _short(nm), "start_at": (mc.get("start_at") or "")[11:16],
                            "event": tag, "date": cdate,
                            "type": ("team" if team_sec is not None or "team" in nm.lower() else "indiv"),
                            "state": _class_state(len(starts), len(ranked)), "n": len(starts),
                            "swe": swe, "leaders": leaders, "teams": teams})
    def _csort(c):                       # untimed (00:00 / blank) classes drop to the BOTTOM of their day
        t = c.get("start_at") or ""
        return ((c.get("date") or "9999"), 1 if t in ("", "00:00") else 0, t or "99")
    classes.sort(key=_csort)
    one = sorted(tdates)[0] if len(tdates) == 1 else None
    label = (datetime.datetime.strptime(one, "%Y-%m-%d").strftime("%a %d %b") if one else "Strömsholm")
    return {"meeting": " + ".join(n for n in names if n), "subtitle": "Jumping &amp; Eventing",
            "heading": "NBECH — Jumping + Eventing", "brand": "NBECH — Jumping + Eventing",
            "venue": "Strömsholm", "date": one or "all",
            "day_no": (today - datetime.date(2026, 6, 22)).days + 1, "date_label": label,
            "generated": datetime.datetime.now().strftime("%H:%M"), "classes": classes}


# --------------------------------------------------------------------------- HTML
CSS = """
:root{--green:#379144;--gold:#f3c01b;--bg:#000;--card:#0c0f0c;--line:#1c241f;--txt:#e8efe6;--mut:#8aa093;--red:#d8473f}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,Segoe UI,Roboto,system-ui,sans-serif;
 line-height:1.44;font-size:15px;max-width:680px;margin:0 auto;padding:14px 14px calc(env(safe-area-inset-bottom) + 30px)}
h1{font-size:21px;color:#fff;letter-spacing:.2px}
.sub{color:var(--mut);font-size:12.5px;margin:2px 0 12px}
a.btn{display:block;text-align:center;background:var(--green);color:#04150a;font-weight:800;text-decoration:none;
 padding:13px;border-radius:12px;margin:10px 0;font-size:16px}
.pins{display:flex;flex-wrap:wrap;gap:8px;margin:11px 0 2px}
a.pin{display:inline-flex;align-items:center;gap:6px;background:rgba(55,145,68,.16);border:1px solid var(--green);color:#dff0e2;text-decoration:none;font-weight:700;font-size:13px;padding:8px 13px;border-radius:999px}
.gold{border:1.5px solid var(--gold);background:rgba(243,192,27,.08);border-radius:12px;padding:11px 12px;margin:10px 0;font-size:14px}
.gold b{color:var(--gold)}
h2{color:var(--green);font-size:16px;margin:22px 0 8px;border-bottom:1px solid var(--line);padding-bottom:5px}
.ro{display:flex;gap:9px;align-items:baseline;padding:7px 2px;border-bottom:1px solid var(--line);font-size:14px}
.ro .t{color:var(--green);font-weight:800;min-width:44px}
.ro .c{flex:1;min-width:0}
.ro .st{font-size:11px;font-weight:700;padding:1px 7px;border-radius:9px;white-space:nowrap}
.ro .ar{color:var(--gold);font-size:11px;font-weight:700;white-space:nowrap}
.cls h3 .tag .ar{color:var(--gold)}
.st.upcoming{color:var(--mut);border:1px solid var(--line)}
.st.running{color:#04150a;background:var(--gold)}
.st.done{color:#04150a;background:var(--green)}
.cls{margin:14px 0 16px}
.cls h3{font-size:14px;color:#cfe6d2;margin-bottom:4px}
.cls h3 .tm{color:var(--green);font-weight:800}
.cls h3 .tag{color:var(--mut);font-size:11px;font-weight:600}
.r{font-size:14px;padding:4px 0;border-bottom:1px solid #141a13;display:flex;gap:8px;align-items:baseline}
.r .no{color:var(--gold);font-weight:800;min-width:32px}
.r .nm{flex:1;min-width:0}
.r .hs{color:var(--mut);font-size:12.5px}
.r .stt{color:var(--mut);font-size:11px;white-space:nowrap;font-variant-numeric:tabular-nums;opacity:.85}
.r .res{font-weight:800;white-space:nowrap}
.r .res .pl{color:var(--gold)}
.lead{font-size:13px;color:var(--mut);margin:4px 0 0}
.lead b{color:#cfe6d2}
.teams{margin:6px 0 0;font-size:13.5px}
.teams .tr{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid #141a13}
.teams .tr .rk{color:var(--gold);font-weight:800;min-width:24px}
.teams .tr .tt{font-weight:700}
.note{color:var(--mut);font-size:13px}
.foot{color:#5f7363;font-size:11.5px;margin-top:22px;border-top:1px solid var(--line);padding-top:10px}
"""


def _e(s):
    return _html.escape(str(s if s is not None else ""))


def _res_str(r):
    if r.get("place") is None:
        return ""
    return "<span class='res'><span class='pl'>%d.</span> %s</span>" % (r["place"], ("%.2f" % r["score"]) if r.get("score") is not None else "")


# Pinned interview cheat-sheets (each built by nbech_rider.py, saved next to this page). Rendered as quick
# round-link chips at the very top of the page. Add/remove (label, relative_href) tuples; [] = no bar.
PINNED_LINKS = []   # eventing finished 27 Jun -> Frida interview pin removed (re-add here for a live interview)


def build_html(event, refresh_secs=90):
    cl = event.get("classes") or []
    n_swe = sum(len(c.get("swe") or []) for c in cl)
    day_no = event.get("day_no", 1)
    date_label = event.get("date_label") or "Strömsholm"
    # Branding follows the meeting: single-meeting builds carry the Equipe display_name as brand/heading/venue
    # (so Falsterbo etc. self-label correctly); the merged NBECH build sets these explicitly. The "NBECH Day N"
    # fallbacks only fire for a bare event dict with no meeting name (keeps old behaviour + the selftest stub).
    brand = (event.get("brand") or event.get("meeting") or "").strip() or ("NBECH Day %d" % day_no)
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>",
           "<meta name='theme-color' content='#000000'><title>%s</title>" % _e(brand),
           "<style>%s</style></head><body>" % CSS]
    out.append("<h1>🐴 %s</h1>" % _e(event.get("heading") or brand))
    bits = [_e(date_label)]
    if event.get("subtitle"):
        bits.append(event["subtitle"])            # pre-escaped entity string, e.g. 'Jumping &amp; Eventing'
    if event.get("venue"):
        bits.append(_e(event["venue"]))
    bits.append("%d classes" % len(cl))
    # international meeting -> "N Swedish" (the nation lens); domestic -> the count is meaningless (all Swedish)
    any_intl = any(not c.get("domestic") for c in cl)
    if any_intl:
        bits.append("%d Swedish" % n_swe)
    out.append("<div class='sub'>%s · updated %s</div>" % (" · ".join(bits), _e(event.get("generated"))))
    if PINNED_LINKS:
        out.append("<div class='pins'>%s</div>" % "".join(
            "<a class='pin' href='%s'>🎤 %s</a>" % (_e(href), _e(label)) for label, href in PINNED_LINKS))
    # --- riders & results (placed FIRST, above the running order + focus box). The 🇸🇪 nation lens only
    # makes sense at an international meeting; a domestic Swedish show gets a neutral header + club standings. ---
    swe_block = ["<h2>%s</h2>" % ("🇸🇪 Swedish riders &amp; results" if any_intl else "🏇 Classes &amp; results")]
    for c in cl:
        loc = (("%s · " % c["arena"]) if c.get("arena") else "")
        unit = "teams" if c["type"] == "team" else "riders"
        tag = ((c.get("event") + " · ") if c.get("event") else "") + ("TEAM" if c["type"] == "team" else "indiv")
        swe_block.append("<div class='cls'><h3><span class='tm'>%s</span> %s &nbsp;<span class='tag'>%s%s · %d %s</span></h3>"
                         % (_e(c["start_at"]), _e(c["name"]), _e(loc), _e(tag), c["n"], unit))
        if not c["swe"] and not c.get("domestic") and c["type"] != "team":
            swe_block.append("<div class='note'>No Swedish riders.</div>")
        for r in c["swe"]:
            stt = ("<span class='stt'>⏱ %s</span>" % _e(r["start"])) if r.get("start") else ""
            swe_block.append("<div class='r'><span class='no'>#%s</span><span class='nm'>%s "
                             "<span class='hs'>%s</span></span>%s%s</div>"
                             % (_e(r["no"]), _e(r["rider"]), _e(r["horse"]), stt, _res_str(r)))
        if c.get("leaders") and c["state"] != "upcoming":
            lead = " · ".join("<b>%d.</b> %s (%s)%s" % (l["place"], _e(l["rider"]), _e(l["nation"]),
                                                        (" %.2f" % l["score"]) if l.get("score") is not None else "")
                              for l in c["leaders"])
            swe_block.append("<div class='lead'>Leaders: %s</div>" % lead)
        if c.get("teams"):          # show seeded teams pre-score too (Allsvenska line-up), totals fill in live
            swe_block.append("<div class='teams'>")
            for t in c["teams"]:
                tot = ("%.2f" % t["total"]) if t.get("total") is not None else "—"
                swe_block.append("<div class='tr'><span class='rk'>%s</span><span class='tt'>%s</span>"
                                 "<span style='margin-left:auto;font-weight:800'>%s</span></div>"
                                 % (("%d" % t["rank"]) if t.get("rank") else "·", _e(t["nation"]), tot))
            swe_block.append("</div>")
        swe_block.append("</div>")

    # --- running order (arena chip first: at a multi-ring venue, WHERE to be beats what's on) ---
    ro_block = ["<h2>⏱ Running order</h2>"]
    for c in cl:
        cname = (("%s · " % c["event"]) if c.get("event") else "") + c["name"]
        loc = ("<span class='ar'>🏟 %s</span> " % _e(c["arena"])) if c.get("arena") else ""
        ro_block.append("<div class='ro'><span class='t'>%s</span><span class='c'>%s%s</span>"
                        "<span class='st %s'>%s</span></div>"
                        % (_e(c["start_at"]), loc, _e(cname), c["state"],
                           {"upcoming": "—", "running": "LIVE", "done": "done"}[c["state"]]))

    # --- focus box (now AFTER the running order) ---
    gold_block = ["<div class='gold'><b>🥇 Focus = gold winners.</b> When a rider/team wins, flag their round "
                  "→ <b>🥇 Win</b> (or long-press their row) → chase the <b>ceremony + reactions</b>.</div>"]

    # ORDER: Swedish riders & results  ->  running order  ->  focus box
    out.extend(swe_block)
    out.extend(ro_block)
    out.extend(gold_block)

    out.append("<h2>🎥 Shot checklist</h2><div class='note'><b>Wide</b> entry · <b>Tight</b> in the test · "
               "<b>Detail</b> (hands/expression) · <b>React</b> (face after) · <b>Follow</b> exit.<br>"
               "B-roll: warm-up · crowd &amp; flags · sponsor boards · stable · venue · weather.</div>")
    out.append("<div class='foot'>Live from Equipe · auto-refreshes every %ds while open · updated %s · Equisport</div>"
               % (refresh_secs, _e(event.get("generated"))))
    # auto-refresh ONLY while the page is open/foreground (visibilityState), scroll preserved. The static
    # Pages copy uses 90s (watcher cadence); the proxy serves the same page with a fast refresh.
    out.append("<script>(function(){try{var y=sessionStorage.getItem('nbsy');if(y)window.scrollTo(0,+y);}"
               "catch(e){}setInterval(function(){try{sessionStorage.setItem('nbsy',window.scrollY);}catch(e){}"
               "if(document.visibilityState==='visible')location.reload();},%d);})();</script>" % (refresh_secs * 1000))
    out.append("</body></html>")
    return "\n".join(out)


_MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}
def _ord(n):
    return {1: "st", 2: "nd", 3: "rd"}.get(n, "th")


def compute_notifications(event, statepath):
    """When a class FINISHES with Swedish rider(s) on the podium, emit a phone-notification line (once
    per class). Low-noise: only final podiums, de-duped via a small state file. Returns [str]."""
    import json
    try:
        state = json.load(open(statepath, encoding="utf-8"))
    except Exception:
        state = {}
    done = set(state.get("done_notified") or [])
    msgs = []
    for c in event.get("classes") or []:
        if c.get("state") != "done" or c["name"] in done:
            continue
        pod = sorted([r for r in c.get("swe") or [] if r.get("place") and r["place"] <= 3],
                     key=lambda r: r["place"])
        for r in pod:
            msgs.append("%s %s — %d%s in %s (%.2f)"
                        % (_MEDAL[r["place"]], r["rider"], r["place"], _ord(r["place"]),
                           c["name"], r["score"]))
        if c.get("state") == "done":
            done.add(c["name"])
    state["done_notified"] = sorted(done)
    try:
        json.dump(state, open(statepath, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return msgs


def run_selftest():
    ok = [True]
    def check(c, m):
        ok[0] = ok[0] and bool(c); print(("ok  " if c else "FAIL") + "  " + m)
    # ranking
    starts = [
        {"start_no": 1, "rider_name": "A", "logo_id": "NOR", "results": [{"type": "DressageTotal", "value": "70.0"}]},
        {"start_no": 2, "rider_name": "B", "logo_id": "SWE", "results": [{"type": "DressageTotal", "value": "72.5"}]},
        {"start_no": 3, "rider_name": "C", "logo_id": "SWE", "results": []}]
    import team_standings as ts
    ranked = rank_section(starts, ts.member_total)
    check([p for _, _, p in ranked] == [1, 2] and ranked[0][0]["rider_name"] == "B",
          "rank_section: B (72.5) ranks 1st, unscored rider excluded")
    check(_class_state(3, 0) == "upcoming" and _class_state(3, 1) == "running" and _class_state(3, 3) == "done",
          "class_state: upcoming/running/done by scored count")
    # official Equipe ranks but BLANK score field (e.g. Senior GP Special) -> use ranks, class is done
    rank_only = [
        {"start_no": 1, "rider_name": "Cecilie", "logo_id": "DEN", "rank": 1, "results": []},
        {"start_no": 4, "rider_name": "Ebba", "logo_id": "SWE", "rank": 4, "results": []},
        {"start_no": 11, "rider_name": "Beata", "logo_id": "SWE", "rank": 3, "results": []},
        {"start_no": 2, "rider_name": "Ville", "logo_id": "FIN", "rank": 2, "results": []}]
    rr = rank_section(rank_only, ts.member_total)
    check([p for _, _, p in rr] == [1, 2, 3, 4] and rr[2][0]["rider_name"] == "Beata",
          "rank_section: falls back to Equipe rank when score is blank (Beata 3rd)")
    check(_class_state(len(rank_only), len(rr)) == "done", "class_state: full Equipe ranking -> done")
    # build_html on a synthetic event
    ev = {"meeting": "NBECH", "date": "2026-06-22", "generated": "10:42", "classes": [
        {"name": "Junior — Team", "start_at": "10:30", "type": "team", "state": "running", "n": 17,
         "swe": [{"no": "3", "rider": "Erla Argus", "horse": "Olympia", "start": "10:42", "score": 71.2, "place": 2},
                 {"no": "7", "rider": "Hildur Elfgren", "horse": "Anemon", "start": "10:51", "score": None, "place": None}],
         "leaders": [{"place": 1, "rider": "Ingse H", "nation": "NOR", "score": 72.1},
                     {"place": 2, "rider": "Erla Argus", "nation": "SWE", "score": 71.2}],
         "teams": [{"rank": 1, "nation": "SWE", "total": 213.4}, {"rank": 2, "nation": "NOR", "total": 210.0}]}]}
    h = build_html(ev)
    check("Erla Argus" in h and "Junior" in h, "build_html: rider + class present")
    if PINNED_LINKS:
        check("class='pins'" in h and PINNED_LINKS[0][1] in h, "build_html: pinned interview link(s) shown on top")
    check("10:42" in h and "10:51" in h and "stt" in h, "build_html: per-rider start times shown")
    check("2.</span> 71.20" in h or "71.20" in h, "build_html: shows the Swedish rider's place + score")
    check("LIVE" in h and "Leaders:" in h, "build_html: running state + leaders block")
    check("213.40" in h and "SWE" in h, "build_html: team standings rendered")
    check("Open field logger" not in h, "build_html: field-logger button removed (saves space)")
    check("location.reload" in build_html(ev, refresh_secs=15) and "15000" in build_html(ev, refresh_secs=15),
          "build_html: refresh interval is parameterised (proxy uses fast refresh)")
    check("gold winners" in h.lower(), "build_html: gold-winner focus")
    check(h.startswith("<!doctype html>") and h.strip().endswith("</html>"), "build_html: full document")
    # merged jumping + eventing (stubbed network): time-sorted, event-tagged, masterlist fallback
    _td = datetime.date.today().strftime("%Y-%m-%d")
    def _mstub(u):
        if u.endswith("/meetings/J/schedule"):
            return {"display_name": "NBECH Jumping 2026", "meeting_classes": [
                {"name": "Senior Individual Final", "discipline": "show_jumping",
                 "start_at": _td + "T09:00:00", "class_sections": [{"id": "j1"}]},
                {"name": "Fam 1.30m", "discipline": "show_jumping",          # untimed warm-up -> 00:00
                 "start_at": _td + "T00:00:00", "class_sections": [{"id": "j0"}]},
                {"name": "Masterlist Seniorer", "discipline": "list",
                 "start_at": _td + "T00:00:00", "class_sections": [{"id": "jm"}]}]}
        if u.endswith("/meetings/E/schedule"):
            return {"display_name": "Strömsholm Eventing 2026", "meeting_classes": [
                {"name": "CCI4*-S", "discipline": "eventing",
                 "start_at": _td + "T08:00:00", "class_sections": [{"id": "e1"}]}]}
        return {"j1": {"starts": []},
                "j0": {"starts": [{"start_no": "1", "rider_name": "Fam Rider", "horse_name": "H3",
                                   "logo_id": "SWE", "start_at": _td + "T00:00:00"}]},
                "jm": {"starts": [{"rider_name": "Sv Senior", "horse_name": "H1", "logo_id": "SWE", "rider_id": 1}]},
                "e1": {"starts": [{"start_no": "401", "rider_name": "Ev Rider", "horse_name": "H2",
                                   "logo_id": "SWE", "start_at": _td + "T08:06:00"}]}}.get(u.rsplit("/", 1)[-1], {"starts": []})
    mev = fetch_merged(["J", "E"], fetch=_mstub)
    check({c["event"] for c in mev["classes"]} == {"Jump", "Event"}, "fetch_merged: both events present")
    check(mev["classes"][0]["event"] == "Event" and mev["classes"][0]["start_at"] == "08:00",
          "fetch_merged: time-sorted (08:00 eventing before 09:00 jumping)")
    check(mev["classes"][-1]["start_at"] == "00:00",
          "fetch_merged: untimed (00:00) classes sort to the bottom, real times on top")
    _jc = [c for c in mev["classes"] if c["event"] == "Jump"][0]
    check(_jc["swe"] and _jc["swe"][0]["rider"] == "Sv Senior",
          "fetch_merged: no-startlist jumping class falls back to its masterlist roster")
    mh = build_html(mev)
    check("Jumping &amp; Eventing" in mh and "Jumping + Eventing" in mh, "merged build_html: subtitle + heading")
    check("Jump · " in mh and "Event · " in mh, "merged build_html: per-class event tag shown")
    # --- domestic (club) meeting: numeric club ids (not nations), team-only Allsvenska class, arena chips ---
    check(_is_nation("SWE") and _is_nation("GER") and not _is_nation("2690") and not _is_nation(""),
          "_is_nation: alpha nation code yes; numeric club id / blank no")
    check(_nat_or_club({"logo_id": "SWE"}) == "SWE"
          and _nat_or_club({"logo_id": "2690", "club_name": "Falsterbo RK"}) == "Falsterbo RK",
          "_nat_or_club: nation kept int'l; numeric club id -> club name domestic")
    def _dstub(u):
        if u.endswith("/meetings/D/schedule"):
            return {"display_name": "Falsterbo Horse Show 2026", "meeting_classes": [
                {"name": "Tour of Amateurs Semifinal", "arena": "Dressyrarenan",
                 "start_at": _td + "T09:00:00", "class_sections": [{"id": "d1"}]},
                {"name": "Folksam Ponnyallsvenska SM-final", "arena": "Nationsbanan",
                 "start_at": _td + "T12:00:00", "class_sections": [{"id": "dt"}]}]}
        return {"d1": {"starts": [
                    {"start_no": "1", "rider_name": "Esther Ansgariusson", "horse_name": "H1",
                     "logo_id": "2690", "club_name": "Falsterbo RK", "rank": 1, "results": []},
                    {"start_no": "2", "rider_name": "Jill Spencer", "horse_name": "H2",
                     "logo_id": "623", "club_name": "Ystad RK", "rank": 2, "results": []},
                    # one foreign visitor in an otherwise-Swedish amateur class - must NOT flip it to int'l
                    {"start_no": "3", "rider_name": "Marie Dupont", "horse_name": "H3",
                     "logo_id": "FRA", "club_name": "", "rank": 3, "results": []}]},
                "dt": {"starts": [
                    {"type": "Team", "club_name": "Skåne", "team_no": 1, "position": 1, "starts": [], "results": []},
                    {"type": "Team", "club_name": "Halland", "team_no": 2, "position": 2, "starts": [], "results": []}]}
               }.get(u.rsplit("/", 1)[-1], {"starts": []})
    dev = fetch_event("D", fetch=_dstub)
    check(any("Ponnyallsvenska" in c["name"] for c in dev["classes"]),
          "fetch_event: TEAM-only (Allsvenska) class is surfaced, not dropped")
    di = [c for c in dev["classes"] if c["type"] == "indiv"][0]
    check(di.get("domestic") is True and di.get("arena") == "Dressyrarenan",
          "fetch_event: domestic flag set (majority rule: 1 foreign visitor doesn't flip it) + arena captured")
    check(di["swe"] == [] and di["leaders"] and di["leaders"][0]["nation"] == "Falsterbo RK",
          "fetch_event: domestic indiv -> no SWE-nation riders, leader shows CLUB name")
    dh = build_html(dev)
    check("🏇 Classes &amp; results" in dh and "🇸🇪 Swedish riders" not in dh,
          "build_html domestic: neutral header (SWE nation lens dropped)")
    check("No Swedish riders" not in dh, "build_html domestic: no misleading 'No Swedish riders' note")
    check("Falsterbo RK" in dh and "Dressyrarenan" in dh and "Nationsbanan" in dh,
          "build_html domestic: club name + arena chips shown")
    check("Skåne" in dh and "Halland" in dh,
          "build_html: Allsvenska team line-up rendered (seeded, pre-score)")
    # a FINISHED meeting (only past-dated classes) must drop off a live day - not sort to the top - and
    # the remaining single live day then gets a real dated label (not the multi-day "Strömsholm" fallback).
    _past = (datetime.date.today() - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    def _mstub2(u):
        if u.endswith("/meetings/J/schedule"):
            return {"display_name": "NBECH Jumping 2026", "meeting_classes": [
                {"name": "Senior Final", "discipline": "show_jumping",
                 "start_at": _td + "T09:00:00", "class_sections": [{"id": "j1"}]}]}
        if u.endswith("/meetings/E/schedule"):
            return {"display_name": "Strömsholm Eventing 2026", "meeting_classes": [
                {"name": "CCI4*-S", "discipline": "eventing",
                 "start_at": _past + "T08:00:00", "class_sections": [{"id": "e1"}]}]}
        return {"j1": {"starts": [{"start_no": "1", "rider_name": "Sv R", "horse_name": "H",
                                   "logo_id": "SWE", "start_at": _td + "T09:00:00"}]},
                "e1": {"starts": [{"start_no": "401", "rider_name": "Ev R", "horse_name": "H2",
                                   "logo_id": "SWE", "start_at": _past + "T08:00:00"}]}}.get(u.rsplit("/", 1)[-1], {"starts": []})
    mev2 = fetch_merged(["J", "E"], fetch=_mstub2)
    check({c["event"] for c in mev2["classes"]} == {"Jump"},
          "fetch_merged: a finished meeting (only past classes) drops off the live day")
    check(mev2["date"] != "all" and mev2["date_label"] != "Strömsholm",
          "fetch_merged: single live day -> real dated label (no Strömsholm fallback)")
    # notifications: a finished class with a Swedish podium fires once, then de-dupes
    import tempfile, os as _os
    st = _os.path.join(tempfile.mkdtemp(), "n.json")
    done_ev = {"classes": [{"name": "Junior — Team", "state": "done", "swe": [
        {"no": "3", "rider": "Erla Argus", "score": 72.5, "place": 1},
        {"no": "7", "rider": "Hildur Elfgren", "score": 66.0, "place": 9}]}]}
    m1 = compute_notifications(done_ev, st)
    check(len(m1) == 1 and "Erla Argus" in m1[0] and "🥇" in m1[0] and "1st" in m1[0],
          "notify: Swedish winner of a finished class fires (gold, 1st)")
    check(compute_notifications(done_ev, st) == [], "notify: same class de-dupes (no repeat)")
    up_ev = {"classes": [{"name": "U25", "state": "upcoming", "swe": [{"no": "2", "rider": "X", "place": None}]}]}
    check(compute_notifications(up_ev, st) == [], "notify: an unfinished class fires nothing")
    print("\nnbech_cheatsheet SELFTEST", "PASS" if ok[0] else "FAIL")
    return 0 if ok[0] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pb = sub.add_parser("build", help="fetch the meeting + write the cheat-sheet HTML")
    pb.add_argument("--meeting", default="80465")
    pb.add_argument("--meetings", help="comma-separated meeting ids -> ONE merged, time-sorted page "
                                       "(e.g. 80632,80567 for jumping + eventing)")
    pb.add_argument("--out", required=True)
    pb.add_argument("--notify-state", dest="notify_state",
                    help="emit 'NOTIFY: ...' lines for newly-finished classes with a Swedish podium (de-duped via this state file)")
    pb.add_argument("--date", help="override 'today' as YYYY-MM-DD to build a specific day's page "
                                   "(e.g. tonight build tomorrow's order); merged page only")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return run_selftest()
    if getattr(args, "meetings", None):
        day = datetime.datetime.strptime(args.date, "%Y-%m-%d").date() if getattr(args, "date", None) else None
        event = fetch_merged([m.strip() for m in args.meetings.split(",") if m.strip()], today=day)
    else:
        event = fetch_event(args.meeting)
    if not event.get("classes"):
        sys.stderr.write("no classes fetched\n"); return 2
    htmltext = build_html(event)
    d = os.path.dirname(args.out)
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as fh:
        fh.write(htmltext)
    n_swe = sum(len(c.get("swe") or []) for c in event["classes"])
    done = sum(1 for c in event["classes"] if c["state"] == "done")
    print("wrote %s  (%d classes, %d done, %d SWE, updated %s)"
          % (args.out, len(event["classes"]), done, n_swe, event["generated"]))
    if args.notify_state:
        for m in compute_notifications(event, args.notify_state):
            print("NOTIFY: " + m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
