#!/usr/bin/env python3
"""equipe_api.py — fetch + parse Equipe startlist JSON and write the enriched section CSV.

Why this exists: the CSV is currently built in PowerShell, which parses the API JSON with
ConvertFrom-Json. PS 5.1's ConvertFrom-Json collapses a JSON array whose objects have
different key sets (exactly what starts[]/horses[]/meeting_classes[] are) into one object
with array-valued props — a real correctness landmine. Python's json has no such bug, so
doing the parse + CSV write here removes that risk. The CSV format is byte-for-byte the same
as PowerShell's Write-StartlistCsv (verified by the cross-language parity test in Test-Smoke);
the row/meta text reuses the already parity-proven CleanText/CsvField from equisport_core.

CLI:  python equipe_api.py csv --section S.json --schedule SCH.json --horses H.json [--section-id ID] [--out F]
      python equipe_api.py startlist --id <sectionId> [--out F]      # live fetch (urllib)
      python equipe_api.py selftest
"""
import argparse
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta

import equisport_core as core  # parity-proven CleanText / CsvField

BASE = "https://online.equipe.com/api/v1"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "Chrome/124.0 Safari/537.36"),
}

# Header row + the official types pulled out, in the exact PowerShell order.
CSV_HEADER = ("St.No,H.no,Rider,Club,Horse,Breed,Born,Sex,Reg.No,Owner,First,Last,"
              "RiderId,HorseId,ClubId,FeiId,Color,Sire,Dam,DamSire,Breeder,Licence")


def _s(v):
    """Interpolate a JSON value the way PowerShell does: None -> '' (no quoting)."""
    return "" if v is None else str(v)


def find_class(schedule, section_id):
    """The meeting_class that owns this section (matches the PS Where {id -eq sectionId})."""
    sid = str(section_id)
    for cls in (schedule.get("meeting_classes") or []):
        for sec in (cls.get("class_sections") or []):
            if str(sec.get("id")) == sid:
                return cls
    return None


def _officials(cls):
    """Collect officials by type; multiple of the same type joined ' / ' (CleanText'd)."""
    out = {}
    for o in (cls.get("officials") or []):
        name = core.clean_text(o.get("official_name"))
        if not name:
            continue
        t = o.get("official_type")
        out[t] = (out[t] + " / " + name) if t in out else name
    return out


def build_lines(section, schedule, cls, horses, section_id):
    """Return the CSV as a list of line strings, byte-identical to Write-StartlistCsv."""
    if cls is None:
        cls = {}
    schedule = schedule or {}
    section = section or {}

    starts = section.get("starts") if isinstance(section, dict) else None
    if starts is None:
        starts = section if isinstance(section, list) else []

    horse_map = {}
    for h in (horses or []):
        horse_map[str(h.get("id"))] = h

    competition = core.clean_text(schedule.get("display_name"))
    class_name = core.clean_text(cls.get("name"))
    fence = _s(cls.get("fence_height"))
    arena = core.clean_text(cls.get("arena")) if cls.get("arena") else ""
    ofc = _officials(cls)

    sec_per_start = _s(section.get("sec_per_start")) if section else ""
    state = _s(section.get("state")) if section else ""
    entries = _s(section.get("total")) if section else ""
    rounds = section.get("rounds") if section else None
    max_time = _s(rounds[0].get("max_time")) if (rounds and rounds[0].get("max_time")) else ""

    lines = [
        competition,
        class_name,
        "Arena," + arena,
        "Date," + _s(schedule.get("start_on")) + " to " + _s(schedule.get("end_on")),
        "Start time," + _s(cls.get("display_time")),
        "Start at," + _s(cls.get("start_at")),
        "Class no," + _s(cls.get("class_no")),
        "Fence height," + ((fence + "m") if fence else ""),
        "Discipline," + _s(cls.get("discipline")),
        "Status," + _s(cls.get("status")),
        "Sec per start," + sec_per_start,
        "Max time," + max_time,
        "State," + state,
        "Entries," + entries,
        "Venue country," + _s(schedule.get("venue_country")),
        "Team," + _s(schedule.get("team")),
        "Organizer url," + _s(schedule.get("organizer_url")),
        "Chief judge," + ofc.get("chief_judge", ""),
        "Course designer," + ofc.get("course_designer", ""),
        "Course designer assistant," + ofc.get("course_designer_assistant", ""),
        "Show director," + ofc.get("show_director", ""),
        "Jumping judge," + ofc.get("show_jumping_judge", ""),
        "Press manager," + ofc.get("press_manager", ""),
        "",
        CSV_HEADER,
    ]

    cf = core.csv_field
    for st in sorted(starts, key=lambda s: int(s.get("start_no") or 0)):
        hid = st.get("horse_id")
        h = horse_map.get(str(hid)) if hid is not None else None
        h = h or {}
        row = ",".join([
            cf(st.get("start_no")), cf(st.get("horse_combination_no")), cf(st.get("rider_name")),
            cf(st.get("club_name")), cf(st.get("horse_name")),
            cf(h.get("breed")), cf(h.get("born_year")), cf(h.get("sex")), cf(h.get("reg_no")),
            cf(h.get("owner")), cf(st.get("rider_first_name")), cf(st.get("rider_last_name")),
            cf(st.get("rider_id")), cf(st.get("horse_id")), cf(st.get("club_id")),
            cf(h.get("fei_id")), cf(h.get("color")), cf(h.get("sire")), cf(h.get("dam")),
            cf(h.get("dam_sire")), cf(h.get("breeder")), cf(h.get("licence")),
        ])
        lines.append(row)
    return lines


# ── meeting plan (Tool 1: folder tree for a whole meeting) ────────────────────
# Folder NAMES come from the parity-proven equisport_core helpers (get_class_folder_name,
# get_canon_folder_name, to_title_name, limit_words), so the structure is byte-identical to
# the PowerShell tool. PowerShell still creates the directories from this plan.

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PLAN_DISCIPLINES = ("show_jumping", "dressage", "eventing")


def parse_date(s):
    """Parse an Equipe date ('YYYY-MM-DD' or an ISO datetime) to a date; None if unparseable."""
    s = ("" if s is None else str(s)).strip()
    if not s:
        return None
    head = s.replace("T", " ").split(" ")[0]
    try:
        return datetime.strptime(head, "%Y-%m-%d").date()
    except ValueError:
        try:
            return datetime.fromisoformat(s).date()
        except ValueError:
            return None


def build_meeting_plan(meeting_id, schedule, horses, section_fetcher, csv_root, write_csv_files=True):
    """Build the folder/CSV plan for a whole meeting. section_fetcher(section_id)->section|None
    is injected so the same logic serves both live fetch and tests. Returns a JSON-able dict;
    writes each published section's CSV when write_csv_files is set."""
    schedule = schedule or {}
    horses = horses or []
    competition = core.clean_text(schedule.get("display_name"))
    comp_folder = core.limit_words(core.to_title_name(competition), 40)
    date_from = schedule.get("start_on") or ""
    dt_from = parse_date(date_from)

    classes = [c for c in (schedule.get("meeting_classes") or [])
               if c.get("discipline") in PLAN_DISCIPLINES]

    entries = []
    for cls in classes:
        class_date_str = cls.get("date") or cls.get("start_date") or date_from
        dt_class = parse_date(class_date_str)
        if dt_class and dt_from:
            day_num = (dt_class - dt_from).days + 1
            dow = WEEKDAYS[dt_class.weekday()]
        else:
            day_num, dow = 1, "Unknown"
        day_folder = "Day_%d_%s" % (day_num, dow)
        class_folder_base = core.get_class_folder_name(
            cls.get("fence_height"), cls.get("class_no"), cls.get("name"))
        class_no = str(cls.get("class_no")) if cls.get("class_no") else "0"

        for section in (cls.get("class_sections") or []):
            section_id = str(section.get("id"))
            section_obj = section
            starts = section.get("starts")
            if starts:
                if starts[0].get("class_section_id"):
                    section_id = str(starts[0]["class_section_id"])
            else:
                sdata = section_fetcher(section_id) if section_fetcher else None
                if sdata:
                    section_obj = sdata
                    starts = (sdata.get("starts") if isinstance(sdata, dict) else sdata) \
                        or (sdata.get("entries") if isinstance(sdata, dict) else None) or []
                    if starts and starts[0].get("class_section_id"):
                        section_id = str(starts[0]["class_section_id"])
            riders = sorted(starts or [], key=lambda s: int(s.get("start_no") or 0))
            class_folder = "%s_%s" % (class_folder_base, section_id)

            no_riders = len(riders) == 0
            all_info = (not no_riders) and all(int(r.get("start_no") or 0) == 0 for r in riders)

            csv_path, csv_existed = "", False
            if not no_riders and not all_info and write_csv_files:
                lines = build_lines(section_obj, schedule, cls, horses, section_id)
                mid = (str(riders[0].get("meeting_id")) if riders[0].get("meeting_id") else "0")
                csv_path = os.path.join(csv_root, mid, "%s.csv" % section_id)
                d = os.path.dirname(csv_path)
                if d:
                    os.makedirs(d, exist_ok=True)
                csv_existed = os.path.exists(csv_path)
                write_csv(lines, csv_path)

            sort_key = (dt_class.strftime("%Y%m%d") if dt_class else "00000000") + class_no.zfill(5)
            entries.append({
                "sort_key": sort_key, "day_num": day_num, "day_folder": day_folder,
                "class_folder": class_folder, "class_no": class_no, "section_id": section_id,
                "no_riders": no_riders, "all_info": all_info,
                "csv_path": csv_path, "csv_existed": csv_existed,
                "riders": [{"canon": core.get_canon_folder_name(r.get("rider_name"), r.get("start_no") or 0),
                            "rider_name": r.get("rider_name"), "start_no": r.get("start_no")}
                           for r in riders],
            })

    entries.sort(key=lambda e: e["sort_key"])
    return {
        "meeting_id": str(meeting_id), "competition": competition, "comp_folder": comp_folder,
        "date_from": date_from, "date_to": schedule.get("end_on") or "",
        "classes": len(classes), "entries": entries,
    }


# ── section results (Tools 7/8/10/11 + auto-sort) ────────────────────────────
# Same /class_sections/{id} endpoint as the startlist, read for the per-rider RESULT fields
# (result_at, result_rank, results[]...). Normalizing every start to the SAME key set makes the
# array homogeneous, so PowerShell can ConvertFrom-Json it safely (the heterogeneous starts[]
# array is exactly what corrupts under PS 5.1). Field names are preserved, so the PS callers'
# $s.result_at / $s.results / $s.result_rank reads are unchanged.

RESULT_KEYS = ("start_no", "horse_combination_no", "rider_id", "horse_id", "club_id",
               "rider_name", "horse_name", "club_name", "result_at", "result_rank",
               "result_status", "result_preview", "rank", "placed", "ridden",
               "meeting_id", "class_section_id")


def build_results(section):
    """Normalize a section's starts to a homogeneous list (every start has the same keys),
    preserving API field names + the nested results[] array. Returns {section_id, starts}."""
    starts = section.get("starts") if isinstance(section, dict) else section
    starts = starts or []
    out = []
    for s in starts:
        row = {k: s.get(k) for k in RESULT_KEYS}
        row["results"] = s.get("results") or []
        out.append(row)
    sid = str(section.get("id")) if isinstance(section, dict) and section.get("id") is not None else ""
    return {"section_id": sid, "starts": out}


# ── meeting discovery (find which competition a date/venue belongs to) ────────
# The /meetings list endpoint ignores date/search query params but DOES paginate
# (?page=N&per_page=M) newest-first, so we page back until we cover the target date.
_COUNTRY_ALIAS = {"SE": "SWE", "SWE": "SWE", "NO": "NOR", "NOR": "NOR", "DK": "DEN", "DEN": "DEN",
                  "FI": "FIN", "FIN": "FIN", "GB": "GBR", "UK": "GBR", "GBR": "GBR"}


def _norm_country(c):
    if not c:
        return None
    return _COUNTRY_ALIAS.get(c.strip().upper(), c.strip().upper())


# ── folder-name hints ─────────────────────────────────────────────────────────
# The dump folder is usually named with clues to the competition (e.g. "260506_Flyinge_SM",
# "SM Kval omg 1") — not spelled exactly like the official name, but some keywords overlap. We
# extract the meaningful tokens and use them as a SOFT ranking signal to disambiguate when several
# meetings share a date (it never excludes a meeting, so a misleading folder name can't hide the
# real one). Tokens + the haystack are transliterated, so "Bastad" still matches "Båstad".
_HINT_NOISE = {
    "omg", "omgang", "kval", "kvalificering", "runda", "round", "del", "final", "finals", "dag",
    "day", "part", "raw", "export", "exports", "jpg", "jpeg", "foto", "foton", "photos", "photo",
    "pics", "pictures", "bilder", "bild", "kort", "heat", "class", "klass", "competition", "comp",
    "event", "the", "och", "and", "for", "till", "med",
    # reverse-geocode noise (so GPS place strings contribute only real place names)
    "kommun", "lan", "municipality", "county", "parish", "socken", "sverige", "sweden", "sn",
}


def folder_hints(text):
    """Meaningful keyword tokens from a folder name: drop dates/numbers, 1-char tokens and a small
    noise list (omg/kval/runda/bilder/...). Transliterated to ASCII first (so accents fold and any
    combining marks are stripped), then lowercased, de-duplicated, original order."""
    out = []
    for t in re.split(r"[^0-9A-Za-z]+", core.transliterate(str(text or ""))):
        tl = t.lower()
        if not tl or len(tl) < 2 or tl.isdigit() or re.fullmatch(r"\d{2,8}", tl) or tl in _HINT_NOISE:
            continue
        if tl not in out:
            out.append(tl)
    return out


def hint_score(haystack, hints):
    """How many of the hint tokens appear (whole-word, accent-insensitive) in the haystack text."""
    if not hints:
        return 0
    hay = core.transliterate(str(haystack or "")).lower()
    score = 0
    for t in hints:
        tt = core.transliterate(t).lower()
        if re.search(r"\b" + re.escape(tt) + r"\b", hay):
            score += 1
    return score

# Photo GPS is reverse-geocoded to a place name by the PowerShell side (Get-PlaceFromGps, shared with
# Tools 19/22 — it prioritises equestrian/stadium venues), then appended to the --hint string here;
# folder_hints tokenises it and the _HINT_NOISE list above drops the kommun/lan/Sverige filler.


def match_meeting(m, date=None, country=None, name=None, discipline=None):
    """True if a meeting-list record matches the filters. date is matched against the
    [start_on, end_on] span (a multi-day meeting covers each of its days)."""
    if country and (m.get("venue_country") or "").upper() != _norm_country(country):
        return False
    if date:
        start = str(m.get("start_on") or "")
        end = str(m.get("end_on") or "") or start
        if not (start <= date <= end):
            return False
    if name:
        hay = (str(m.get("display_name") or "") + " " + str(m.get("name") or "")).lower()
        if name.lower() not in hay:
            return False
    if discipline:
        discs = m.get("disciplines") or ([m.get("discipline")] if m.get("discipline") else [])
        if discipline not in discs:
            return False
    return True


def classes_on_date(schedule, date):
    """Real competition classes (jumping/dressage/eventing) whose date/start_at falls on `date`,
    sorted by start time — the candidate classes a photo from that day could belong to."""
    out = []
    for mc in (schedule.get("meeting_classes") or []):
        if mc.get("discipline") not in ("show_jumping", "dressage", "eventing"):
            continue
        d = str(mc.get("date") or mc.get("start_at") or "")
        if not d.startswith(date):
            continue
        out.append({
            "class_no": mc.get("class_no"), "name": core.clean_text(mc.get("name")),
            "fence_height": mc.get("fence_height"), "arena": mc.get("arena"),
            "start_at": mc.get("start_at"), "status": mc.get("status"),
            "sections": [cs.get("id") for cs in (mc.get("class_sections") or [])],
        })
    out.sort(key=lambda c: str(c.get("start_at") or ""))
    return out


def parse_start_at(s):
    """'2026-05-06 18:40:00 +0200' (or ...T...) -> naive datetime, or None. The TZ offset is
    dropped: camera EXIF (local) and start_at (local +TZ) are the same wall-clock, so naive compare."""
    if not s:
        return None
    txt = str(s).strip().replace("T", " ")[:19]
    try:
        return datetime.strptime(txt, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _at(date, hhmm):
    """date 'YYYY-MM-DD' + 'HH:MM' or 'HH:MM:SS' -> datetime."""
    t = hhmm if hhmm.count(":") == 2 else hhmm + ":00"
    return datetime.strptime(date + " " + t, "%Y-%m-%d %H:%M:%S")


def _overlap_min(a0, a1, b0, b1):
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds()) / 60.0


def identify_classes(classes, p_start, p_end, tail_hours=2):
    """Match a photo shot-time window [p_start, p_end] to the day's classes. Each class runs from its
    start_at until the NEXT class starts (the last gets a short tail). A class must overlap the photo
    window; results are ranked by how closely the class START aligns with when shooting began (you
    start shooting a class as it starts) — NOT raw overlap, because a small meeting's final class
    would otherwise get a long tail that falsely swallows a later, unrelated window. classes[0] is
    the best guess."""
    parsed = sorted(
        ((parse_start_at(c.get("start_at")), c) for c in classes if parse_start_at(c.get("start_at"))),
        key=lambda x: x[0])
    out = []
    for i, (dt, c) in enumerate(parsed):
        win_end = parsed[i + 1][0] if i + 1 < len(parsed) else dt + timedelta(hours=tail_hours)
        ov = _overlap_min(dt, win_end, p_start, p_end)
        if ov > 0:
            rec = dict(c)
            rec["overlap_min"] = round(ov, 1)
            rec["start_delta_min"] = round(abs((dt - p_start).total_seconds()) / 60.0, 1)
            out.append(rec)
    out.sort(key=lambda c: (c["start_delta_min"], -c["overlap_min"]))
    return out


def fetch_meetings(target_date=None, max_pages=6, per_page=2000, fetch=None):
    """Page the /meetings list back until it reaches `target_date` (or max_pages). Returns a list
    of unique meeting records. `fetch` is injectable for tests (defaults to safe_fetch)."""
    fetch = fetch or safe_fetch
    seen = {}
    for page in range(1, max_pages + 1):
        batch = fetch("%s/meetings?per_page=%d&page=%d" % (BASE, per_page, page))
        if not batch:
            break
        for m in batch:
            if isinstance(m, dict) and "id" in m:
                seen[m["id"]] = m
        if target_date:
            earliest = min((str(x.get("start_on") or "9999") for x in batch), default="9999")
            if earliest <= target_date:   # this page already reaches back past the target
                break
    return list(seen.values())


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def safe_fetch(url):
    """fetch_json but returns None on any error (network / decode), like PS FetchJson."""
    try:
        return fetch_json(url)
    except Exception as e:  # noqa: BLE001 - mirror PS FetchJson swallow-and-continue
        sys.stderr.write("  ERROR fetching %s: %s\n" % (url, e))
        return None


def _load(path):
    with io.open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_csv(lines, out_path):
    # UTF-8 (no BOM), LF. Downstream readers use -Encoding UTF8 / utf-8-sig, both tolerant.
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def run_selftest():
    fails = []

    def check(c, m):
        if not c:
            fails.append(m)

    schedule = {"display_name": "Demo Show", "start_on": "2026-06-04", "end_on": "2026-06-07",
                "venue_country": "SWE", "team": "", "organizer_url": "http://x",
                "meeting_classes": [{
                    "name": "1.35 A", "class_no": 3, "fence_height": "1.35", "discipline": "show_jumping",
                    "status": "national", "arena": "Main, Ring", "start_at": "2026-06-04 10:00",
                    "display_time": "", "class_sections": [{"id": 555}],
                    "officials": [{"official_type": "chief_judge", "official_name": "Anna Pihl"},
                                  {"official_type": "course_designer", "official_name": "Fredrik Malm"},
                                  {"official_type": "course_designer", "official_name": "Stina L"}]}]}
    horses = [{"id": 7811410, "breed": "SH", "born_year": 2019, "owner": "Owner AB", "reg_no": "04194045",
               "sex": "valack", "color": "br", "sire": "Diamant de Semilly (SF)", "dam": "Helena",
               "dam_sire": "Casall", "breeder": "H-J Gerken", "fei_id": "109CF05", "licence": "345128"}]
    section = {"sec_per_start": 103, "state": "starts", "total": 2, "rounds": [{"max_time": 180}],
               "starts": [
                   {"start_no": 2, "horse_combination_no": 463, "rider_name": "B, Rider", "club_name": "C",
                    "horse_name": "NoHorse", "rider_first_name": "B", "rider_last_name": "Rider",
                    "rider_id": 2, "horse_id": 99, "club_id": 7, "meeting_id": 1},
                   {"start_no": 1, "horse_combination_no": 485, "rider_name": "Nicole Holmen",
                    "club_name": "Osterlens", "horse_name": "Wallflower Maximus", "rider_first_name": "Nicole",
                    "rider_last_name": "Holmen", "rider_id": 6407576, "horse_id": 7811410, "club_id": 1844409,
                    "meeting_id": 1}]}

    cls = find_class(schedule, 555)
    check(cls is not None and cls["name"] == "1.35 A", "find_class matches by section id")
    lines = build_lines(section, schedule, cls, horses, 555)
    check(lines[0] == "Demo Show", "line0 competition")
    check(lines[2] == "Arena,Main, Ring", "arena written raw (embedded comma, not quoted)")
    check("Fence height,1.35m" in lines, "fence height gets m suffix")
    check("Sec per start,103" in lines, "section sec_per_start")
    check("Chief judge,Anna Pihl" in lines, "official by type")
    check("Course designer,Fredrik Malm / Stina L" in lines, "same-type officials joined")
    check(lines[24] == CSV_HEADER, "header row at expected index")
    # rows sorted by start_no; first data row is St.No 1, fields CsvField-quoted where needed
    r1 = lines[25]
    check(r1.startswith("1,485,Nicole Holmen,Osterlens,Wallflower Maximus,SH,2019,valack,04194045,Owner AB,"),
          "row1 rider+horse joined fields")
    check(r1.endswith(",109CF05,br,Diamant de Semilly (SF),Helena,Casall,H-J Gerken,345128"),
          "row1 pedigree tail")
    r2 = lines[26]
    check(r2.startswith('2,463,"B, Rider",C,NoHorse,'), "row2 rider name with comma is quoted")
    check(",,,,,," in r2 or r2.count(",,") >= 1, "row2 missing horse -> blank horse fields")

    # --- meeting plan (Tool 1) ----------------------------------------------
    check(parse_date("2026-06-05") is not None and parse_date("bad") is None, "parse_date")
    msched = {
        "display_name": "Demo Show", "start_on": "2026-06-04", "end_on": "2026-06-07",
        "meeting_classes": [
            {"name": "1.35 A", "class_no": 3, "fence_height": "1.35", "discipline": "show_jumping",
             "date": "2026-06-05", "class_sections": [{"id": 555, "starts": [
                 {"start_no": 1, "rider_name": "Nicole Holmen", "horse_id": 7811410,
                  "meeting_id": 79689, "class_section_id": 555}]}]},
            {"name": "Prize Giving", "class_no": 9, "discipline": "list",
             "class_sections": [{"id": 1}]},
            {"name": "0.90 B", "class_no": 1, "fence_height": "0.90", "discipline": "show_jumping",
             "date": "2026-06-04", "class_sections": [{"id": 777, "starts": []}]}],
    }
    plan = build_meeting_plan("79689", msched, horses, lambda sid: None, "", write_csv_files=False)
    check(plan["comp_folder"] == "Demo_Show", "meeting comp folder via ToTitleName/LimitWords")
    check(plan["classes"] == 2, "meeting filters to jumping/dressage/eventing (list dropped)")
    em = {x["section_id"]: x for x in plan["entries"]}
    check("555" in em and "777" in em, "both jumping sections present")
    check(em["555"]["day_folder"] == "Day_2_" + WEEKDAYS[parse_date("2026-06-05").weekday()],
          "day folder = day index + weekday")
    check(em["555"]["class_folder"] == core.get_class_folder_name("1.35", 3, "1.35 A") + "_555",
          "class folder = Get-ClassFolderName + section id")
    check(em["555"]["riders"][0]["canon"] == core.get_canon_folder_name("Nicole Holmen", 1),
          "rider canon folder via Get-CanonFolderName")
    check(em["777"]["no_riders"] is True, "empty section flagged no_riders")
    check(plan["entries"][0]["section_id"] == "777", "entries sorted by date then class no")

    # --- normalized results (Tools 7/8/10/11) -------------------------------
    rsec = {"id": 1242957, "starts": [
        {"start_no": 1, "rider_name": "A", "horse_id": 9, "result_at": "2026-06-04 11:00:00 +0200",
         "result_rank": "1", "result_status": "OK", "results": [{"faults": 0, "time": 68.9}]},
        {"start_no": 2, "rider_name": "B", "horse_id": 8}]}  # un-run: many keys absent
    rr = build_results(rsec)
    check(rr["section_id"] == "1242957", "results section id")
    check(len(rr["starts"]) == 2, "results: 2 starts")
    check(set(rr["starts"][0].keys()) == set(rr["starts"][1].keys()),
          "results: homogeneous keys (safe for PS ConvertFrom-Json)")
    check(rr["starts"][0]["result_at"] and rr["starts"][1]["result_at"] is None,
          "results: missing result_at filled as null")
    check(rr["starts"][0]["results"][0]["faults"] == 0 and rr["starts"][1]["results"] == [],
          "results: nested results preserved / empty when absent")

    # ── meeting discovery (find) ─────────────────────────────────────────────
    sm = {"id": 78915, "display_name": "Folksam Hopp-SM ...", "start_on": "2026-05-05",
          "end_on": "2026-05-10", "venue_country": "SWE", "disciplines": ["show_jumping"]}
    other = {"id": 1, "display_name": "Kungsbacka Ridklubb", "start_on": "2026-05-06",
             "end_on": "2026-05-06", "venue_country": "SWE", "disciplines": ["show_jumping"]}
    nl = {"id": 2, "display_name": "x", "start_on": "2026-05-06", "end_on": "2026-05-06",
          "venue_country": "NED", "disciplines": ["dressage"]}
    check(match_meeting(sm, date="2026-05-06"), "find: multi-day span covers an inner date")
    check(not match_meeting(sm, date="2026-05-11"), "find: date outside span rejected")
    check(match_meeting(sm, country="SE"), "find: country alias SE -> SWE matches")
    check(not match_meeting(nl, country="SWE"), "find: wrong country rejected")
    check(match_meeting(sm, name="hopp-sm"), "find: name substring (case-insensitive)")
    check(match_meeting(sm, discipline="show_jumping") and not match_meeting(nl, discipline="show_jumping"),
          "find: discipline filter")
    pages = {1: [other, nl], 2: [sm]}   # page 1 newest (2026-05-06), page 2 reaches 2026-05-05
    fake = lambda url: pages.get(int(url.split("&page=")[1]), [])
    got = fetch_meetings(target_date="2026-05-05", fetch=fake)
    check(len(got) == 3, "find: pagination collects across pages + dedups")
    cands = [m for m in got if match_meeting(m, date="2026-05-06", country="SWE")]
    check({m["id"] for m in cands} == {78915, 1}, "find: SWE meetings on 2026-05-06 (not NED)")
    sched = {"meeting_classes": [
        {"discipline": "show_jumping", "class_no": 11, "name": "Folksam Senior SM omg. 1",
         "date": "2026-05-06", "start_at": "2026-05-06 18:40:00 +0200", "fence_height": "1.50",
         "arena": "Fredricson Arena, Flyinge", "status": "elite", "class_sections": [{"id": 1230496}]},
        {"discipline": "show_jumping", "class_no": 6, "name": "warmup", "date": "2026-05-06",
         "start_at": "2026-05-06 10:00:00 +0200", "class_sections": [{"id": 1}]},
        {"discipline": "list", "class_no": None, "name": "divider", "date": "2026-05-06"},
        {"discipline": "show_jumping", "class_no": 20, "name": "next day", "date": "2026-05-07"},
    ]}
    cls = classes_on_date(sched, "2026-05-06")
    check(len(cls) == 2 and cls[0]["class_no"] == 6 and cls[1]["class_no"] == 11,
          "find: classes_on_date filters by day, sorts by start_at, drops divider/other days")
    check(cls[1]["sections"] == [1230496], "find: class section id surfaced for rider matching")

    # ── identify (match a shot-time window to the class) ─────────────────────
    check(parse_start_at("2026-05-06 18:40:00 +0200") == datetime(2026, 5, 6, 18, 40, 0), "identify: parse start_at (drops TZ)")
    check(parse_start_at("") is None and parse_start_at("bad") is None, "identify: bad start_at -> None")
    day = [
        {"class_no": 9, "name": "Robin Z Tour", "start_at": "2026-05-06 15:45:00 +0200", "sections": [1230494]},
        {"class_no": 10, "name": "Young Rider SM", "start_at": "2026-05-06 17:30:00 +0200", "sections": [1230495]},
        {"class_no": 11, "name": "Senior SM omg. 1", "start_at": "2026-05-06 18:40:00 +0200", "sections": [1230496]},
    ]
    m = identify_classes(day, datetime(2026, 5, 6, 18, 40, 12), datetime(2026, 5, 6, 19, 21, 30))
    check(m and m[0]["class_no"] == 11, "identify: 18:40-19:21 -> class 11 (Senior SM)")
    check(m[0]["sections"] == [1230496], "identify: best class carries its section id")
    # a window that straddles two classes overlaps both, ranked by overlap
    m2 = identify_classes(day, datetime(2026, 5, 6, 17, 20, 0), datetime(2026, 5, 6, 17, 50, 0))
    nos = [c["class_no"] for c in m2]
    check(9 in nos and 10 in nos, "identify: straddling window overlaps both classes")
    check(m2[0]["class_no"] == 10, "identify: ranks the dominant-overlap class first")
    check(identify_classes(day, datetime(2026, 5, 7, 10, 0, 0), datetime(2026, 5, 7, 11, 0, 0)) == [],
          "identify: a different day -> no match")
    check(_at("2026-05-06", "18:40") == datetime(2026, 5, 6, 18, 40, 0), "identify: _at HH:MM")

    # ── folder-name hints (disambiguate which competition) ───────────────────
    check(folder_hints("260506_Flyinge_SM") == ["flyinge", "sm"], "hint: drops the date, keeps Flyinge/SM")
    check(folder_hints("SM Kval omg 1") == ["sm"], "hint: drops noise words (Kval/omg) and the number")
    check(folder_hints("") == [] and folder_hints(None) == [], "hint: empty -> no tokens")
    check(folder_hints("Bastad Outdoor 2025") == ["bastad", "outdoor"], "hint: keeps venue + descriptor, drops year")
    h = folder_hints("260506_Flyinge_SM")
    check(hint_score("Folksam Hopp-SM ... arena Fredricson Arena, Flyinge SWE", h) == 2,
          "hint: scores both Flyinge (in arena) and SM (in name)")
    check(hint_score("Bastad Outdoor", folder_hints("Båstad")) == 1,
          "hint: accent-insensitive (decomposed Bastad matches Bastad)")
    check(hint_score("Falsterbo Horse Show", h) == 0, "hint: unrelated meeting scores 0 (never excluded)")
    check(hint_score("anything", []) == 0, "hint: no hints -> score 0 (ranking unchanged)")
    # a reverse-geocoded GPS place string (from PS Get-PlaceFromGps) is folded into --hint: the kommun/
    # lan/Sverige filler is dropped, the real place names survive and can match a meeting's venue text.
    gph = folder_hints("Flyinge, Lunds kommun, Skane lan, Sverige")
    check(gph == ["flyinge", "lunds", "skane"], "hint: GPS place string -> place tokens (filler dropped)")
    check(hint_score("Folksam Hopp-SM, Fredricson Arena, Flyinge", gph) >= 1, "hint: GPS place can match the venue text")

    if fails:
        print("SELFTEST: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("equipe_api SELFTEST: PASS (%d checks)" % 56)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Equipe startlist -> enriched section CSV")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("csv", help="build the CSV from cached JSON files")
    pc.add_argument("--section", required=True)
    pc.add_argument("--schedule", required=True)
    pc.add_argument("--horses", required=True)
    pc.add_argument("--section-id", dest="section_id")
    pc.add_argument("--out")

    ps = sub.add_parser("startlist", help="fetch live + build the CSV")
    ps.add_argument("--id", required=True)
    ps.add_argument("--root", default="meetings")
    ps.add_argument("--out")
    ps.add_argument("--fallback-competition", default="")
    ps.add_argument("--fallback-class", default="")
    ps.add_argument("--json", action="store_true")

    pm = sub.add_parser("meeting", help="fetch a whole meeting; write section CSVs; emit a folder plan")
    pm.add_argument("--id", required=True)
    pm.add_argument("--root", default="meetings")

    pr2 = sub.add_parser("results", help="fetch a section; emit normalized per-rider results JSON")
    pr2.add_argument("--id", required=True)

    pf = sub.add_parser("find", help="find which meeting/competition a date (+venue) belongs to")
    pf.add_argument("--date", required=True, help="YYYY-MM-DD (e.g. a photo's shot date)")
    pf.add_argument("--country", help="venue country, e.g. SE / SWE (default: any)")
    pf.add_argument("--name", help="filter by name substring, e.g. Flyinge / SM")
    pf.add_argument("--discipline", choices=["show_jumping", "dressage", "eventing"])
    pf.add_argument("--classes", action="store_true", help="also list that day's classes (+section ids)")
    pf.add_argument("--hint", help="folder name / keywords (incl. a GPS place name) to rank meetings by")
    pf.add_argument("--pages", type=int, default=6, help="max meeting-list pages to page back through")
    pf.add_argument("--json", action="store_true")

    pi = sub.add_parser("identify", help="match a date + shot-time window to the exact meeting + class + section")
    pi.add_argument("--date", required=True, help="YYYY-MM-DD")
    pi.add_argument("--from", dest="frm", required=True, help="earliest shot time HH:MM[:SS]")
    pi.add_argument("--to", dest="to", required=True, help="latest shot time HH:MM[:SS]")
    pi.add_argument("--country", help="venue country, e.g. SE (default: any)")
    pi.add_argument("--name", help="name substring filter")
    pi.add_argument("--discipline", choices=["show_jumping", "dressage", "eventing"])
    pi.add_argument("--hint", help="folder name / keywords (incl. a GPS place name) to disambiguate the meeting")
    pi.add_argument("--pages", type=int, default=6)
    pi.add_argument("--json", action="store_true")

    sub.add_parser("selftest")

    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return run_selftest()

    if args.cmd == "csv":
        section = _load(args.section)
        schedule = _load(args.schedule)
        horses = _load(args.horses)
        section_id = args.section_id
        if section_id is None:
            section_id = str(section.get("id")) if isinstance(section, dict) else ""
        cls = find_class(schedule, section_id)
        lines = build_lines(section, schedule, cls, horses, section_id)
        if args.out:
            write_csv(lines, args.out)
            print("Wrote %d line(s) to %s" % (len(lines), args.out))
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print("\n".join(lines))
        return 0

    if args.cmd == "startlist":
        sid = args.id
        section = safe_fetch("%s/class_sections/%s" % (BASE, sid))
        if section is None:
            sys.stderr.write("Could not fetch section %s\n" % sid)
            return 2
        starts = section.get("starts") if isinstance(section, dict) else section
        starts = starts or []
        meeting_id = (starts[0].get("meeting_id") if starts else None) or "0"
        schedule = safe_fetch("%s/meetings/%s/schedule" % (BASE, meeting_id)) if meeting_id != "0" else None
        horses = safe_fetch("%s/meetings/%s/horses" % (BASE, meeting_id)) if meeting_id != "0" else None
        cls = find_class(schedule, sid) if schedule else None
        # Mirror the PowerShell fallback: if schedule/class are unavailable, still write a CSV
        # using the names scraped from the startlist HTML page (passed by the caller).
        if not schedule:
            schedule = {"display_name": args.fallback_competition, "start_on": "", "end_on": "",
                        "venue_country": "", "team": "", "organizer_url": ""}
        if cls is None:
            cls = {"name": args.fallback_class, "class_no": "", "fence_height": "", "display_time": "",
                   "arena": "", "start_at": "", "discipline": "", "status": "", "officials": []}
        lines = build_lines(section, schedule, cls, horses or [], sid)
        out = args.out or os.path.join(args.root, str(meeting_id), "%s.csv" % sid)
        d = os.path.dirname(out)
        if d:
            os.makedirs(d, exist_ok=True)
        existed = os.path.exists(out)
        write_csv(lines, out)
        info = {"path": out, "riders": len(starts), "existed": existed, "meeting_id": str(meeting_id)}
        if args.json:
            print(json.dumps(info, ensure_ascii=True))
        else:
            print("Wrote %d riders to %s" % (info["riders"], out))
        return 0

    if args.cmd == "meeting":
        mid = args.id
        schedule = safe_fetch("%s/meetings/%s/schedule" % (BASE, mid))
        if not schedule:
            sys.stderr.write("Could not fetch schedule for meeting %s\n" % mid)
            return 2
        horses = safe_fetch("%s/meetings/%s/horses" % (BASE, mid)) or []
        plan = build_meeting_plan(mid, schedule, horses,
                                  lambda sid: safe_fetch("%s/class_sections/%s" % (BASE, sid)),
                                  args.root)
        print(json.dumps(plan, ensure_ascii=True))
        return 0

    if args.cmd == "results":
        section = safe_fetch("%s/class_sections/%s" % (BASE, args.id))
        if section is None:
            sys.stderr.write("Could not fetch section %s\n" % args.id)
            return 2
        print(json.dumps(build_results(section), ensure_ascii=True))
        return 0

    if args.cmd == "find":
        meetings = fetch_meetings(target_date=args.date, max_pages=args.pages)
        if not meetings:
            sys.stderr.write("Could not fetch the Equipe meetings list.\n")
            return 2
        cands = [m for m in meetings if match_meeting(
            m, date=args.date, country=args.country, name=args.name, discipline=args.discipline)]
        cands.sort(key=lambda m: (str(m.get("display_name") or "")))
        hints = folder_hints(getattr(args, "hint", None))
        out = []
        for m in cands:
            rec = {"id": m.get("id"), "display_name": core.clean_text(m.get("display_name")),
                   "start_on": m.get("start_on"), "end_on": m.get("end_on"),
                   "disciplines": m.get("disciplines"), "statuses": m.get("statuses"),
                   "venue_country": m.get("venue_country"), "tdb_id": m.get("tdb_id")}
            if args.classes:
                sched = safe_fetch("%s/meetings/%s/schedule" % (BASE, m.get("id")))
                rec["classes"] = classes_on_date(sched, args.date) if sched else []
            hay = " ".join([rec["display_name"], str(rec["venue_country"] or "")]
                           + [str(c.get("arena") or "") for c in rec.get("classes", [])])
            rec["name_score"] = hint_score(hay, hints)
            out.append(rec)
        # folder-name match first (when a --hint is given), then by name for stable ordering
        out.sort(key=lambda r: (-r["name_score"], str(r["display_name"] or "")))
        if args.json:
            # ASCII-safe (\uXXXX): PowerShell captures native stdout via the console OEM codepage,
            # which mangles raw UTF-8 (ö -> ├╢). ConvertFrom-Json restores the real chars from \uXXXX.
            print(json.dumps({"date": args.date, "count": len(out), "meetings": out}, ensure_ascii=True))
            return 0
        sys.stdout.reconfigure(encoding="utf-8")
        if not out:
            print("No meeting found for %s%s." % (args.date, " in " + args.country if args.country else ""))
            return 0
        print("Meetings on %s%s:" % (args.date, " (" + args.country + ")" if args.country else ""))
        for m in out:
            print("  %-7s %s..%s  %-22s %s  [%s]  tdb=%s" % (
                m["id"], m["start_on"], m["end_on"], ",".join(m.get("disciplines") or []),
                m["display_name"], ",".join(m.get("statuses") or []), m.get("tdb_id")))
            for c in m.get("classes", []):
                print("        cls %-4s %-6s %-28s %s  sect=%s" % (
                    c["class_no"], c.get("fence_height") or "", str(c["name"])[:28],
                    c.get("start_at"), c.get("sections")))
        return 0

    if args.cmd == "identify":
        p_start, p_end = _at(args.date, args.frm), _at(args.date, args.to)
        meetings = fetch_meetings(target_date=args.date, max_pages=args.pages)
        if not meetings:
            sys.stderr.write("Could not fetch the Equipe meetings list.\n")
            return 2
        cands = [m for m in meetings if match_meeting(
            m, date=args.date, country=args.country, name=args.name, discipline=args.discipline)]
        hints = folder_hints(getattr(args, "hint", None))
        ranked = []
        for m in cands:
            sched = safe_fetch("%s/meetings/%s/schedule" % (BASE, m.get("id")))
            if not sched:
                continue
            day_classes = classes_on_date(sched, args.date)
            matched = identify_classes(day_classes, p_start, p_end)
            if not matched:
                continue
            top = matched[0]
            # Folder-name hint: does the dump's name match this meeting (name/venue/arenas)?
            hay = " ".join([str(m.get("display_name") or ""), str(m.get("venue_country") or "")]
                           + [str(c.get("arena") or "") for c in day_classes])
            ranked.append({
                "overlap_min": top["overlap_min"],
                "start_delta_min": top["start_delta_min"],
                "name_score": hint_score(hay, hints),
                "meeting": {"id": m.get("id"), "display_name": core.clean_text(m.get("display_name")),
                            "venue_country": m.get("venue_country"), "tdb_id": m.get("tdb_id"),
                            "start_on": m.get("start_on"), "end_on": m.get("end_on")},
                "klass": {"class_no": top.get("class_no"), "name": top.get("name"),
                          "fence_height": top.get("fence_height"), "arena": top.get("arena"),
                          "start_at": top.get("start_at"), "status": top.get("status"),
                          "section_id": (top.get("sections") or [None])[0], "sections": top.get("sections")},
                "alternatives": [{"class_no": c.get("class_no"), "name": c.get("name"),
                                  "start_at": c.get("start_at"), "overlap_min": c.get("overlap_min"),
                                  "section_id": (c.get("sections") or [None])[0]} for c in matched[1:3]],
            })
        # A folder-name match (name_score) decides WHICH meeting when several share the date; within
        # that, closeness of the class start to when shooting began, then overlap.
        ranked.sort(key=lambda r: (-r["name_score"], r["start_delta_min"], -r["overlap_min"]))
        result = {"date": args.date, "window": [args.frm, args.to], "count": len(ranked),
                  "best": ranked[0] if ranked else None, "candidates": ranked}
        if args.json:
            # ASCII-safe (\uXXXX) so the PowerShell capture survives the console codepage; the human
            # path below keeps the utf-8 reconfigure for direct, readable terminal output.
            print(json.dumps(result, ensure_ascii=True))
            return 0
        sys.stdout.reconfigure(encoding="utf-8")
        if not ranked:
            print("No matching meeting/class found for %s %s-%s." % (args.date, args.frm, args.to))
            return 0
        b = ranked[0]
        print("Best match for %s photos shot %s-%s:" % (args.date, args.frm, args.to))
        print("  Meeting : %s  (id %s, %s, tdb=%s)" % (b["meeting"]["display_name"], b["meeting"]["id"],
              b["meeting"]["venue_country"], b["meeting"]["tdb_id"]))
        print("  Class   : %s  %s  %s  (start %s, overlap %.0f min)" % (
              b["klass"]["class_no"], b["klass"].get("fence_height") or "", b["klass"]["name"],
              b["klass"]["start_at"], b["overlap_min"]))
        print("  Arena   : %s" % (b["klass"].get("arena") or "?"))
        print("  Section : %s" % b["klass"]["section_id"])
        for alt in (b["alternatives"] + [{"class_no": r["klass"]["class_no"], "name": r["klass"]["name"],
                    "start_at": r["klass"]["start_at"], "overlap_min": r["overlap_min"],
                    "section_id": r["klass"]["section_id"]} for r in ranked[1:3]]):
            print("    alt: cls %s %s (start %s, overlap %.0f min, sect %s)" % (
                  alt["class_no"], str(alt["name"])[:30], alt.get("start_at"), alt.get("overlap_min", 0),
                  alt.get("section_id")))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
