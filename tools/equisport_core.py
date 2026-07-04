#!/usr/bin/env python3
"""equisport_core.py — Python ports of the pure, well-tested helper logic that currently
lives in Equisport-Workflow.ps1. stdlib only.

Purpose (see memory: project_ps_to_python_port): move risk-prone / data-heavy logic out of
Windows PowerShell 5.1 (which has JSON/encoding landmines) into Python, where it is faster
to run in batch and easier to test. This module is ADDITIVE — the PowerShell functions still
exist; ports are validated to produce identical output (selftest asserts the same expected
values the Test-Smoke.ps1 suite asserts) before any caller is migrated.

Run:  python equisport_core.py selftest
      python equisport_core.py parity --file <utf8 json>   # [{"func","args"}...] -> [results]
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime

# ── text helpers (Cluster A) ─────────────────────────────────────────────────

def csv_field(v):
    """Mirror of CsvField: collapse runs of spaces, trim, quote if it contains , " CR or LF."""
    v = "" if v is None else str(v)
    while "  " in v:
        v = v.replace("  ", " ")
    v = v.strip()
    if re.search(r'[,"\r\n]', v):
        return '"' + v.replace('"', '""') + '"'
    return v


def clean_text(s):
    """Mirror of CleanText: drop astral chars (emoji), a band of symbol/zero-width ranges,
    collapse double spaces, trim."""
    s = "" if s is None else str(s)
    # PS removes UTF-16 surrogate pairs = astral code points (> U+FFFF)
    s = "".join(c for c in s if ord(c) <= 0xFFFF)
    s = re.sub("[ -⟿⬀-⯿︀-️]", "", s)
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


_TRANSLIT = {
    "å": "a", "ä": "a", "ö": "o", "é": "e", "è": "e", "ê": "e",
    "ë": "e", "à": "a", "â": "a", "á": "a", "ã": "a", "ü": "u",
    "ú": "u", "û": "u", "ù": "u", "ï": "i", "î": "i", "í": "i",
    "ì": "i", "ô": "o", "ó": "o", "ò": "o", "õ": "o", "ø": "o",
    "ñ": "n", "ç": "c", "æ": "ae",
    "Å": "A", "Ä": "A", "Ö": "O", "É": "E", "È": "E", "Ê": "E",
    "Ë": "E", "À": "A", "Â": "A", "Á": "A", "Ã": "A", "Ü": "U",
    "Ú": "U", "Û": "U", "Ù": "U", "Ï": "I", "Î": "I", "Í": "I",
    "Ì": "I", "Ô": "O", "Ó": "O", "Ò": "O", "Õ": "O", "Ø": "O",
    "Ñ": "N", "Ç": "C", "Æ": "AE", "ß": "ss",
}


def transliterate(s):
    """Mirror of Transliterate: map accented Latin (Swedish a/a/o etc.) to ASCII; ss for sharp-s.
    Then strip any leftover combining marks, so a DECOMPOSED (NFD) name like 'a'+U+030A (a) or
    'o'+U+0308 (o) — which the precomposed map alone would leave a dangling mark on — also folds
    to plain ASCII instead of turning the mark into a '_' downstream."""
    s = "" if s is None else str(s)
    s = "".join(_TRANSLIT.get(c, c) for c in s)
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return s


def _titlecase_word(w):
    return (w[:1].upper() + w[1:].lower()) if w else w


def to_title_name(s):
    """Mirror of ToTitleName: transliterate, non [A-Za-z0-9-] -> _, collapse/trim _, then
    Title-Case each underscore segment (sub-splitting on hyphens)."""
    s = transliterate("" if s is None else str(s))
    s = re.sub(r"[^A-Za-z0-9\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    segs = []
    for seg in s.split("_"):
        segs.append("-".join(_titlecase_word(p) for p in seg.split("-")))
    return "_".join(segs)


def clean_class_name(s):
    """Mirror of CleanClassName: transliterate; 1.35m -> 135M; drop punctuation; spaces/hyphens
    -> _; keep [A-Za-z0-9_]; collapse/trim _; Title-Case each word."""
    s = transliterate("" if s is None else str(s))
    s = re.sub(r"(\d+)\.(\d+)[mM]", lambda m: m.group(1) + m.group(2) + "M", s)
    s = re.sub(r"[:\.]+", "", s)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^A-Za-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return "_".join(_titlecase_word(w) for w in s.split("_"))


def limit_words(s, max_len):
    """Mirror of LimitWords: keep whole underscore-words until adding the next would exceed max_len."""
    s = "" if s is None else str(s)
    max_len = int(max_len)
    if len(s) <= max_len:
        return s
    result = ""
    for w in s.split("_"):
        candidate = (result + "_" + w) if result else w
        if len(candidate) <= max_len:
            result = candidate
        else:
            break
    return result


def xmp_escape(s):
    """Mirror of XmpEscape: &, <, >, " to entities (ampersand first)."""
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ── naming / parsing helpers (Cluster B) ─────────────────────────────────────
# NB: PowerShell's -match / -replace / -contains are CASE-INSENSITIVE by default, so the
# ports below use re.IGNORECASE / lowercased comparisons to stay byte-identical (verified
# by the cross-language parity test).

PHOTO_EXTS = {".cr3", ".jpg", ".jpeg"}
VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".crm"}


def get_file_media_class(ext):
    """Mirror Get-FileMediaClass: 'photo' | 'video' | None (case-insensitive extension)."""
    e = ("" if ext is None else str(ext)).lower()
    if e in PHOTO_EXTS:
        return "photo"
    if e in VIDEO_EXTS:
        return "video"
    return None


_CAMORIG_PATTERNS = [
    r"^[A-Z]{3,4}\d{3,5}$", r"^DSC_?\d{3,5}$", r"^IMG_?\d{3,5}$",
    r"^MVI_?\d{3,5}$", r"^_MG_\d{3,5}$", r"^C\d{4}$",
    r"^[A-Z]\d{3}[CL]\d{3}", r"^DJI_?\d{3,5}$",
    r"^GX\d{6}$", r"^GOPR\d{3,5}$", r"^GH\d{6}$",
]


def test_is_camera_original_name(name):
    """Mirror Test-IsCameraOriginalName: True if the file stem looks like a raw camera name."""
    stem = os.path.splitext(os.path.basename("" if name is None else str(name)))[0]
    return any(re.search(p, stem, re.IGNORECASE) for p in _CAMORIG_PATTERNS)


_RIDER_FORM = re.compile(
    r"^\d{1,3}_[0-9A-Za-z\-]{1,10}_\d{1,3}_[A-Za-z][A-Za-z0-9_\-]*_(?:V|INT)?\d{2,4}"
    r"\.(?:mp4|mov|mxf|cr3|jpg|jpeg)$", re.IGNORECASE)
_TYPE_FORM = re.compile(
    r"^\d{6}_[A-Za-z0-9\-]+_(?:ACTION|INTERVIEW|BROLL|SCENERY|WARMUP|CEREMONY|DRESSAGE|"
    r"STABLE|COURSEWALK)_\d{2,4}\.(?:mp4|mov|mxf)$", re.IGNORECASE)


def test_canonical_filename(name):
    """Mirror Test-CanonicalFilename: True if the name follows the rider or type convention."""
    n = "" if name is None else str(name)
    return bool(_RIDER_FORM.search(n) or _TYPE_FORM.search(n))


def get_fence_file_parts(raw):
    """Mirror Get-FenceFileParts. Returns {'Tag','Qual'}.
    '1.35'->135M/''  ;  '0.90'->090M/''  ;  '100-120 (Msv B)'->100-120M/'Msv-B'."""
    if not raw:
        return {"Tag": "", "Qual": ""}
    s = re.sub(r"m$", "", str(raw), flags=re.IGNORECASE).strip()
    m = re.match(r"^(\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?)(.*)$", s)
    if m:
        num = m.group(1).replace(".", "")
        rest = re.sub(r"[()]", "", m.group(2)).strip()
        rest = re.sub(r"\s+", "-", rest)
        rest = re.sub(r"-+", "-", rest).strip("-")
        return {"Tag": num + "M", "Qual": rest}
    clean = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9\-]", "", transliterate(s)))
    return {"Tag": clean + "M", "Qual": ""}


def get_canon_folder_name(rider_name, start_no):
    """Mirror Get-CanonFolderName: '<100+start><FFFLL>' from transliterated name initials."""
    parts = re.split(r"\s+", ("" if rider_name is None else str(rider_name)).strip(), maxsplit=1)
    fn = parts[0] if len(parts) >= 1 else ""
    ln = parts[1] if len(parts) >= 2 else ""
    first = re.sub(r"[^A-Z]", "", transliterate(fn).upper())
    last = re.sub(r"[^A-Z]", "", transliterate(ln).upper())
    letters = (first + "ZZZ")[:3] + (last + "ZZ")[:2]
    return str(100 + int(start_no)) + letters


def get_class_folder_name(fence_height, class_no, name):
    """Mirror Get-ClassFolderName: '<classNo>_<fenceTag>_<cleanClassName>' with the fence
    token de-duplicated out of the class name and the name length-limited to 30."""
    fence_raw = str(fence_height) if fence_height else ""
    class_no_s = str(class_no) if class_no else "0"
    fence_tag = get_fence_file_parts(fence_raw)["Tag"]
    class_tag = clean_class_name(clean_text(name))
    class_tag = re.sub(r"(?i)^(Klass|Class)_\d+_?", "", class_tag)
    class_tag = re.sub(r"^_+", "", class_tag)
    if fence_tag:
        stripped = re.sub(r"^0+", "", fence_tag) or fence_tag
        seen = []
        for token in (fence_tag, stripped):
            if token not in seen:
                seen.append(token)
        for token in seen:
            esc = re.escape(token)
            class_tag = re.sub(r"(?i)(^|_)" + esc + r"(_|$)", r"\1", class_tag)
            class_tag = re.sub(r"_+", "_", class_tag)
        class_tag = class_tag.strip("_")
    class_tag = limit_words(class_tag, 30)
    if fence_tag:
        return "%s_%s_%s" % (class_no_s, fence_tag, class_tag)
    return "%s_%s" % (class_no_s, class_tag)


def new_video_canonical_name(class_no=None, fence_tag=None, start_no=None, rider=None,
                             clip_type=None, seq=0, ext="", event_name=None, today=None):
    """Mirror New-VideoCanonicalName. Rider form is deterministic; type/b-roll form uses
    today's date (yyMMdd) unless `today` is supplied (for testing)."""
    ext = ext if str(ext).startswith(".") else "." + str(ext)
    seq3 = "%03d" % int(seq)
    if rider:
        rn = to_title_name(rider)
        kind = "INT" if clip_type == "interview" else "V"
        cn = class_no if class_no else "0"
        ft = fence_tag if fence_tag else "NA"
        st = start_no if start_no else "0"
        return "%s_%s_%s_%s_%s%s%s" % (cn, ft, st, rn, kind, seq3, ext)
    ev = re.sub(r"[^A-Za-z0-9]", "", transliterate(event_name)) if event_name else "Event"
    if len(ev) > 12:
        ev = ev[:12]
    typ = clip_type.upper() if clip_type else "BROLL"
    date = today or datetime.now().strftime("%y%m%d")
    return "%s_%s_%s_%s%s" % (date, ev, typ, seq3, ext)


# ── classification / assignment helpers (Cluster C) ──────────────────────────

def convert_to_seconds(s):
    """Mirror ConvertTo-Seconds: 'H:MM:SS', 'N s'/'N sec', or a leading number -> float seconds."""
    s = ("" if s is None else str(s)).strip()
    if not s:
        return 0.0
    m = re.match(r"^(\d+):(\d{2}):(\d{2})(?:\.\d+)?$", s)
    if m:
        return float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
    m = re.match(r"^([\d.]+)\s*s(ec)?$", s)
    if m:
        return float(m.group(1))
    cleaned = re.sub(r"[^\d.].*$", "", s)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def get_video_kind(audio_streams, duration_sec, interview_min=25):
    """Mirror Get-VideoKind: 0 audio -> slowmo, <0 -> unknown, >=interview_min -> interview, else action."""
    a = int(audio_streams)
    if a == 0:
        return "slowmo"
    if a < 0:
        return "unknown"
    return "interview" if float(duration_sec) >= float(interview_min) else "action"


def get_clip_type_from_transcript(transcript):
    """Mirror Get-ClipTypeFromTranscript. NB: the live PS regex contains Swedish-only keywords
    (traning/stallgang/hovvard) that are mojibake in the cp1252-read .ps1 and so never match in
    PS; this port keeps the correct UTF-8 forms (a latent fix). Parity is tested on ASCII inputs."""
    if not transcript:
        return None
    t = transliterate(str(transcript)).lower()
    rules = [
        (r"b[\s\-]?roll|cutaway", "broll"),
        (r"interview|intervju", "interview"),
        (r"scenery|landscape|nature|natur|landskap|establishing", "scenery"),
        (r"warm[\s\-]?up|uppv|inridn|uppridn|schooling|traning", "warmup"),
        (r"ceremony|prisutdeln|prisceremoni|award|podium|pokal|rosett|prize", "ceremony"),
        (r"dressyr|dressage", "dressage"),
        (r"stall|paddock|stable|grooming|borstning|hovvard|stallgang", "stable"),
        (r"banpromenad|course[\s\-]?walk", "coursewalk"),
    ]
    for pat, kind in rules:
        if re.search(pat, t):
            return kind
    return None


def get_start_no_from_scene(scene_name, known_start_nos):
    """Mirror Get-StartNoFromScene: strip a letter/underscore prefix + leading zeros, parse int,
    return it (as string) only if it's in known_start_nos; else None."""
    if not scene_name:
        return None
    s = re.sub(r"^[A-Za-z_]+", "", str(scene_name).strip())
    stripped = s.lstrip("0") or "0"
    if not re.fullmatch(r"-?\d+", stripped):
        return None
    key = str(int(stripped))
    return key if key in [str(k) for k in known_start_nos] else None


def _strip_breed(horse):
    return re.sub(r"\s*\([^)]+\)\s*$", "", "" if horse is None else str(horse)).strip()


def new_whisper_prompt(riders):
    """Mirror New-WhisperPrompt: 'Startlista: <names>' from rider + breed-stripped horse names,
    case-insensitively de-duplicated preserving first occurrence (PS Select-Object -Unique)."""
    names = []
    for r in riders:
        rider = (r.get("Rider") or "").strip()
        if rider:
            names.append(rider)
        horse = _strip_breed(r.get("Horse"))
        if len(horse) >= 3:
            names.append(horse)
    seen, unique = set(), []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            unique.append(n)
    if not unique:
        return ""
    return "Startlista: " + ", ".join(unique)


def _stno(r):
    return str(int(str(r.get("St.No"))))


def get_start_no_from_transcript(transcript, riders):
    """Mirror Get-StartNoFromTranscript: returns (startNo, 'rider'|'horse') or (None, None).
    A name matches if every >=3-char part of the transliterated name appears in the transcript."""
    if not transcript:
        return (None, None)
    text = transliterate(transcript).lower()
    for r in riders:
        parts = [p for p in re.split(r"\s+", transliterate(r.get("Rider") or "").lower()) if len(p) >= 3]
        if parts and all(re.search(re.escape(p), text) for p in parts):
            return (_stno(r), "rider")
    for r in riders:
        horse = _strip_breed(r.get("Horse"))
        if len(horse) < 3:
            continue
        parts = [p for p in re.split(r"\s+", transliterate(horse).lower()) if len(p) >= 3]
        if parts and all(re.search(re.escape(p), text) for p in parts):
            return (_stno(r), "horse")
    return (None, None)


def get_start_no_from_file_name(file_stem, riders):
    """Mirror Get-StartNoFromFileName: strip the FX6 _C###_YYMMDD / _C### suffix, transliterate,
    and match the remaining name (underscores -> spaces) against the startlist."""
    file_stem = str(file_stem)
    name_part = re.sub(r"_C\d+_\d{6}$", "", file_stem)
    name_part = re.sub(r"_C\d+$", "", name_part)
    if not name_part or len(name_part) < 3 or name_part == file_stem:
        return None
    needle = transliterate(name_part).lower().replace("_", " ")
    for r in riders:
        if transliterate(r.get("Rider") or "").lower() == needle:
            return _stno(r)
    return None


# ── datetime-window helpers (Cluster C2) ─────────────────────────────────────
# Times are handled here as EPOCH SECONDS. The PS originals take [datetime]; only relative
# arithmetic and comparisons are used, so epoch math is identical (and TZ-safe for parity).

def get_sections_for_time(sections, time_epoch, margin_seconds=180):
    """Mirror Get-SectionsForTime: Ids of sections whose [Start-margin, End+margin] window
    contains time. Sections are dicts {Id, Start, End} (epoch seconds); missing Start/End skip."""
    t = float(time_epoch)
    m = float(margin_seconds)
    hits = []
    for s in sections:
        start = s.get("Start")
        end = s.get("End")
        if start is None or end is None:
            continue
        if float(start) - m <= t <= float(end) + m:
            hits.append(s.get("Id"))
    return hits


def get_rider_for_time(rider_times, time_epoch):
    """Mirror Get-RiderForTime: first StartNo whose ResultAt (epoch) is strictly after time;
    else the last rider's StartNo; else None. rider_times: [{StartNo, ResultAt}]."""
    t = float(time_epoch)
    for r in rider_times:
        if t < float(r.get("ResultAt")):
            return r.get("StartNo")
    return rider_times[-1].get("StartNo") if rider_times else None


def get_estimated_rider_times(start_at_epoch, sec_per_start, riders):
    """Mirror Get-EstimatedRiderTimes: the k-th starter (by St.No>0 ascending) is estimated to
    finish at start_at + k*sec_per_start. Returns [{StartNo, ResultAt(epoch)}]."""
    start = float(start_at_epoch)
    sec = int(sec_per_start)
    sorted_r = sorted([r for r in riders if int(r.get("St.No")) > 0],
                      key=lambda r: int(r.get("St.No")))
    out = []
    for k, r in enumerate(sorted_r, start=1):
        out.append({"StartNo": str(int(str(r.get("St.No")))), "ResultAt": start + k * sec})
    return out


# ── parity dispatch (for cross-checking against the PowerShell originals) ──────

def _pair_str(t):
    """Format a (startNo, type) tuple as 'sn|type', None -> '' (matches the PS adapter)."""
    a, b = t
    return "%s|%s" % ("" if a is None else a, "" if b is None else b)


FUNCS = {
    "CsvField": lambda a: csv_field(a[0]),
    "CleanText": lambda a: clean_text(a[0]),
    "Transliterate": lambda a: transliterate(a[0]),
    "ToTitleName": lambda a: to_title_name(a[0]),
    "CleanClassName": lambda a: clean_class_name(a[0]),
    "LimitWords": lambda a: limit_words(a[0], a[1]),
    "XmpEscape": lambda a: xmp_escape(a[0]),
    # Cluster B
    "Get-FileMediaClass": lambda a: get_file_media_class(a[0]),
    "Test-IsCameraOriginalName": lambda a: test_is_camera_original_name(a[0]),
    "Test-CanonicalFilename": lambda a: test_canonical_filename(a[0]),
    "New-VideoCanonicalName": lambda a: new_video_canonical_name(*a),
    "Get-FenceFileParts": lambda a: "%s|%s" % (get_fence_file_parts(a[0])["Tag"],
                                               get_fence_file_parts(a[0])["Qual"]),
    "Get-CanonFolderName": lambda a: get_canon_folder_name(a[0], a[1]),
    "Get-ClassFolderName": lambda a: get_class_folder_name(a[0], a[1], a[2]),
    # Cluster C  (riders/known-nos passed flattened after the first positional arg)
    "ConvertTo-Seconds": lambda a: convert_to_seconds(a[0]),
    "Get-VideoKind": lambda a: get_video_kind(a[0], a[1], a[2] if len(a) > 2 else 25),
    "Get-ClipTypeFromTranscript": lambda a: get_clip_type_from_transcript(a[0]),
    "Get-StartNoFromScene": lambda a: get_start_no_from_scene(a[0], a[1:]),
    "New-WhisperPrompt": lambda a: new_whisper_prompt(a),
    "Get-StartNoFromTranscript": lambda a: _pair_str(get_start_no_from_transcript(a[0], a[1:])),
    "Get-StartNoFromFileName": lambda a: get_start_no_from_file_name(a[0], a[1:]),
    # Cluster C2 (epoch-decomposed; results joined to a comparable string)
    "Get-SectionsForTime": lambda a: ",".join(
        str(x) for x in get_sections_for_time(a[2:], a[0], a[1])),
    "Get-RiderForTime": lambda a: get_rider_for_time(a[1:], a[0]),
    "Get-EstimatedRiderTimes": lambda a: ";".join(
        "%s@%d" % (e["StartNo"], int(e["ResultAt"]))
        for e in get_estimated_rider_times(a[0], a[1], a[2:])),
}


def run_parity(calls):
    """calls: [{"func": name, "args": [...]}]. Returns [result, ...] (strings).
    Tolerates PowerShell collapsing a single-element args array into a scalar."""
    def _args(c):
        a = c["args"]
        return a if isinstance(a, list) else [a]
    return [FUNCS[c["func"]](_args(c)) for c in calls]


# ── selftest (expected values mirror Test-Smoke.ps1) ─────────────────────────

def run_selftest():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # CsvField
    check(csv_field("a, b") == '"a, b"', "CsvField quotes field with comma")
    check(csv_field("plain") == "plain", "CsvField plain value unchanged")
    check(csv_field('he said "hi"') == '"he said ""hi"""', "CsvField escapes quotes")
    check(csv_field("a   b") == "a b", "CsvField collapses spaces")
    # CleanText
    check(clean_text("a   b  c") == "a b c", "CleanText collapses whitespace")
    check(clean_text("hello​world") == "helloworld", "CleanText strips zero-width")
    check(clean_text("nice \U0001F600 day") == "nice day", "CleanText drops emoji (astral)")
    # Transliterate
    check(transliterate("åäö") == "aao", "Transliterate Swedish chars")
    check(transliterate("Nicole Holmén") == "Nicole Holmen", "Transliterate accent")
    check(transliterate("Straße") == "Strasse", "Transliterate sharp-s -> ss")
    # ToTitleName
    check(to_title_name("erik vassmar") == "Erik_Vassmar", "ToTitleName capitalises words")
    check(to_title_name("anna-lena Bäck") == "Anna-Lena_Back", "ToTitleName hyphen + accent")
    # CleanClassName
    check(clean_class_name("1.35m bed. A") == "135m_Bed_A", "CleanClassName normalises fence height")
    check(clean_class_name("Two Phases A:0") == "Two_Phases_A0", "CleanClassName drops colon")
    # LimitWords
    check(limit_words("One_Two_Three_Four", 9) == "One_Two", "LimitWords truncates at word boundary")
    check(limit_words("Short", 20) == "Short", "LimitWords short string unchanged")
    # XmpEscape
    check(xmp_escape('a & b < c > d "e"') == "a &amp; b &lt; c &gt; d &quot;e&quot;",
          "XmpEscape all entities")

    # --- Cluster B: naming / parsing -----------------------------------------
    check(get_file_media_class(".CR3") == "photo", "Get-FileMediaClass CR3 -> photo")
    check(get_file_media_class(".mxf") == "video", "Get-FileMediaClass mxf -> video")
    check(get_file_media_class(".CRM") == "video", "Get-FileMediaClass CRM -> video")
    check(get_file_media_class(".txt") is None, "Get-FileMediaClass txt -> None")
    check(test_is_camera_original_name("VASS4513.MP4") is True, "camera original VASS")
    check(test_is_camera_original_name("G007L054_2606040Y.MXF") is True, "camera original FX6 reel")
    check(test_is_camera_original_name("1_090M_5_Ramona_Svensson_V001.MP4") is False, "canonical not camera-orig")
    check(test_canonical_filename("1_090M_5_Ramona_Svensson_101.CR3") is True, "canonical rider photo")
    check(test_canonical_filename("260527_Norrkoping_BROLL_003.MP4") is True, "canonical b-roll")
    check(test_canonical_filename("VASS4513.MP4") is False, "camera original is not canonical")
    check(get_fence_file_parts("1.35") == {"Tag": "135M", "Qual": ""}, "fence 1.35 -> 135M")
    check(get_fence_file_parts("0.90") == {"Tag": "090M", "Qual": ""}, "fence 0.90 -> 090M")
    check(get_fence_file_parts("100-120 (Msv B)") == {"Tag": "100-120M", "Qual": "Msv-B"},
          "fence range + qual")
    check(get_canon_folder_name("Ramona Svensson", 5) == "105RAMSV", "canon folder RAMSV")
    check(get_canon_folder_name("Bo", 1) == "101BOZZZ", "canon folder Z-padded")
    check(new_video_canonical_name("1", "090M", "5", "Ramona Svensson", "action", 1, ".MP4")
          == "1_090M_5_Ramona_Svensson_V001.MP4", "video canonical rider/action")
    check(new_video_canonical_name(None, None, None, "Anna Bäck", "interview", 2, "mp4")
          == "0_NA_0_Anna_Back_INT002.mp4", "video canonical interview defaults")
    check(new_video_canonical_name(clip_type="broll", seq=3, ext=".MP4",
          event_name="Norrkoping Horse Show", today="260527")
          == "260527_NorrkopingHo_BROLL_003.MP4", "video canonical b-roll (fixed date, 12-char event)")
    check(get_class_folder_name("0.90", "1", "0.90m bed. A - CC Holsteiner Breeding")
          .startswith("1_090M_"), "class folder de-dups fence tag")

    # --- Cluster C: classification / assignment ------------------------------
    check(convert_to_seconds("0:00:30") == 30.0, "ConvertTo-Seconds H:MM:SS")
    check(convert_to_seconds("1:02:03") == 3723.0, "ConvertTo-Seconds 1:02:03")
    check(convert_to_seconds("5.00 s") == 5.0, "ConvertTo-Seconds '5.00 s'")
    check(convert_to_seconds("24.28 s") == 24.28, "ConvertTo-Seconds decimal seconds")
    check(convert_to_seconds("") == 0.0, "ConvertTo-Seconds empty -> 0")
    check(convert_to_seconds("abc") == 0.0, "ConvertTo-Seconds non-numeric -> 0")
    check(get_video_kind(0, 9.4, 25) == "slowmo", "Get-VideoKind 0 audio -> slowmo")
    check(get_video_kind(4, 39, 25) == "interview", "Get-VideoKind audio + long -> interview")
    check(get_video_kind(4, 9.4, 25) == "action", "Get-VideoKind audio + short -> action")
    check(get_video_kind(-1, 24, 25) == "unknown", "Get-VideoKind unprobable -> unknown")
    check(get_clip_type_from_transcript("det här är b-roll") == "broll", "ClipType b-roll")
    check(get_clip_type_from_transcript("en intervju med") == "interview", "ClipType intervju")
    check(get_clip_type_from_transcript("dressyr idag") == "dressage", "ClipType dressyr")
    check(get_clip_type_from_transcript("inridning på banan") == "warmup", "ClipType inridning")
    check(get_clip_type_from_transcript("träning") == "warmup", "ClipType Swedish traning -> warmup (transliterate)")
    check(get_clip_type_from_transcript("hoppning") is None, "ClipType none")
    check(get_start_no_from_scene("00005", ["1", "5", "6"]) == "5", "scene 00005 -> 5")
    check(get_start_no_from_scene("S0005", ["1", "5"]) == "5", "scene S0005 -> 5")
    check(get_start_no_from_scene("99", ["1", "5"]) is None, "scene 99 unknown -> None")
    check(get_start_no_from_scene("ABC", ["1", "5"]) is None, "scene ABC -> None")
    riders = [{"Rider": "Erik Vassmar", "Horse": "Bonita (SWB)", "St.No": "5"},
              {"Rider": "Anna Back", "Horse": "Comet", "St.No": "12"}]
    check(new_whisper_prompt(riders) == "Startlista: Erik Vassmar, Bonita, Anna Back, Comet",
          "New-WhisperPrompt names breed-stripped")
    check(new_whisper_prompt([]) == "", "New-WhisperPrompt empty -> ''")
    check(get_start_no_from_transcript("erik vassmar rider idag", riders) == ("5", "rider"),
          "transcript rider match")
    check(get_start_no_from_transcript("vi ser comet hoppa", riders) == ("12", "horse"),
          "transcript horse fallback")
    check(get_start_no_from_transcript("ingen match", riders) == (None, None),
          "transcript no match")
    check(get_start_no_from_file_name("Erik_Vassmar_C001_260527", riders) == "5",
          "filename FX6 suffix stripped -> match")
    check(get_start_no_from_file_name("Random", riders) is None, "filename no suffix -> None")

    # --- Cluster C2: datetime-window helpers (epoch seconds) -----------------
    secs = [{"Id": "A", "Start": 1000, "End": 2000}, {"Id": "B", "Start": 3000, "End": 4000}]
    check(get_sections_for_time(secs, 1500) == ["A"], "sections: inside A")
    check(get_sections_for_time(secs, 2500) == [], "sections: gap -> none")
    check(get_sections_for_time(secs, 2150) == ["A"], "sections: within +margin of A")
    check(get_sections_for_time(secs, 3000) == ["B"], "sections: inside B")
    rts = [{"StartNo": "1", "ResultAt": 1000}, {"StartNo": "2", "ResultAt": 2000},
           {"StartNo": "3", "ResultAt": 3000}]
    check(get_rider_for_time(rts, 500) == "1", "rider time: before first")
    check(get_rider_for_time(rts, 1500) == "2", "rider time: mid window")
    check(get_rider_for_time(rts, 3500) == "3", "rider time: after last")
    check(get_rider_for_time([], 100) is None, "rider time: empty -> None")
    est = get_estimated_rider_times(1000, 100,
        [{"St.No": "1", "Rider": "A"}, {"St.No": "3", "Rider": "C"},
         {"St.No": "2", "Rider": "B"}, {"St.No": "0", "Rider": "X"}])
    check([e["StartNo"] for e in est] == ["1", "2", "3"], "estimated: sorted, St.No=0 excluded")
    check([int(e["ResultAt"]) for e in est] == [1100, 1200, 1300], "estimated: start + k*sec")

    if fails:
        print("SELFTEST: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("equisport_core SELFTEST: PASS (76 checks)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pure Equisport helper logic (PS port)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    pp = sub.add_parser("parity", help="run FUNCS on a JSON call list and emit results")
    pp.add_argument("--file", required=True, help="UTF-8 JSON: [{func,args}...]")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return run_selftest()
    if args.cmd == "parity":
        with open(args.file, "r", encoding="utf-8-sig") as fh:
            calls = json.load(fh)
        # ASCII-escaped so the result survives the PowerShell native-output pipe
        print(json.dumps(run_parity(calls), ensure_ascii=True))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
