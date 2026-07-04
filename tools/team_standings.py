#!/usr/bin/env python3
"""team_standings.py - team-competition standings for NBECH (and any Equipe team class).

Equestrian team classes (dressage/jumping) score by NATION: each team fields several riders and the
team total is the sum of the best N of them (the weakest are dropped). Equipe models this natively - a
team class has a "[Team]" class_section whose `starts[]` are the TEAMS (logo_id=nation, club_name=team
name, team_no, position=rank, nested `starts[]`=the member riders, plus team-level `results`). Once the
class is ridden Equipe fills in the totals + positions; until then they're null.

This module turns that into a clean standings list + a board feed for the graphics, and - because
Equipe's team total can lag the individual scores - it can ALSO compute the team total itself from the
member percentages with a configurable drop rule (best `count` of M). So the standings are live the
moment the riders' scores land, not only when Equipe publishes the team aggregate.

    python team_standings.py find      --meeting 80465                  # list the team sections
    python team_standings.py standings --section 1260699 [--count 3]    # live fetch + print
    python team_standings.py board     --section 1260699 [--count 3] [--out board.json]
    python team_standings.py selftest

Pure parse/compute/sort/board logic is covered by selftest; `find`/`standings`/`board` need the network.
Stdlib + equipe_api only.
"""
import argparse
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _num(v):
    """First sane float out of an Equipe result value (handles None / '' / '72,50' / '72.50')."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def member_total(member):
    """The rider's final dressage PERCENT from their results[]. Equipe gives one row per judge with a
    'percent' (e.g. 67.714); the rider's final score is the mean of the judge percents. An explicit
    *Total* row (with a percent/value) is preferred if present. Returns None until the rider has ridden.
    NOTE: do NOT use the 'total' field - that's the raw points sum (e.g. 237), not the %."""
    results = (member.get("results") if isinstance(member, dict) else None) or []
    total_pct, judge_pcts = None, []
    for r in results:
        t = str(r.get("type", "")).lower()
        if "total" in t:
            n = _num(r.get("percent"))
            if n is None:
                n = _num(r.get("value")) or _num(r.get("result"))
            if n is not None:
                total_pct = n
        else:
            n = _num(r.get("percent"))
            if n is not None:
                judge_pcts.append(n)
    if total_pct is not None:
        return round(total_pct, 3)
    if judge_pcts:
        return round(sum(judge_pcts) / len(judge_pcts), 3)
    return None


def _member_name(m):
    n = (m.get("rider_name") or "").strip()
    if n:
        return n
    fn = (m.get("rider_first_name") or "").strip()
    ln = (m.get("rider_last_name") or "").strip()
    return (fn + " " + ln).strip()


def compute_team_total(member_scores, count=3):
    """Best `count` of the member percentages, summed (the rest dropped). Partial teams (fewer scores
    than `count`) sum what they have. Returns (total, n_counted)."""
    scores = sorted([s for s in member_scores if s is not None], reverse=True)
    counted = scores[:count] if count and count > 0 else scores
    return (round(sum(counted), 3), len(counted))


def parse_team_section(section, count=3):
    """A '[Team]' class_section -> list of team dicts:
        {nation, team_name, team_no, members:[{rider,horse,score}], total, counted, equipe_position}
    `total` is computed from member scores with the drop rule; `equipe_position` is Equipe's own rank
    (used as a fallback ordering before any score lands)."""
    teams = []
    for t in (section.get("starts") if isinstance(section, dict) else section) or []:
        if str(t.get("type", "")).lower() and str(t.get("type", "")).lower() != "team":
            # a [Team] section's entries are type 'Team'; skip anything else defensively
            pass
        members = []
        for m in (t.get("starts") or []):
            members.append({"rider": _member_name(m), "horse": (m.get("horse_name") or "").strip(),
                            "score": member_total(m)})
        comp_total, counted = compute_team_total([m["score"] for m in members], count)
        # Equipe's OWN official team total + rank (the team entry's *Total* result). Authoritative — matches
        # the arena board exactly and applies the correct drop rule itself; fall back to our computed best-N
        # only until Equipe publishes it.
        off_total, off_rank = None, None
        for r in (t.get("results") or []):
            if "total" in str(r.get("type", "")).lower():
                n = _num(r.get("percent"))
                if n is None:
                    n = _num(r.get("value")) or _num(r.get("result"))
                if n is not None:
                    off_total = round(n, 3)
                if r.get("rank") is not None:
                    off_rank = r.get("rank")
        total = off_total if off_total is not None else (comp_total if counted else None)
        teams.append({
            "nation": (t.get("logo_id") or "").strip().upper(),
            "team_name": (t.get("club_name") or t.get("logo_id") or "").strip(),
            "team_no": t.get("team_no"),
            "members": members,
            "total": total,
            "counted": counted,
            "official": off_total is not None,   # True = Equipe's published total, not our computation
            "equipe_rank": off_rank,
            "equipe_position": t.get("position"),
        })
    return teams


def standings(teams):
    """Rank the teams: by Equipe's official rank when published, else by total (desc) once any score is in,
    else by Equipe's seeded position. Returns the same dicts with a 'rank' added, in ranked order."""
    have_off = any(t.get("equipe_rank") is not None for t in teams)
    have_scores = any(t.get("total") is not None for t in teams)
    if have_off:
        ordered = sorted(teams, key=lambda t: (t.get("equipe_rank") is None, t.get("equipe_rank") or 1e9))
    elif have_scores:
        ordered = sorted(teams, key=lambda t: (t.get("total") is None, -(t.get("total") or 0)))
    else:
        ordered = sorted(teams, key=lambda t: (t.get("equipe_position") in (None, 0),
                                               t.get("equipe_position") or 1e9))
    out = []
    for i, t in enumerate(ordered, 1):
        d = dict(t)
        if t.get("equipe_rank") is not None:
            d["rank"] = t.get("equipe_rank")          # authoritative placing
        else:
            d["rank"] = i if (t.get("total") is not None or not have_scores) else None
        out.append(d)
    return out


def to_scoreboard(teams_ranked, title="", kicker="TEAM STANDINGS"):
    """Map team standings onto the vfxpack `scoreboard.html` data shape (rows rider/horse/nation/result),
    so the proven brand board renders the team table with no new template. The team NAME goes in the
    rider slot, the counted riders in the horse slot, the team total in the result chip."""
    rows = []
    for t in teams_ranked:
        res = ("%.2f" % t["total"]) if t.get("total") is not None else ""
        counters = [m["rider"] for m in t.get("members", []) if m.get("score") is not None]
        sub = (", ".join(counters[:3]) if counters else "best 3 count")
        rows.append({"rank": t.get("rank"), "rider": t.get("team_name") or t.get("nation"),
                     "horse": sub, "nation": t.get("nation"), "result": res})
    return {"mode": "result", "box": "light", "kicker": kicker,
            "title": title or "Team Standings", "rows": rows}


def to_board(teams_ranked, title="", count=3):
    """Board feed for the team-standings graphic (mirrors equipe_board's shape: rows + meta)."""
    rows = []
    for t in teams_ranked:
        rows.append({"rank": t.get("rank"), "nation": t.get("nation"),
                     "team": t.get("team_name"), "total": t.get("total"),
                     "riders": [{"rider": m["rider"], "horse": m["horse"], "score": m["score"]}
                                for m in t.get("members", [])]})
    return {"schema": "equisport.team_board/1", "title": title, "drop_rule": "best %d" % count,
            "rows": rows}


# --------------------------------------------------------------------------- Equipe bridge
def _find_team_sections(meeting_id, fetch_section, schedule):
    """Section ids in a meeting whose entries are TEAMS (type 'Team'). Returns [(class_name, section_id)]."""
    out = []
    for mc in (schedule.get("meeting_classes") or []):
        for s in (mc.get("class_sections") or []):
            sec = fetch_section(s["id"])
            starts = (sec.get("starts") if isinstance(sec, dict) else None) or []
            if starts and any(str(x.get("type", "")).lower() == "team" for x in starts):
                out.append(((mc.get("name") or "").strip(), s["id"]))
    return out


def run_selftest():
    ok = [True]
    def check(c, m):
        ok[0] = ok[0] and bool(c); print(("ok  " if c else "FAIL") + "  " + m)

    check(_num("72,50") == 72.5 and _num("68.1") == 68.1 and _num(None) is None and _num("") is None,
          "num: parses comma/dot, None/'' -> None")
    m_done = {"rider_name": "A", "horse_name": "H", "results": [
        {"type": "Dressage", "judge_by": "H", "value": "70.0"},
        {"type": "DressageTotal", "value": "72,134"}]}
    check(member_total(m_done) == 72.134, "member_total: reads the *Total* row (comma decimal)")
    check(member_total({"results": []}) is None, "member_total: no results -> None (not yet ridden)")
    # drop rule: best 3 of 4
    tot, n = compute_team_total([70.0, 68.0, 72.0, 60.0], count=3)
    check(tot == 210.0 and n == 3, "drop rule: best 3 of 4 sums 72+70+68, drops 60")
    check(compute_team_total([71.0], count=3) == (71.0, 1), "drop rule: partial team sums what it has")

    # parse a synthetic [Team] section (2 teams, one ridden, one not) + rank
    section = {"starts": [
        {"type": "Team", "logo_id": "swe", "club_name": "Sweden", "team_no": 1, "position": 3, "starts": [
            {"rider_name": "S1", "horse_name": "h1", "results": [{"type": "DressageTotal", "value": "70.0"}]},
            {"rider_name": "S2", "horse_name": "h2", "results": [{"type": "DressageTotal", "value": "72.0"}]},
            {"rider_name": "S3", "horse_name": "h3", "results": [{"type": "DressageTotal", "value": "68.0"}]},
            {"rider_name": "S4", "horse_name": "h4", "results": [{"type": "DressageTotal", "value": "60.0"}]}]},
        {"type": "Team", "logo_id": "den", "club_name": "Denmark", "team_no": 2, "position": 1, "starts": [
            {"rider_name": "D1", "horse_name": "h5", "results": [{"type": "DressageTotal", "value": "71.0"}]},
            {"rider_name": "D2", "horse_name": "h6", "results": [{"type": "DressageTotal", "value": "69.0"}]},
            {"rider_name": "D3", "horse_name": "h7", "results": [{"type": "DressageTotal", "value": "67.0"}]}]}]}
    teams = parse_team_section(section, count=3)
    swe = next(t for t in teams if t["nation"] == "SWE")
    check(swe["total"] == 210.0 and swe["counted"] == 3, "parse: SWE total = best 3 of 4 (210.0)")
    check(swe["team_name"] == "Sweden" and len(swe["members"]) == 4, "parse: team name + members")
    ranked = standings(teams)
    check(ranked[0]["nation"] == "SWE" and ranked[0]["rank"] == 1, "standings: SWE (210) ranks above DEN (207)")
    check(ranked[1]["nation"] == "DEN" and ranked[1]["total"] == 207.0, "standings: DEN total 71+69+67=207")
    # Equipe's OWN team total + rank (DressageTotal on the team entry) is authoritative when present
    off_section = {"starts": [
        {"type": "Team", "logo_id": "FIN", "club_name": "Finland", "position": 1,
         "results": [{"type": "DressageTotal", "percent": "130.476", "rank": 1}],
         "starts": [{"rider_name": "F1", "results": [{"type": "DressageTotal", "percent": "66.0"}]}]},
        {"type": "Team", "logo_id": "DEN", "club_name": "Denmark", "position": 3,
         "results": [{"type": "DressageTotal", "percent": "129.666", "rank": 2}],
         "starts": [{"rider_name": "D1", "results": [{"type": "DressageTotal", "percent": "64.8"}]}]}]}
    ot = parse_team_section(off_section)
    fin = next(t for t in ot if t["nation"] == "FIN")
    check(fin["total"] == 130.476 and fin["official"] is True and fin["equipe_rank"] == 1,
          "parse: uses Equipe's official team total + rank when published (not the computed best-N)")
    roff = standings(ot)
    check(roff[0]["nation"] == "FIN" and roff[0]["rank"] == 1 and roff[1]["nation"] == "DEN" and roff[1]["rank"] == 2,
          "standings: orders + labels by Equipe's official rank (FIN 1, DEN 2)")

    # not-yet-ridden -> fall back to Equipe's seeded position, no scores
    pre = {"starts": [
        {"type": "Team", "logo_id": "FIN", "club_name": "Finland", "position": 2, "starts": [
            {"rider_name": "F1", "results": []}]},
        {"type": "Team", "logo_id": "NOR", "club_name": "Norway", "position": 1, "starts": [
            {"rider_name": "N1", "results": []}]}]}
    rpre = standings(parse_team_section(pre))
    check(rpre[0]["nation"] == "NOR" and rpre[0]["total"] is None,
          "standings: before any score, order by Equipe position (NOR pos1 first), totals None")

    board = to_board(ranked, title="Junior Team", count=3)
    check(board["rows"][0]["nation"] == "SWE" and board["rows"][0]["total"] == 210.0
          and board["drop_rule"] == "best 3", "board: ranked rows + drop-rule label")
    sb = to_scoreboard(ranked, title="Junior Team")
    check(sb["mode"] == "result" and sb["rows"][0]["rider"] == "Sweden" and sb["rows"][0]["result"] == "210.00"
          and sb["rows"][0]["nation"] == "SWE", "scoreboard map: team name in rider slot, total in result chip")
    print("\nteam_standings SELFTEST", "PASS" if ok[0] else "FAIL")
    return 0 if ok[0] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("find", help="list a meeting's team sections")
    pf.add_argument("--meeting", required=True)
    ps = sub.add_parser("standings", help="fetch a [Team] section and print standings")
    ps.add_argument("--section", required=True)
    ps.add_argument("--count", type=int, default=3, help="how many member scores count (best N)")
    pb = sub.add_parser("board", help="fetch a [Team] section -> board JSON for the graphic")
    pb.add_argument("--section", required=True)
    pb.add_argument("--count", type=int, default=3)
    pb.add_argument("--title", default="")
    pb.add_argument("--scoreboard", action="store_true",
                    help="emit the vfxpack scoreboard.html shape (render via render_vfx --component scoreboard)")
    pb.add_argument("--out")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return run_selftest()

    import equipe_api  # network only here
    if args.cmd == "find":
        sch = equipe_api.safe_fetch("%s/meetings/%s/schedule" % (equipe_api.BASE, args.meeting))
        if not sch:
            sys.stderr.write("could not fetch schedule\n"); return 2
        found = _find_team_sections(args.meeting, lambda sid: equipe_api.safe_fetch(
            "%s/class_sections/%s" % (equipe_api.BASE, sid)) or {}, sch)
        for name, sid in found:
            print("  %-46s section=%s" % (name[:46], sid))
        print("%d team section(s)" % len(found))
        return 0

    sec = equipe_api.safe_fetch("%s/class_sections/%s" % (equipe_api.BASE, args.section))
    if not sec:
        sys.stderr.write("could not fetch section %s\n" % args.section); return 2
    ranked = standings(parse_team_section(sec, args.count))
    if args.cmd == "standings":
        print("Team standings (best %d count):" % args.count)
        for t in ranked:
            tot = ("%.2f" % t["total"]) if t.get("total") is not None else "-- (not ridden)"
            print("  %s %-10s %-12s %s" % (
                (str(t["rank"]) + ".").ljust(3) if t.get("rank") else "  -",
                t.get("nation"), t.get("team_name"), tot))
            for m in t.get("members", []):
                ms = ("%.2f" % m["score"]) if m.get("score") is not None else "--"
                print("        %-26s %-20s %s" % (m["rider"][:26], (m["horse"] or "")[:20], ms))
        return 0
    # board
    board = (to_scoreboard(ranked, title=args.title) if args.scoreboard
             else to_board(ranked, title=args.title, count=args.count))
    out = args.out or ("team_scoreboard.json" if args.scoreboard else "team_board.json")
    with io.open(out, "w", encoding="utf-8") as fh:
        json.dump(board, fh, ensure_ascii=False, indent=2)
    print("wrote %d team row(s) -> %s%s" % (len(board["rows"]), out,
          "  (render: render_vfx.py --component scoreboard --data %s)" % out if args.scoreboard else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
