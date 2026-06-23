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
import json
from datetime import datetime, timezone

import streamlit as st
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import streamlit.components.v1 as components

# ── Config ───────────────────────────────────────────────────────────────

SPREADSHEET_ID = "1jHn1OBy5idSmxowZ9ldyO0KWIW_u-IPrp7_SzHRG8sY"
SHEET_NAME = "Transcript Correction Tracker"

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

def header_index(col_name):
    global _header_cache
    if _header_cache is None:
        _header_cache = sheet.row_values(1)
    return _header_cache.index(col_name) + 1

def headers():
    global _header_cache
    if _header_cache is None:
        _header_cache = sheet.row_values(1)
    return _header_cache

# ── Sheet helpers ─────────────────────────────────────────────────────────

def claim_next_chunk(corrector_name):
    """
    Fetches all row values (no dicts — faster than get_all_records on large sheets)
    and finds either an in-progress row for this corrector or the next not_started row.
    """
    all_rows = sheet.get_all_values()  # raw list of lists, no dict overhead
    hdrs = all_rows[0]
    status_col = hdrs.index("status")
    assigned_col = hdrs.index("assigned_to")

    in_progress_row_num = None
    not_started_row_num = None

    for i, row in enumerate(all_rows[1:], start=2):  # row 1 is header
        status = row[status_col] if status_col < len(row) else ""
        assigned = row[assigned_col] if assigned_col < len(row) else ""
        if status == "in_progress" and assigned == corrector_name:
            in_progress_row_num = i
            break
        if status == "not_started" and not_started_row_num is None:
            not_started_row_num = i

    row_num = in_progress_row_num or not_started_row_num
    if row_num is None:
        return None

    if in_progress_row_num is None:
        # claim it
        sheet.update_cell(row_num, status_col + 1, "in_progress")
        sheet.update_cell(row_num, assigned_col + 1, corrector_name)

    row_values = sheet.row_values(row_num)
    return row_values, row_num

def submit_correction(row_num, corrector_name, corrected_text):
    """Single batch update — 1 API call instead of 4."""
    col_status   = header_index("status")
    col_text     = header_index("corrected_transcript")
    col_by       = header_index("corrected_by")
    col_at       = header_index("corrected_at")

    start_a1 = rowcol_to_a1(row_num, col_status)
    end_a1   = rowcol_to_a1(row_num, col_at)

    sheet.update(
        [[
            "done",
            corrected_text,
            corrector_name,
            datetime.now(timezone.utc).isoformat(),
        ]],
        f"{start_a1}:{end_a1}",
    )

def flag_chunk(row_num, corrector_name):
    col_status = header_index("status")
    col_by     = header_index("corrected_by")
    col_at     = header_index("corrected_at")

    start_a1 = rowcol_to_a1(row_num, col_status)
    end_a1   = rowcol_to_a1(row_num, col_at)

    sheet.update(
        [["flagged", "", corrector_name, datetime.now(timezone.utc).isoformat()]],
        f"{start_a1}:{end_a1}",
    )

def get_progress():
    """Lightweight progress count — fetches status column only."""
    status_col_letter = rowcol_to_a1(1, header_index("status"))[0]  # e.g. "E"
    values = sheet.col_values(header_index("status"))[1:]  # skip header
    done = sum(1 for v in values if v == "done")
    return done, len(values)

# ── Drive audio fetch ────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def fetch_audio_bytes(drive_file_id):
    request = drive_service.files().get_media(fileId=drive_file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf.read()

# ── Custom audio player component (one-way, no return value) ──────────────

def audio_player_component(audio_bytes):
    audio_b64 = base64.b64encode(audio_bytes).decode()
    html = f"""
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: transparent; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
  .player {{
    background: #23262e;
    border: 1px solid #3a3e48;
    border-radius: 12px;
    padding: 14px 16px;
  }}
  audio {{ display: none; }}
  .controls {{ display: flex; align-items: center; gap: 10px; }}
  .ctrl-btn {{
    background: #2e3240;
    border: 1px solid #3a3e48;
    border-radius: 8px;
    color: #e8e6e1;
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 600;
    padding: 10px 14px;
    touch-action: manipulation;
    user-select: none;
    -webkit-tap-highlight-color: transparent;
    transition: background 0.15s;
    white-space: nowrap;
  }}
  .ctrl-btn:active {{ background: #3d4255; }}
  .play-btn {{
    background: #4f6ef7;
    border-color: #4f6ef7;
    font-size: 1.1rem;
    padding: 10px 18px;
    flex-shrink: 0;
  }}
  .play-btn:active {{ background: #3d5ce0; }}
  .progress-wrap {{ flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }}
  .progress-bar {{
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 4px;
    border-radius: 4px;
    background: #3a3e48;
    outline: none;
    cursor: pointer;
    touch-action: manipulation;
  }}
  .progress-bar::-webkit-slider-thumb {{
    -webkit-appearance: none;
    width: 16px; height: 16px;
    border-radius: 50%;
    background: #4f6ef7;
    cursor: pointer;
  }}
  .progress-bar::-moz-range-thumb {{
    width: 16px; height: 16px;
    border-radius: 50%;
    background: #4f6ef7;
    cursor: pointer;
    border: none;
  }}
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
    components.html(html, height=90, scrolling=False)

# ── Page config & global styles ───────────────────────────────────────────

st.set_page_config(page_title="Inzwi Correction", layout="centered")

st.markdown("""
<style>
  .stApp { background-color: #1a1d23; color: #e8e6e1; }
  .category-tag {
    color: #8b8f99;
    font-size: 0.85rem;
    text-transform: lowercase;
    letter-spacing: 0.02em;
    margin-bottom: 10px;
  }
  .progress-tag {
    color: #8b8f99;
    font-size: 0.9rem;
    text-align: right;
  }
  iframe { border: none !important; }
  /* Mobile textarea fixes */
  textarea {
    font-size: 16px !important;
    -webkit-overflow-scrolling: touch !important;
    touch-action: pan-y !important;
  }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────

for key, default in [
    ("corrector_name", None),
    ("current_row", None),
    ("current_row_num", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ── Name selection ────────────────────────────────────────────────────────

if "show_name_picker" not in st.session_state:
    url_name = st.query_params.get("name")
    if url_name:
        st.session_state.corrector_name = url_name
        st.session_state.show_name_picker = False
    else:
        st.session_state.show_name_picker = True

top_left, top_right = st.columns([3, 1])

with top_left:
    if st.session_state.show_name_picker:
        typed_name = st.text_input(
            "Enter your name",
            label_visibility="collapsed",
            placeholder="Your name",
        )
        if st.button("Continue"):
            clean_name = typed_name.strip().title()
            if clean_name:
                st.session_state.corrector_name = clean_name
                st.session_state.show_name_picker = False
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
    if st.session_state.corrector_name:
        done, total = get_progress()
        st.markdown(f"<div class='progress-tag'>{done:,}/{total:,}</div>", unsafe_allow_html=True)

st.divider()

# ── Main flow ─────────────────────────────────────────────────────────────

if not st.session_state.corrector_name:
    st.info("Enter your name above to start.")
else:
    if st.session_state.current_row is None:
        with st.spinner("Loading next chunk…"):
            result = claim_next_chunk(st.session_state.corrector_name)
        if result is None:
            st.success("No chunks left — everything's been corrected. 🎉")
            st.stop()
        st.session_state.current_row, st.session_state.current_row_num = result

    row_dict = dict(zip(headers(), st.session_state.current_row))

    st.markdown(f"<div class='category-tag'>{row_dict.get('category', '')}</div>", unsafe_allow_html=True)

    with st.spinner("Loading audio…"):
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
            with st.spinner("Saving…"):
                flag_chunk(st.session_state.current_row_num, st.session_state.corrector_name)
            st.session_state.current_row = None
            st.rerun()
    with col2:
        if st.button("Submit →", type="primary", use_container_width=True):
            with st.spinner("Saving…"):
                submit_correction(st.session_state.current_row_num, st.session_state.corrector_name, corrected_text)
            st.session_state.current_row = None
            st.rerun()