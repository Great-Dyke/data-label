"""
Inzwi Correction App
Lets correctors listen to a Shona audio chunk, see ElevenLabs Scribe's
draft transcript pre-filled, edit it, and submit — tracked live in a
Google Sheet so progress and pay-per-chunk are always visible.

Deploy on Streamlit Community Cloud. Requires a Google service account
with access to both the Sheet and the Drive folder containing the audio.
"""

import io
from datetime import datetime, timezone

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Config — fill these in ───────────────────────────────────────────────

SPREADSHEET_ID = "1jHn1OBy5idSmxowZ9ldyO0KWIW_u-IPrp7_SzHRG8sY"
SHEET_NAME = "Transcript Correction Tracker"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ── Auth (reads from Streamlit secrets — never hardcode credentials) ────

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
    return sheet.get_all_records()  # list of dicts, keyed by header row

def find_row_index(rows, predicate):
    """Returns the 1-indexed sheet row number (header is row 1) for the first match, or None."""
    for i, row in enumerate(rows):
        if predicate(row):
            return i + 2  # +1 for 0-index, +1 for header row
    return None

def claim_next_chunk(corrector_name):
    rows = get_all_rows()

    # resume an already-claimed-but-unfinished chunk for this person first
    row_num = find_row_index(
        rows, lambda r: r["status"] == "in_progress" and r["assigned_to"] == corrector_name
    )
    if row_num is None:
        row_num = find_row_index(rows, lambda r: r["status"] == "not_started")
        if row_num is None:
            return None  # nothing left
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

# ── Styling ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Inzwi Correction", layout="centered")

st.markdown("""
<style>
    .stApp { background-color: #1a1d23; color: #e8e6e1; }
    .stTextArea textarea {
        font-size: 1.05rem;
        line-height: 1.6;
        background-color: #23262e;
        color: #e8e6e1;
        border: 1px solid #3a3e48;
    }
    .stButton button {
        min-height: 3rem;
        font-size: 1rem;
        font-weight: 600;
        border-radius: 8px;
        width: 100%;
    }
    .category-tag {
        color: #8b8f99;
        font-size: 0.85rem;
        text-transform: lowercase;
        letter-spacing: 0.02em;
    }
    .progress-tag {
        color: #8b8f99;
        font-size: 0.9rem;
        text-align: right;
    }
</style>
""", unsafe_allow_html=True)

# ── App state ────────────────────────────────────────────────────────────

if "corrector_name" not in st.session_state:
    st.session_state.corrector_name = None
if "current_row" not in st.session_state:
    st.session_state.current_row = None
if "current_row_num" not in st.session_state:
    st.session_state.current_row_num = None

# ── Name selection ───────────────────────────────────────────────────────

if "show_name_picker" not in st.session_state:
    # check the URL first — a personalized link (?name=Jane) is what makes the
    # name "stick" across visits, since session_state alone resets every time
    # someone closes the tab and comes back
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
                # don't change corrector_name yet — only the dropdown reappears;
                # the in-progress chunk (if any) stays correctly attributed to
                # whoever currently holds it until a new name is actually picked
                st.session_state.show_name_picker = True
                st.rerun()

with top_right:
    if st.session_state.corrector_name:
        done, total = get_progress()
        st.markdown(f"<div class='progress-tag'>{done}/{total}</div>", unsafe_allow_html=True)

st.divider()

# ── Main flow ────────────────────────────────────────────────────────────

if not st.session_state.corrector_name:
    st.info("Pick your name above to start.")
else:
    if st.session_state.current_row is None:
        result = claim_next_chunk(st.session_state.corrector_name)
        if result is None:
            st.success("No chunks left — everything's been corrected. 🎉")
            st.stop()
        st.session_state.current_row, st.session_state.current_row_num = result

    headers = sheet.row_values(1)
    row_dict = dict(zip(headers, st.session_state.current_row))

    audio_bytes = fetch_audio_bytes(row_dict["drive_file_id"])
    st.audio(audio_bytes, format="audio/wav")

    st.markdown(f"<div class='category-tag'>{row_dict['category']}</div>", unsafe_allow_html=True)

    corrected_text = st.text_area(
        "Transcript",
        value=row_dict.get("scribe_transcript", ""),
        height=160,
        label_visibility="collapsed",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Flag unclear"):
            flag_chunk(st.session_state.current_row_num, st.session_state.corrector_name)
            st.session_state.current_row = None
            st.rerun()
    with col2:
        if st.button("Submit →", type="primary"):
            submit_correction(st.session_state.current_row_num, st.session_state.corrector_name, corrected_text)
            st.session_state.current_row = None
            st.rerun()