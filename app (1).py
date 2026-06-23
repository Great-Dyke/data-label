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
from datetime import datetime, timezone

import streamlit as st
import gspread
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

# ── Sheet helpers ─────────────────────────────────────────────────────────

def get_all_rows():
    return sheet.get_all_records()

def find_row_index(rows, predicate):
    for i, row in enumerate(rows):
        if predicate(row):
            return i + 2
    return None

def claim_next_chunk(corrector_name):
    rows = get_all_rows()
    row_num = find_row_index(
        rows, lambda r: r["status"] == "in_progress" and r["assigned_to"] == corrector_name
    )
    if row_num is None:
        row_num = find_row_index(rows, lambda r: r["status"] == "not_started")
        if row_num is None:
            return None
        sheet.update_cell(row_num, header_index("status"), "in_progress")
        sheet.update_cell(row_num, header_index("assigned_to"), corrector_name)
    return sheet.row_values(row_num), row_num

_header_cache = None

def header_index(col_name):
    global _header_cache
    if _header_cache is None:
        _header_cache = sheet.row_values(1)
    return _header_cache.index(col_name) + 1

def submit_correction(row_num, corrector_name, corrected_text):
    sheet.update_cell(row_num, header_index("status"), "done")
    sheet.update_cell(row_num, header_index("corrected_transcript"), corrected_text)
    sheet.update_cell(row_num, header_index("corrected_by"), corrector_name)
    sheet.update_cell(row_num, header_index("corrected_at"), datetime.now(timezone.utc).isoformat())

def flag_chunk(row_num, corrector_name):
    sheet.update_cell(row_num, header_index("status"), "flagged")
    sheet.update_cell(row_num, header_index("corrected_by"), corrector_name)
    sheet.update_cell(row_num, header_index("corrected_at"), datetime.now(timezone.utc).isoformat())

def get_progress():
    rows = get_all_rows()
    done = sum(1 for r in rows if r["status"] == "done")
    return done, len(rows)

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

# ── Custom audio + textarea component ────────────────────────────────────

def audio_editor_component(audio_bytes, draft_text, key="editor"):
    """
    Renders a custom HTML5 audio player with ±5s skip buttons and a
    mobile-optimised textarea. Returns the submitted text or None.
    """
    audio_b64 = base64.b64encode(audio_bytes).decode()

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: transparent;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding: 0;
  }}

  /* ── Audio player ── */
  .player {{
    background: #23262e;
    border: 1px solid #3a3e48;
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 14px;
  }}

  audio {{
    display: none;
  }}

  .controls {{
    display: flex;
    align-items: center;
    gap: 10px;
  }}

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

  .progress-wrap {{
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }}

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
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #4f6ef7;
    cursor: pointer;
  }}
  .progress-bar::-moz-range-thumb {{
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #4f6ef7;
    cursor: pointer;
    border: none;
  }}

  .time-label {{
    color: #8b8f99;
    font-size: 0.75rem;
    font-variant-numeric: tabular-nums;
  }}

  /* ── Textarea ── */
  .transcript-area {{
    width: 100%;
    min-height: 140px;
    background: #23262e;
    border: 1px solid #3a3e48;
    border-radius: 12px;
    color: #e8e6e1;
    font-size: 16px;          /* 16px prevents iOS auto-zoom */
    line-height: 1.6;
    padding: 12px 14px;
    resize: vertical;
    outline: none;
    font-family: inherit;
    -webkit-overflow-scrolling: touch;
    touch-action: pan-y;
    margin-bottom: 12px;
    display: block;
  }}
  .transcript-area:focus {{
    border-color: #4f6ef7;
  }}

  /* ── Buttons ── */
  .btn-row {{
    display: flex;
    gap: 10px;
  }}

  .action-btn {{
    flex: 1;
    padding: 14px;
    border-radius: 10px;
    border: none;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
    transition: opacity 0.15s;
  }}
  .action-btn:active {{ opacity: 0.75; }}

  .flag-btn {{
    background: #2e3240;
    color: #8b8f99;
    border: 1px solid #3a3e48;
  }}

  .submit-btn {{
    background: #4f6ef7;
    color: #fff;
  }}
</style>
</head>
<body>

<audio id="audio" src="data:audio/wav;base64,{audio_b64}" preload="auto"></audio>

<!-- Player -->
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

<!-- Transcript -->
<textarea
  class="transcript-area"
  id="transcript"
  placeholder="Edit transcript here…"
  autocomplete="off"
  autocorrect="off"
  autocapitalize="off"
  spellcheck="false"
>{draft_text}</textarea>

<!-- Actions -->
<div class="btn-row">
  <button class="action-btn flag-btn" onclick="submitResult('flag')">Flag unclear</button>
  <button class="action-btn submit-btn" onclick="submitResult('submit')">Submit →</button>
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
    if (audio.paused) {{
      audio.play();
      playBtn.textContent = '⏸';
    }} else {{
      audio.pause();
      playBtn.textContent = '▶';
    }}
  }}

  function skip(secs) {{
    audio.currentTime = Math.max(0, Math.min(audio.duration || 0, audio.currentTime + secs));
  }}

  function submitResult(action) {{
    const text = document.getElementById('transcript').value;
    // Send result back to Streamlit via query param trick
    const result = JSON.stringify({{ action: action, text: text }});
    window.parent.postMessage({{
      type: 'streamlit:setComponentValue',
      value: result
    }}, '*');
  }}
</script>
</body>
</html>
"""

    result = components.html(html, height=380, scrolling=False)
    return result


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
  /* hide Streamlit default form elements we're replacing */
  div[data-testid="stVerticalBlock"] iframe { border: none; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────

if "corrector_name" not in st.session_state:
    st.session_state.corrector_name = None
if "current_row" not in st.session_state:
    st.session_state.current_row = None
if "current_row_num" not in st.session_state:
    st.session_state.current_row_num = None
if "pending_action" not in st.session_state:
    st.session_state.pending_action = None

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
        st.markdown(f"<div class='progress-tag'>{done}/{total}</div>", unsafe_allow_html=True)

st.divider()

# ── Main flow ─────────────────────────────────────────────────────────────

if not st.session_state.corrector_name:
    st.info("Enter your name above to start.")
else:
    if st.session_state.current_row is None:
        result = claim_next_chunk(st.session_state.corrector_name)
        if result is None:
            st.success("No chunks left — everything's been corrected. 🎉")
            st.stop()
        st.session_state.current_row, st.session_state.current_row_num = result

    headers = sheet.row_values(1)
    row_dict = dict(zip(headers, st.session_state.current_row))

    st.markdown(f"<div class='category-tag'>{row_dict['category']}</div>", unsafe_allow_html=True)

    audio_bytes = fetch_audio_bytes(row_dict["drive_file_id"])
    draft = row_dict.get("scribe_transcript", "")

    # Render the custom component
    component_value = audio_editor_component(audio_bytes, draft)

    # Handle result coming back from the component
    if component_value is not None:
        import json
        try:
            result = json.loads(component_value)
            action = result.get("action")
            text = result.get("text", "")
            if action == "submit":
                submit_correction(st.session_state.current_row_num, st.session_state.corrector_name, text)
                st.session_state.current_row = None
                st.rerun()
            elif action == "flag":
                flag_chunk(st.session_state.current_row_num, st.session_state.corrector_name)
                st.session_state.current_row = None
                st.rerun()
        except (json.JSONDecodeError, KeyError):
            pass