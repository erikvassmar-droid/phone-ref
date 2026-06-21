# Field Logger (phone) — Tool 46

A **camera-mounted phone web app** (PWA) that turns what you shoot into ground-truth metadata. Instead of
the pipeline *guessing* which rider a photo belongs to (Equipe time-window estimate, bib OCR), you tap once
and it *knows*: "between these timestamps I shot rider X doing Y."

It is the tactile sibling of the verbal-slate field log ([../field_log.py](../field_log.py), Tool 35) and
emits the **same `field_log.json`** that `Auto-sort -FieldLog` already consumes — so it plugs straight into
the existing sorter with no new consumer.

## Capture modes (all land in one export)

| Mode | What you do | What it gives the edit |
|------|-------------|------------------------|
| **Tap** rider / activity | tap the rider as they go, tap an activity (Round / Warm-up / B-roll …) | a timed **segment** → deterministic rider routing (`start_no` window) |
| **Shot** chip | tap Wide / Tight / Detail / React / Follow | a **shot event** → coverage tracking + gap warnings ("no Tight of #5 yet") |
| **★ Flag** | star a keeper moment | a marker for culling + Resolve markers |
| **🎤 Note** | hold and say "that was the hero shot at fence 5" | a timestamped **voice note**, transcribed on the PC by our Whisper |
| **GPS** (Sync panel) | tap *Tag venue GPS* once | the venue lat/lon → reverse-geocoded on the PC (fills Equipe's GPS gap) |

Taps are the *machine spine* (who/what/when → routing); shots/flags/voice are the *human nuance* (variety /
why / how-good → editing & story). Routing never depends on Whisper hearing a name right — the tap already
nailed the rider.

**Coverage view:** tap the status line to see shots-by-type, subjects covered, missing types, and which logged
riders still have no shot — so you chase gaps in the moment. (GPS needs a secure origin = the GitHub Pages
URL, not the plain-http LAN serve.)

## On-screen helpers (added 2026-06-20)

| Helper | What it does |
|--------|--------------|
| **NEXT line** (under the card) | who's up next + an ETA from the class start time + `sec_per_start`, self-correcting off your taps |
| **★ priority** (list + card edge) | must-cover riders (Swedish + top-3) from `build --priority`; the coverage view flags any with no shot yet |
| **Story angle** (under the horse) | a one-line prep hook from `build --storyprep` (tdb_history) — handy when you tap a rider for an interview |
| **Flag reason** | after ★, an optional one-tap *why* (hero / emotion / fall / **🥇 Win** / sponsor / funny) — story-prep + culling |
| **🥇 Gold winners** | a **🥇 Win** flag marks that rider as a gold winner (gold badge in the list + gold ring on the card) so you chase their ceremony + reactions; **long-press** any rider to mark/unmark manually. The card debrief lists the winners and flags any with no shot yet |
| **FX6 / 1DX3 pill** | tag the active camera; ingest applies each camera's own clock offset to a mixed session |
| **B-roll checklist** (in Coverage) | tick the standing cutaways (coursewalk / crowd / sponsor / stable / venue / weather / details / atmosphere) |
| **⤢ big mode + haptics** | a giant current-rider card and a buzz on every log, so you log without leaving the viewfinder |
| **Keep screen on** | a wake-lock (Sync panel toggle, on by default) so the display doesn't sleep mid-shoot on the mount |
| **Install nudge** | in a browser tab it offers *Add to Home screen* — run it standalone for full-screen + reliable nav-bar insets (tuned for a Galaxy Note 9, 360 px / on-screen nav bar) |
| **📋 Card summary** | end-of-card debrief: counts, coverage + priority gaps, sync/GPS status, then Send / Export |

`build` flags `--priority <comma start_nos \| JSON squad/names>` and `--storyprep <tdb storyprep .md \| JSON>`
are also exposed on Tool 46 build (`-Priority` / `-StoryPrep`). All of these are optional and back-compatible —
a session that uses none of them ingests exactly as before.

## Clock sync (optional — only when a clock is off)

**If the camera clocks are set correctly, skip this** — ingest's default treats phone-local time as camera
time, which is exactly right when clocks agree. In practice the **1DX3 GPS-syncs its own clock** (and geotags
stills, so `Auto-sort -Geocode` gets the venue straight from the photos — the phone GPS below is just a
backup, mainly for the GPS-less FX6); just keep the **FX6** clock set. Only when a clock might be off do you
measure the offset **once per camera per card**:

- **Camera shoots the phone** — open **Sync**, point the camera at the screen, take one photo. The on-screen
  **QR** carries the phone time; that photo's EXIF carries the camera time → offset. (The sync frame lands on
  the card, so it flows through normal ingest.)
- **Phone shoots the camera** — read the camera's own clock and type it into **Sync** (pick FX6 / 1DX3).

Either way `ingest` converts every phone timestamp to camera wall-clock. With no sync it falls back to the
phone's timezone (assumes the camera clock ≈ phone local time) and says so. The phone is the single reference
that reconciles **both** cameras (FX6 + 1DX3) onto one timeline.

## Flow

```
field_logger.py build  --section <id>     →  field_logger/startlist.json   (rider list, from Equipe)
        (Tool 46 → Serve)                  →  phone opens http://<pc>:8137/  (Add to Home screen, offline)
        … shoot + tap/flag/talk …          →  Export  →  field_session_<id>.json (+ embedded audio)
field_logger.py ingest --session <file>    →  field_log.json   (camera-time segments)  +  field_notes.json
        Auto-sort (Tool 22) -FieldLog      →  photos/clips routed to the right rider folders
```

Run it from the workflow menu (**Tool 46**): **B**uild start list → **S**erve to phone → **I**ngest the
session. Or directly: `python field_logger.py build|ingest|selftest` (see the file header).

## Deployment & delivery

- **LAN serve (default):** Tool 46 → *Serve* runs `field_logger.py serve` = static files **+ a `POST /upload`
  receiver**. The phone opens `http://<pc>:8137/` on the same Wi-Fi, "Add to Home screen" installs it offline,
  and the phone's **Send** button posts the session straight into the project folder — no file copy. (When the
  receiver is reachable the app shows *Send*; otherwise it falls back to *Export* = download.)
- **GitHub Pages (fixed URL, no PC):** the app also lives at
  **`erikvassmar-droid.github.io/phone-ref/field-logger/`** (deployed from `phone-ref/field-logger/`). https
  there means **GPS + install work without a PC running**. The start list comes from `field_logger.py build`
  (use the LAN serve for the live list, or commit `startlist.json` into the Pages folder per event).

## Schemas

**`field_session.json`** (phone → PC, raw, phone-time):
```jsonc
{ "schema":"equisport.field_session/1", "section_id":"1251175", "tz_offset_min":-120,
  "sync":[ {"epoch_ms":1750000000000, "camera_time":"2026-05-27 11:00:00", "camera":"FX6"} ],
  "events":[
    {"kind":"segment","start_no":5,"rider":"…","activity":"round","t_start":<ms>,"t_end":<ms>,"flag":true},
    {"kind":"note","t_ms":<ms>,"start_no":5,"rider":"…","audio_id":"note_…","dur_ms":3500},
    {"kind":"flag","t_ms":<ms>,"start_no":5,"rider":"…"} ],
  "audio":{ "note_…":"data:audio/webm;base64,…" } }
```

**`field_log.json`** (PC, camera-time — identical to `field_log.py`, consumed by `Auto-sort -FieldLog`):
```jsonc
{ "segments":[ {"start":"2026-05-27 11:00:00","end":"2026-05-27 11:00:30","type":"round",
                "start_no":5,"subject":"…","via":"fieldlog-tap","source":"phone"} ] }
```

`field_notes.json` carries the flags + transcribed voice notes (story prep / Resolve markers).

## Files

| File | Role |
|------|------|
| `index.html` | single-screen UI (dark/OLED, brand green, big tap targets) |
| `app.js` | state, tap→segment logic, clock-sync, flag, voice (MediaRecorder→IndexedDB), export; exposes `window.FL` for tests |
| `qrcode.js` | vendored QR encoder (Kazuhiko Arase, MIT) for the sync QR |
| `sw.js`, `manifest.webmanifest`, `icon-*.png` | PWA (offline app shell, installable) |
| `startlist.sample.json` | demo list so the app runs standalone before you Build a real one |
| `../field_logger.py` | `build` (Equipe→startlist.json) · `ingest` (session→field_log.json + transcribe) · `selftest` |

Verified: `field_logger.py selftest` (24 checks), a Playwright end-to-end (drive the PWA → export → ingest →
`field_log.json`), and Test-Smoke assertions including a round-trip through the real `Find-FieldSegment`.
