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
    all_rows = sheet.get_all_values()
    hdrs = all_rows[0]
    status_col = hdrs.index("status")
    assigned_col = hdrs.index("assigned_to")

    in_progress_row_num = None
    not_started_row_num = None

    for i, row in enumerate(all_rows[1:], start=2):
        status = row[status_col] if status_col < len(row) else ""
        assigned = row[assigned_col] if assigned_col < len(row) else ""
        if status == "in_progress" and assigned == corrector_name:
            in_progress_row_num = i
            break
        if status == "not_started" and not_started_row_num is None:
            not_started_row_num = i

    row_num = in_progress_row_num or not_started_row_num
    if row_num is None:
        return None, None

    if in_progress_row_num is None:
        sheet.update_cell(row_num, status_col + 1, "in_progress")
        sheet.update_cell(row_num, assigned_col + 1, corrector_name)

    row_values = sheet.row_values(row_num)
    return row_values, row_num

def peek_next_file_id(corrector_name, current_row_num):
    """
    Looks ahead in the sheet for the next not_started row after current_row_num.
    Returns its drive_file_id so we can prefetch audio in the background.
    """
    try:
        all_rows = sheet.get_all_values()
        hdrs = all_rows[0]
        status_col = hdrs.index("status")
        file_id_col = hdrs.index("drive_file_id")

        for i, row in enumerate(all_rows[1:], start=2):
            if i <= current_row_num:
                continue
            status = row[status_col] if status_col < len(row) else ""
            if status == "not_started":
                return row[file_id_col] if file_id_col < len(row) else None
    except Exception:
        pass
    return None

def _write_correction_bg(row_num, corrector_name, corrected_text):
    """Runs in a background thread — sheet write without blocking UI."""
    try:
        col_status = header_index("status")
        col_text   = header_index("corrected_transcript")
        col_by     = header_index("corrected_by")
        col_at     = header_index("corrected_at")
        start_a1   = rowcol_to_a1(row_num, col_status)
        end_a1     = rowcol_to_a1(row_num, col_at)
        sheet.update(
            [[
                "done",
                corrected_text,
                corrector_name,
                datetime.now(timezone.utc).isoformat(),
            ]],
            f"{start_a1}:{end_a1}",
        )
    except Exception as e:
        # Log but don't crash — UI has already moved on
        print(f"[bg write error] {e}")

def _write_flag_bg(row_num, corrector_name):
    """Runs in a background thread."""
    try:
        col_status = header_index("status")
        col_by     = header_index("corrected_by")
        col_at     = header_index("corrected_at")
        start_a1   = rowcol_to_a1(row_num, col_status)
        end_a1     = rowcol_to_a1(row_num, col_at)
        sheet.update(
            [["flagged", "", corrector_name, datetime.now(timezone.utc).isoformat()]],
            f"{start_a1}:{end_a1}",
        )
    except Exception as e:
        print(f"[bg flag error] {e}")

def submit_correction_async(row_num, corrector_name, corrected_text):
    t = threading.Thread(
        target=_write_correction_bg,
        args=(row_num, corrector_name, corrected_text),
        daemon=True,
    )
    t.start()

def flag_chunk_async(row_num, corrector_name):
    t = threading.Thread(
        target=_write_flag_bg,
        args=(row_num, corrector_name),
        daemon=True,
    )
    t.start()

def get_progress():
    values = sheet.col_values(header_index("status"))[1:]
    done = sum(1 for v in values if v == "done")
    return done, len(values)

# ── Drive audio fetch ────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def fetch_audio_bytes(drive_file_id: str) -> bytes:
    request = drive_service.files().get_media(fileId=drive_file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf.read()

def prefetch_next_audio(drive_file_id: str):
    """Warms the cache for the next chunk's audio in a background thread."""
    def _fetch():
        try:
            fetch_audio_bytes(drive_file_id)
        except Exception as e:
            print(f"[prefetch error] {e}")
    threading.Thread(target=_fetch, daemon=True).start()

# ── Custom audio player component ─────────────────────────────────────────

def audio_player_component(audio_bytes: bytes):
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
    # Claim chunk if we don't have one
    if st.session_state.current_row is None:
        with st.spinner("Loading…"):
            result, row_num = claim_next_chunk(st.session_state.corrector_name)
        if result is None:
            st.success("No chunks left — everything's been corrected. 🎉")
            st.stop()
        st.session_state.current_row = result
        st.session_state.current_row_num = row_num

    row_dict = dict(zip(headers(), st.session_state.current_row))

    # Fetch current audio (likely already cached after first load)
    audio_bytes = fetch_audio_bytes(row_dict["drive_file_id"])

    # Prefetch next chunk's audio in background while corrector works
    next_file_id = peek_next_file_id(
        st.session_state.corrector_name,
        st.session_state.current_row_num
    )
    if next_file_id:
        prefetch_next_audio(next_file_id)

    st.markdown(f"<div class='category-tag'>{row_dict.get('category', '')}</div>", unsafe_allow_html=True)

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
            # Fire sheet write in background, move on instantly
            flag_chunk_async(st.session_state.current_row_num, st.session_state.corrector_name)
            st.session_state.current_row = None
            st.session_state.current_row_num = None
            st.rerun()
    with col2:
        if st.button("Submit →", type="primary", use_container_width=True):
            # Fire sheet write in background, move on instantly
            submit_correction_async(
                st.session_state.current_row_num,
                st.session_state.corrector_name,
                corrected_text,
            )
            st.session_state.current_row = None
            st.session_state.current_row_num = None
            st.rerun()