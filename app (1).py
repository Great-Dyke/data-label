"""
Inzwi Correction App
Lets correctors listen to a Shona audio chunk, see ElevenLabs Scribe's
draft transcript pre-filled, edit it, and submit — tracked live in a
Google Sheet so progress and pay-per-chunk are always visible.

Deploy on Streamlit Community Cloud. Requires a Google service account
with access to both the Sheet and the Drive folder containing the audio.
"""

import io
import base64
import threading
from datetime import datetime, timezone
from collections import deque

import streamlit as st
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Config ───────────────────────────────────────────────────────────────

SPREADSHEET_ID = "1jHn1OBy5idSmxowZ9ldyO0KWIW_u-IPrp7_SzHRG8sY"
SHEET_NAME = "Transcript Correction Tracker"
PREFETCH_AHEAD = 2  # how many chunks to prefetch audio for

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ── Auth ──────────────────────────────────────────────────────────────────

@st.cache_resource
def get_clients():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    return sheet, drive_service

sheet, drive_service = get_clients()

# ── Header index (cached) ─────────────────────────────────────────────────

_header_cache = None

def get_headers():
    global _header_cache
    if _header_cache is None:
        _header_cache = sheet.row_values(1)
    return _header_cache

def header_index(col_name):
    return get_headers().index(col_name) + 1

# ── Queue bootstrap — runs ONCE per session ───────────────────────────────

def bootstrap_queue(corrector_name: str):
    """
    Downloads the full sheet once, finds:
      - any in-progress row for this corrector (goes to front)
      - all not_started rows (queued in order)
    Stores a deque of (row_num, row_dict) in session_state.
    Also counts done rows for the progress counter.
    Never called again after first load.
    """
    all_rows = sheet.get_all_values()
    hdrs = all_rows[0]
    status_col    = hdrs.index("status")
    assigned_col  = hdrs.index("assigned_to")
    file_id_col   = hdrs.index("drive_file_id")

    in_progress = None
    queue = deque()
    done_count = 0
    total_count = len(all_rows) - 1

    for i, row in enumerate(all_rows[1:], start=2):
        status   = row[status_col]   if status_col   < len(row) else ""
        assigned = row[assigned_col] if assigned_col < len(row) else ""

        if status == "done" or status == "flagged":
            done_count += 1
        elif status == "in_progress" and assigned == corrector_name:
            in_progress = (i, dict(zip(hdrs, row)))
        elif status == "not_started":
            queue.append((i, dict(zip(hdrs, row))))

    if in_progress:
        queue.appendleft(in_progress)

    st.session_state.chunk_queue    = queue
    st.session_state.done_count     = done_count
    st.session_state.total_count    = total_count
    st.session_state.queue_loaded   = True

# ── Claim next from local queue (no network call) ─────────────────────────

def claim_next_from_queue(corrector_name: str):
    """
    Pops the next chunk from the local queue.
    Fires a background write to mark it in_progress in the sheet.
    Returns (row_num, row_dict) or (None, None).
    """
    queue = st.session_state.chunk_queue
    if not queue:
        return None, None

    row_num, row_dict = queue.popleft()

    # Mark in_progress in background — don't block
    def _claim():
        try:
            col_s = header_index("status")
            col_a = header_index("assigned_to")
            sheet.update(
                [["in_progress", corrector_name]],
                f"{rowcol_to_a1(row_num, col_s)}:{rowcol_to_a1(row_num, col_a)}",
            )
        except Exception as e:
            print(f"[claim error] {e}")
    threading.Thread(target=_claim, daemon=True).start()

    return row_num, row_dict

# ── Background sheet writes ───────────────────────────────────────────────

def _write_correction_bg(row_num, corrector_name, corrected_text):
    try:
        now = datetime.now(timezone.utc).isoformat()
        sheet.batch_update([
            {"range": rowcol_to_a1(row_num, header_index("status")),               "values": [["done"]]},
            {"range": rowcol_to_a1(row_num, header_index("corrected_transcript")), "values": [[corrected_text]]},
            {"range": rowcol_to_a1(row_num, header_index("corrected_by")),         "values": [[corrector_name]]},
            {"range": rowcol_to_a1(row_num, header_index("corrected_at")),         "values": [[now]]},
        ])
    except Exception as e:
        print(f"[bg write error] {e}")

def _write_flag_bg(row_num, corrector_name):
    try:
        now = datetime.now(timezone.utc).isoformat()
        sheet.batch_update([
            {"range": rowcol_to_a1(row_num, header_index("status")),       "values": [["flagged"]]},
            {"range": rowcol_to_a1(row_num, header_index("corrected_by")), "values": [[corrector_name]]},
            {"range": rowcol_to_a1(row_num, header_index("corrected_at")), "values": [[now]]},
        ])
    except Exception as e:
        print(f"[bg flag error] {e}")

def submit_correction_async(row_num, corrector_name, corrected_text):
    threading.Thread(
        target=_write_correction_bg,
        args=(row_num, corrector_name, corrected_text),
        daemon=True,
    ).start()

def flag_chunk_async(row_num, corrector_name):
    threading.Thread(
        target=_write_flag_bg,
        args=(row_num, corrector_name),
        daemon=True,
    ).start()

# ── Drive audio fetch ────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, max_entries=10)
def fetch_audio_bytes(drive_file_id: str) -> bytes:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            request = drive_service.files().get_media(fileId=drive_file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buf.seek(0)
            return buf.read()
        except Exception as e:
            if attempt < max_retries - 1:
                import time
                time.sleep(1.5 ** attempt)  # 1s, 1.5s backoff
            else:
                raise

def prefetch_audio_bg(file_ids: list):
    """Fire-and-forget: warm cache for upcoming chunks."""
    def _fetch():
        for fid in file_ids:
            try:
                fetch_audio_bytes(fid)
            except Exception as e:
                print(f"[prefetch error] {e}")
    threading.Thread(target=_fetch, daemon=True).start()

# ── Audio player component ────────────────────────────────────────────────

def audio_player_component(audio_bytes: bytes):
    audio_b64 = base64.b64encode(audio_bytes).decode()
    html = f"""
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: transparent; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
  .player {{ background: #23262e; border: 1px solid #3a3e48; border-radius: 12px; padding: 14px 16px; }}
  audio {{ display: none; }}
  .controls {{ display: flex; align-items: center; gap: 10px; }}
  .ctrl-btn {{
    background: #2e3240; border: 1px solid #3a3e48; border-radius: 8px;
    color: #e8e6e1; cursor: pointer; font-size: 0.85rem; font-weight: 600;
    padding: 10px 14px; touch-action: manipulation; user-select: none;
    -webkit-tap-highlight-color: transparent; transition: background 0.15s; white-space: nowrap;
  }}
  .ctrl-btn:active {{ background: #3d4255; }}
  .play-btn {{ background: #4f6ef7; border-color: #4f6ef7; font-size: 1.1rem; padding: 10px 18px; flex-shrink: 0; }}
  .play-btn:active {{ background: #3d5ce0; }}
  .progress-wrap {{ flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }}
  .progress-bar {{
    -webkit-appearance: none; appearance: none; width: 100%; height: 4px;
    border-radius: 4px; background: #3a3e48; outline: none; cursor: pointer; touch-action: manipulation;
  }}
  .progress-bar::-webkit-slider-thumb {{ -webkit-appearance: none; width: 16px; height: 16px; border-radius: 50%; background: #4f6ef7; cursor: pointer; }}
  .progress-bar::-moz-range-thumb {{ width: 16px; height: 16px; border-radius: 50%; background: #4f6ef7; cursor: pointer; border: none; }}
  .time-label {{ color: #8b8f99; font-size: 0.75rem; font-variant-numeric: tabular-nums; }}
</style>
<audio id="audio" src="data:audio/wav;base64,{audio_b64}" preload="auto"></audio>
<div class="player">
  <div class="controls">
    <button class="ctrl-btn" onclick="skip(-5)">⏪ 5s</button>
    <button class="play-btn ctrl-btn" id="playBtn" onclick="togglePlay()">▶</button>
    <button class="ctrl-btn" onclick="skip(5)">5s ⏩</button>
    <div class="progress-wrap">
      <input class="progress-bar" type="range" id="progressBar" value="0" step="0.1">
      <span class="time-label" id="timeLabel">0:00 / 0:00</span>
    </div>
  </div>
</div>
<script>
  const audio = document.getElementById('audio');
  const playBtn = document.getElementById('playBtn');
  const progressBar = document.getElementById('progressBar');
  const timeLabel = document.getElementById('timeLabel');
  function fmt(s) {{
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60).toString().padStart(2, '0');
    return m + ':' + sec;
  }}
  audio.addEventListener('timeupdate', () => {{
    if (!audio.duration) return;
    progressBar.value = (audio.currentTime / audio.duration) * 100;
    timeLabel.textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
  }});
  audio.addEventListener('ended', () => {{ playBtn.textContent = '▶'; }});
  progressBar.addEventListener('input', () => {{
    if (audio.duration) audio.currentTime = (progressBar.value / 100) * audio.duration;
  }});
  function togglePlay() {{
    if (audio.paused) {{ audio.play(); playBtn.textContent = '⏸'; }}
    else {{ audio.pause(); playBtn.textContent = '▶'; }}
  }}
  function skip(secs) {{
    audio.currentTime = Math.max(0, Math.min(audio.duration || 0, audio.currentTime + secs));
  }}
</script>
"""
    st.iframe(html, height=90)

# ── Page config & styles ──────────────────────────────────────────────────

st.set_page_config(page_title="Inzwi Correction", layout="centered")
st.markdown("""
<style>
  .stApp { background-color: #1a1d23; color: #e8e6e1; }
  .category-tag { color: #8b8f99; font-size: 0.85rem; text-transform: lowercase; letter-spacing: 0.02em; margin-bottom: 10px; }
  .progress-tag { color: #8b8f99; font-size: 0.9rem; text-align: right; }
  iframe { border: none !important; }
  textarea { font-size: 16px !important; -webkit-overflow-scrolling: touch !important; touch-action: pan-y !important; -webkit-user-select: text !important; user-select: text !important; }
  #MainMenu {visibility: hidden;}
  footer {visibility: hidden;}
  header {visibility: hidden;}
  [data-testid="stToolbar"] {visibility: hidden;}
  [data-testid="stDecoration"] {visibility: hidden;}
  [data-testid="stStatusWidget"] {visibility: hidden;}
  [data-testid="collapsedControl"] {display: none;}
  section[data-testid="stSidebar"] {display: none;}
  #GithubIcon {display: none !important;}
  [data-testid="stHeader"] {display: none !important;}
  [data-testid="stToolbar"] {display: none !important;}
  [data-testid="stDecoration"] {display: none !important;}
  [data-testid="stStatusWidget"] {display: none !important;}
  .viewerBadge_container__1QSob {display: none !important;}
  .styles_viewerBadge__1yB5_ {display: none !important;}
  .viewerBadge_link__qRIco {display: none !important;}
  .viewerBadge_text__1CPSC {display: none !important;}
  /* nuclear option — hide anything with github in the href */
  a[href*="github"] {display: none !important;}
</style>
<script>
(function() {
  // Prevent anything except textarea itself from stealing keyboard focus
  document.addEventListener('mousedown', function(e) {
    if (e.target.tagName !== 'TEXTAREA') {
      e.preventDefault();
    }
  }, true);
  document.addEventListener('touchstart', function(e) {
    if (e.target.tagName !== 'TEXTAREA') {
      // Save and restore textarea focus after touch
      var ta = document.querySelector('textarea');
      if (ta && document.activeElement === ta) {
        var start = ta.selectionStart;
        var end = ta.selectionEnd;
        setTimeout(function() {
          ta.focus();
          ta.setSelectionRange(start, end);
        }, 50);
      }
    }
  }, { passive: true, capture: true });
})();
</script>
""", unsafe_allow_html=True)

# ── Session state defaults ────────────────────────────────────────────────

for key, default in [
    ("corrector_name", None),
    ("show_name_picker", None),
    ("queue_loaded", False),
    ("chunk_queue", deque()),
    ("done_count", 0),
    ("total_count", 0),
    ("current_row_num", None),
    ("current_row_dict", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ── Name selection ────────────────────────────────────────────────────────

if st.session_state.show_name_picker is None:
    url_name = st.query_params.get("name")
    if url_name:
        st.session_state.corrector_name = url_name
        st.session_state.show_name_picker = False
    else:
        st.session_state.show_name_picker = True

top_left, top_right = st.columns([3, 1])

with top_left:
    if st.session_state.show_name_picker:
        typed_name = st.text_input("Your name", placeholder="Your name", label_visibility="collapsed")
        if st.button("Continue"):
            clean_name = typed_name.strip().title()
            if clean_name:
                st.session_state.corrector_name = clean_name
                st.session_state.show_name_picker = False
                st.session_state.queue_loaded = False  # force reload for new name
                st.query_params["name"] = clean_name
                st.rerun()
    else:
        name_col, switch_col = st.columns([4, 1])
        with name_col:
            st.markdown(f"Correcting as **{st.session_state.corrector_name}**")
        with switch_col:
            if st.button("Switch", key="switch_user"):
                st.session_state.show_name_picker = True
                st.rerun()

with top_right:
    if st.session_state.corrector_name and st.session_state.queue_loaded:
        done  = st.session_state.done_count
        total = st.session_state.total_count
        st.markdown(f"<div class='progress-tag'>{done:,}/{total:,}</div>", unsafe_allow_html=True)

st.divider()

# ── Main flow ─────────────────────────────────────────────────────────────

if not st.session_state.corrector_name:
    st.info("Enter your name above to start.")
else:
    # ── One-time queue bootstrap (the only blocking sheet read) ───────────
    if not st.session_state.queue_loaded:
        with st.spinner("Setting up your queue…"):
            bootstrap_queue(st.session_state.corrector_name)
        st.rerun()

    # ── Claim next chunk from local queue (instant, no network) ───────────
    if st.session_state.current_row_dict is None:
        row_num, row_dict = claim_next_from_queue(st.session_state.corrector_name)
        if row_dict is None:
            st.success("No chunks left — everything's been corrected. 🎉")
            st.stop()
        st.session_state.current_row_num  = row_num
        st.session_state.current_row_dict = row_dict

        # Prefetch audio for next PREFETCH_AHEAD chunks in background
        upcoming_ids = [
            r["drive_file_id"]
            for _, r in list(st.session_state.chunk_queue)[:PREFETCH_AHEAD]
            if r.get("drive_file_id")
        ]
        if upcoming_ids:
            prefetch_audio_bg(upcoming_ids)

    row_dict = st.session_state.current_row_dict

    # ── Render ────────────────────────────────────────────────────────────
    st.markdown(f"<div class='category-tag'>{row_dict.get('category', '')}</div>", unsafe_allow_html=True)

    # Audio fetch — instant if prefetch already ran, one Drive call otherwise
    audio_bytes = fetch_audio_bytes(row_dict["drive_file_id"])
    audio_player_component(audio_bytes)

    corrected_text = st.text_area(
        "Transcript",
        value=row_dict.get("scribe_transcript", ""),
        height=160,
        label_visibility="collapsed",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Flag unclear", use_container_width=True):
            flag_chunk_async(st.session_state.current_row_num, st.session_state.corrector_name)
            st.session_state.done_count += 1
            st.session_state.current_row_dict = None
            st.session_state.current_row_num  = None
            st.rerun()
    with col2:
        if st.button("Submit →", type="primary", use_container_width=True):
            submit_correction_async(
                st.session_state.current_row_num,
                st.session_state.corrector_name,
                corrected_text,
            )
            st.session_state.done_count += 1
            st.session_state.current_row_dict = None
            st.session_state.current_row_num  = None
            st.rerun()