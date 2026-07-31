import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timedelta
import uuid

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
SHEET_ID = "1TC20pKMIINY1zkUJfLxBQL4uTC-oCort1LlMOuVXGx4"
SHEET_NAME = "Sheet1"       # Tasks tab
PRACTICE_SHEET_NAME = "Practice"   # Trading-practice tab (auto-created if missing)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

st.set_page_config(page_title="Tasks", page_icon="✅", layout="centered")

# ----------------------------------------------------------------------
# GOOGLE SHEETS CONNECTION
# ----------------------------------------------------------------------
@st.cache_resource
def get_spreadsheet():
    """Connect to the Google Sheet using a service account (cached)."""
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            "service_account.json", scopes=SCOPES
        )
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def get_worksheet(sh, name, create_if_missing=True, header=None):
    """Get (or create) a worksheet tab by name, and make sure it has a header row."""
    header = header or ["id", "name", "date", "time", "done"]
    try:
        worksheet = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        if create_if_missing:
            worksheet = sh.add_worksheet(title=name, rows=200, cols=10)
        else:
            worksheet = sh.sheet1
    values = worksheet.get_all_values()
    if not values:
        worksheet.append_row(header)
    return worksheet


def load_tasks(worksheet):
    records = worksheet.get_all_records()
    df = pd.DataFrame(records)
    if df.empty:
        df = pd.DataFrame(columns=["id", "name", "date", "time", "done"])
    else:
        df["done"] = df["done"].astype(str).str.lower().isin(["true", "1", "yes"])
    return df


def save_tasks(worksheet, df):
    worksheet.clear()
    out = df.copy()
    out["done"] = out["done"].astype(str)
    rows = [out.columns.tolist()] + out.values.tolist()
    worksheet.update(rows)


PRACTICE_COLUMNS = ["id", "start_date", "chart_start_date", "chart_end_date"]


def load_practice(worksheet):
    records = worksheet.get_all_records()
    df = pd.DataFrame(records)
    if df.empty:
        df = pd.DataFrame(columns=PRACTICE_COLUMNS)
    return df


def save_practice(worksheet, df):
    worksheet.clear()
    out = df[PRACTICE_COLUMNS].copy()
    rows = [out.columns.tolist()] + out.values.tolist()
    worksheet.update(rows)


TARGETS_SHEET_NAME = "PracticeTargets"
TARGET_COLUMNS = ["total_target_days", "per_day_target"]
DEFAULT_TOTAL_TARGET_DAYS = 7
DEFAULT_PER_DAY_TARGET = 27


def load_targets(worksheet):
    records = worksheet.get_all_records()
    if not records:
        return DEFAULT_TOTAL_TARGET_DAYS, DEFAULT_PER_DAY_TARGET
    row = records[0]
    try:
        total_target_days = int(row.get("total_target_days") or DEFAULT_TOTAL_TARGET_DAYS)
    except (TypeError, ValueError):
        total_target_days = DEFAULT_TOTAL_TARGET_DAYS
    try:
        per_day_target = int(row.get("per_day_target") or DEFAULT_PER_DAY_TARGET)
    except (TypeError, ValueError):
        per_day_target = DEFAULT_PER_DAY_TARGET
    return total_target_days, per_day_target


def save_targets(worksheet, total_target_days, per_day_target):
    worksheet.clear()
    worksheet.update([TARGET_COLUMNS, [total_target_days, per_day_target]])


# ----------------------------------------------------------------------
# SETTINGS (kept in session for this browser session)
# ----------------------------------------------------------------------
if "week_start" not in st.session_state:
    st.session_state.week_start = "Sunday"
if "font_size" not in st.session_state:
    st.session_state.font_size = "Medium"
if "bg_theme" not in st.session_state:
    st.session_state.bg_theme = "Sky"
if "icon_size" not in st.session_state:
    st.session_state.icon_size = "Medium"
# top header (dark toolbar strip) — size + per-icon colour, set from Settings
if "topbar_height" not in st.session_state:
    st.session_state.topbar_height = "Medium"
if "topbar_icon_size" not in st.session_state:
    st.session_state.topbar_icon_size = "Medium"
if "task_icon_color" not in st.session_state:
    st.session_state.task_icon_color = "#9598a1"
if "practice_icon_color" not in st.session_state:
    st.session_state.practice_icon_color = "#9598a1"
if "settings_icon_color" not in st.session_state:
    st.session_state.settings_icon_color = "#9598a1"

FONT_SIZES = {"Extra Small": "12px", "Small": "14px", "Medium": "16px", "Large": "19px"}
ICON_SIZES = {"Small": 40, "Medium": 52, "Large": 66}
TOPBAR_HEIGHTS = {"Small": 28, "Medium": 34, "Large": 42}       # header button height, px
TOPBAR_ICON_SIZES = {"Small": 12, "Medium": 15, "Large": 18}    # header icon font-size, px
THEMES = {
    "Sky":   {"bg": "#dff0fb", "card": "#ffffff", "text": "#26313a"},
    "Mint":  {"bg": "#e2f5ea", "card": "#ffffff", "text": "#26313a"},
    "Sand":  {"bg": "#faf3e6", "card": "#ffffff", "text": "#26313a"},
    "Dark":  {"bg": "#1b2430", "card": "#26313f", "text": "#e7edf3"},
    "Plain": {"bg": "#f2f2f2", "card": "#ffffff", "text": "#26313a"},
}

theme = THEMES[st.session_state.bg_theme]
font_px = FONT_SIZES[st.session_state.font_size]
icon_px = ICON_SIZES[st.session_state.icon_size]
bar_h = TOPBAR_HEIGHTS[st.session_state.topbar_height]
bar_icon_px = TOPBAR_ICON_SIZES[st.session_state.topbar_icon_size]
task_color = st.session_state.task_icon_color
practice_color = st.session_state.practice_icon_color
gear_color = st.session_state.settings_icon_color


st.markdown(
    f"""
    <style>
    /* hide Streamlit's own header/menu/footer + Streamlit Cloud's own
       toolbar/badge/"Manage app" button so more screen is usable */
    #MainMenu                          {{display:none !important;}}
    header                             {{display:none !important; height:0;}}
    footer                             {{display:none !important;}}
    [data-testid="stToolbar"]          {{display:none !important;}}
    [data-testid="stDecoration"]       {{display:none !important;}}
    [data-testid="stStatusWidget"]     {{display:none !important;}}
    [data-testid="manage-app-button"]  {{display:none !important;}}
    .viewerBadge_container__1QSob      {{display:none !important;}}
    .styles_viewerBadge__1yB5_         {{display:none !important;}}
    #stDecoration                      {{display:none !important;}}

    .stApp {{
        background-color: {theme['bg']};
        font-size: {font_px};
    }}

    /* ── ONE persistent top bar — dark compact toolbar strip, styled after
       the reference chart.html #hdr (TradingView-style header): dark bg,
       thin border, small squarish icon buttons in a row, status dot
       tucked in the right corner. Targeted via a stable container key
       (NOT :first-of-type, which also matched the unrelated date/time
       row inside the add-form and broke it). ── */
    .st-key-topbar {{
        position: fixed !important;
        top: 0; left: 0; right: 0;
        z-index: 1000;
        height: {bar_h + 12}px !important;
        overflow: hidden !important;
        background: #1e222d;
        border-bottom: 1px solid #2a2e39;
        padding: 6px 10px;
        box-shadow: 0 2px 8px rgba(0,0,0,.35);
    }}
    /* Streamlit auto-stacks st.columns vertically on narrow/mobile screens
       (its own responsive media query) — force it to stay a single row. */
    .st-key-topbar div[data-testid="stHorizontalBlock"] {{
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        align-items: center !important;
        gap: 6px !important;
        width: 100% !important;
    }}
    .st-key-topbar div[data-testid="column"],
    .st-key-topbar div[data-testid="stColumn"] {{
        width: fit-content !important;
        flex: unset !important;
        min-width: unset !important;
    }}
    /* push the last column (status dot) to the far right corner, instead
       of relying on a wide empty spacer column (which broke across
       Streamlit versions that rename the column data-testid) */
    .st-key-topbar div[data-testid="stHorizontalBlock"] > div:last-child {{
        margin-left: auto !important;
    }}
    .st-key-topbar button {{
        min-height: unset !important;
        height: {bar_h}px !important;
        padding: 0 12px !important;
        background: transparent !important;
        border: 1px solid #363c4e !important;
        border-radius: 6px !important;
        color: #9598a1 !important;
        font-size: {bar_icon_px}px !important;
        white-space: nowrap !important;
    }}
    .st-key-topbar button:hover {{
        border-color: #2962ff !important;
        color: #d1d4dc !important;
    }}
    .st-key-topbar button[kind="primary"] {{
        background: #2962ff !important;
        border-color: #2962ff !important;
        color: #fff !important;
    }}
    .st-key-topbar div[data-testid="stPopover"] > div > button {{
        width: {bar_h}px !important;
        height: {bar_h}px !important;
        border-radius: 6px !important;
        padding: 0 !important;
        font-size: {bar_icon_px}px !important;
        background: transparent !important;
        border: 1px solid #363c4e !important;
        color: #9598a1 !important;
    }}
    .st-key-topbar div[data-testid="stPopover"] > div > button:hover {{
        border-color: #2962ff !important;
        color: #d1d4dc !important;
    }}
    .topbar-spacer {{ height: {bar_h + 20}px; }}

    /* per-icon colours, set from Settings — border+text when inactive,
       solid fill when the tab is the active one (kind="primary") */
    .st-key-tab_btn_tasks button {{
        border-color: {task_color} !important;
        color: {task_color} !important;
    }}
    .st-key-tab_btn_tasks button[kind="primary"] {{
        background: {task_color} !important;
        border-color: {task_color} !important;
        color: #fff !important;
    }}
    .st-key-tab_btn_practice button {{
        border-color: {practice_color} !important;
        color: {practice_color} !important;
    }}
    .st-key-tab_btn_practice button[kind="primary"] {{
        background: {practice_color} !important;
        border-color: {practice_color} !important;
        color: #fff !important;
    }}
    .st-key-settings_popover div[data-testid="stPopover"] > div > button {{
        border-color: {gear_color} !important;
        color: {gear_color} !important;
    }}

    /* connection status dot — tucked in the top-right corner, like #ws-dot */
    .status-dot-wrap {{
        display: flex;
        align-items: center;
        justify-content: flex-end;
        height: {bar_h}px;
    }}
    .status-dot {{
        width: 8px;
        height: 8px;
        border-radius: 50%;
        box-shadow: 0 0 4px rgba(0,0,0,.35);
    }}

    /* add-task, fixed bottom-left, round icon-only FAB */
    div[data-testid="stExpander"] {{
        position: fixed !important;
        bottom: 18px;
        left: 14px;
        width: {icon_px}px !important;
        z-index: 1000;
    }}
    div[data-testid="stExpander"] summary {{
        width: {icon_px}px !important;
        height: {icon_px}px !important;
        border-radius: 50% !important;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: {int(icon_px*0.45)}px !important;
        background: {theme['card']} !important;
        box-shadow: 0 2px 8px rgba(0,0,0,.3);
    }}
    div[data-testid="stExpander"] summary svg {{ display: none; }}
    div[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {{
        position: fixed;
        bottom: {icon_px + 34}px;
        left: 14px;
        width: min(320px, 90vw);
        background: {theme['card']};
        border-radius: 14px;
        padding: 14px;
        box-shadow: 0 4px 16px rgba(0,0,0,.35);
    }}
    .task-card {{
        background: {theme['card']};
        color: {theme['text']};
        border-radius: 14px;
        padding: 14px 18px;
        margin-bottom: 10px;
        box-shadow: 0 1px 3px rgba(20,30,40,.12);
    }}
    /* Tap-anywhere-on-the-card to open its edit/delete panel — an
       invisible full-size button sits on top of the visual card.
       [class*=...] matches every row's own unique key (task_wrap_<id>,
       practice_wrap_<id>) with one static rule. */
    div[class*="st-key-task_wrap_"],
    div[class*="st-key-practice_wrap_"] {{
        position: relative !important;
    }}
    div[class*="st-key-task_wrap_"] .task-card,
    div[class*="st-key-practice_wrap_"] .task-card {{
        margin-bottom: 0;
    }}
    div[class*="st-key-task_wrap_"] div[data-testid="stButton"],
    div[class*="st-key-practice_wrap_"] div[data-testid="stButton"] {{
        position: absolute !important;
        inset: 0 !important;
        z-index: 5;
        margin: 0 !important;
    }}
    div[class*="st-key-task_wrap_"] div[data-testid="stButton"] button,
    div[class*="st-key-practice_wrap_"] div[data-testid="stButton"] button {{
        width: 100% !important;
        height: 100% !important;
        min-height: unset !important;
        opacity: 0 !important;
        cursor: pointer;
        border: none !important;
        background: transparent !important;
        padding: 0 !important;
        margin: 0 !important;
    }}
    div[class*="st-key-task_edit_"],
    div[class*="st-key-practice_edit_"] {{
        background: {theme['card']};
        border-radius: 14px;
        padding: 10px 14px 2px;
        margin: -6px 0 10px;
        box-shadow: 0 1px 3px rgba(20,30,40,.12);
    }}
    .task-title {{
        font-size: 1.1em;
        font-weight: 600;
    }}
    .task-title.done {{
        text-decoration: line-through;
        opacity: .55;
    }}
    .task-date-red {{ color:#d32f2f; font-weight:600; font-size:.9em; }}
    .task-date-blue {{ color:#1565c0; font-weight:600; font-size:.9em; }}
    .task-date-gray {{ color:#8a97a3; font-weight:600; font-size:.9em; }}
    .section-label {{
        font-weight:700; font-size:1.05em; margin:18px 0 8px;
        color:{theme['text']};
    }}
    .section-label.overdue {{ color:#d32f2f; }}

    /* Practice tab: fields laid out horizontally in one row per entry */
    .practice-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 18px;
        align-items: baseline;
    }}
    .practice-field {{ font-size: 1em; }}
    .practice-label {{
        display: block;
        font-size: .72em;
        font-weight: 700;
        text-transform: uppercase;
        color: #8a97a3;
        letter-spacing: .03em;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------
# FULLSCREEN ON FIRST TAP (best-effort; works on Chrome Android)
# ----------------------------------------------------------------------
components.html(
    """
    <script>
    (function() {
        function tryFS(win) {
            try {
                var doc = win.document;
                var el = doc.documentElement;
                var rfs = el.requestFullscreen || el.webkitRequestFullscreen || el.mozRequestFullScreen;
                if (doc.fullscreenElement || !rfs) return false;
                var p = rfs.call(el, {navigationUI: 'hide'});
                if (p && p.catch) p.catch(function(){});
                return true;
            } catch (_) { return false; }
        }
        var done = false;
        function goFullscreen() {
            if (done) return;
            done = true;
            document.removeEventListener('touchstart', goFullscreen, {capture: true});
            document.removeEventListener('pointerdown', goFullscreen, {capture: true});
            // try the real browser tab first (hides address bar + status bar),
            // then the embedding page, then fall back to this frame itself
            tryFS(window.top) || tryFS(window.parent) || tryFS(window);
        }
        document.addEventListener('touchstart', goFullscreen, {capture: true, passive: true, once: true});
        document.addEventListener('pointerdown', goFullscreen, {capture: true, once: true});
    })();
    </script>
    """,
    height=0,
)

# ----------------------------------------------------------------------
# LOAD DATA (both Tasks and Practice tabs, from the same Google Sheet)
# ----------------------------------------------------------------------
EMPTY_DF = pd.DataFrame(columns=["id", "name", "date", "time", "done"])
EMPTY_PRACTICE_DF = pd.DataFrame(columns=PRACTICE_COLUMNS)
try:
    sh = get_spreadsheet()
    tasks_ws = get_worksheet(sh, SHEET_NAME, create_if_missing=False)
    tasks_df = load_tasks(tasks_ws)
    practice_ws = get_worksheet(sh, PRACTICE_SHEET_NAME, create_if_missing=True, header=PRACTICE_COLUMNS)
    practice_df = load_practice(practice_ws)
    targets_ws = get_worksheet(sh, TARGETS_SHEET_NAME, create_if_missing=True, header=TARGET_COLUMNS)
    loaded_total_target_days, loaded_per_day_target = load_targets(targets_ws)
    connected = True
except Exception as e:
    connected = False
    tasks_ws = practice_ws = targets_ws = None
    tasks_df = EMPTY_DF.copy()
    practice_df = EMPTY_PRACTICE_DF.copy()
    loaded_total_target_days, loaded_per_day_target = DEFAULT_TOTAL_TARGET_DAYS, DEFAULT_PER_DAY_TARGET
    st.error(f"Google Sheet se connect nahi ho paya: {e}")

if "practice_total_target_days" not in st.session_state:
    st.session_state.practice_total_target_days = loaded_total_target_days
if "practice_per_day_target" not in st.session_state:
    st.session_state.practice_per_day_target = loaded_per_day_target

# ----------------------------------------------------------------------
# TOP BAR — one persistent fixed row: Tasks | Practice | Gear | ...spacer... | dot
# (dark toolbar strip, styled after the reference chart.html #hdr)
# ----------------------------------------------------------------------
if "active_tab" not in st.session_state:
    st.session_state.active_tab = "tasks"

bar_c1, bar_c2, bar_gear, bar_dot = st.container(key="topbar").columns([1, 1, 1, 1])

with bar_c1:
    if st.button(
        "📋", key="tab_btn_tasks",
        type="primary" if st.session_state.active_tab == "tasks" else "secondary",
    ):
        st.session_state.active_tab = "tasks"
        st.rerun()
with bar_c2:
    if st.button(
        "📈", key="tab_btn_practice",
        type="primary" if st.session_state.active_tab == "practice" else "secondary",
    ):
        st.session_state.active_tab = "practice"
        st.rerun()
with bar_gear:
    with st.popover("⚙️", key="settings_popover"):
        st.markdown(
            '<div style="text-align:right; margin-top:-8px;">'
            '<span onclick="document.body.click()" '
            'style="cursor:pointer; font-size:1.3em; padding:4px 8px;">✕</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown("**Settings**")
        st.session_state.week_start = st.selectbox(
            "Week starts on", ["Sunday", "Monday"],
            index=["Sunday", "Monday"].index(st.session_state.week_start),
        )
        st.session_state.font_size = st.selectbox(
            "Text size", list(FONT_SIZES.keys()),
            index=list(FONT_SIZES.keys()).index(st.session_state.font_size),
        )
        st.session_state.icon_size = st.selectbox(
            "Icon size (add-task button)", list(ICON_SIZES.keys()),
            index=list(ICON_SIZES.keys()).index(st.session_state.icon_size),
        )
        st.session_state.bg_theme = st.selectbox(
            "Background", list(THEMES.keys()),
            index=list(THEMES.keys()).index(st.session_state.bg_theme),
        )

        st.markdown("**Top header**")
        st.session_state.topbar_height = st.selectbox(
            "Header size", list(TOPBAR_HEIGHTS.keys()),
            index=list(TOPBAR_HEIGHTS.keys()).index(st.session_state.topbar_height),
        )
        st.session_state.topbar_icon_size = st.selectbox(
            "Header icon size", list(TOPBAR_ICON_SIZES.keys()),
            index=list(TOPBAR_ICON_SIZES.keys()).index(st.session_state.topbar_icon_size),
        )
        cc1, cc2, cc3 = st.columns(3)
        with cc1:
            st.session_state.task_icon_color = st.color_picker(
                "Task 📋", st.session_state.task_icon_color,
            )
        with cc2:
            st.session_state.practice_icon_color = st.color_picker(
                "Practice 📈", st.session_state.practice_icon_color,
            )
        with cc3:
            st.session_state.settings_icon_color = st.color_picker(
                "Settings ⚙️", st.session_state.settings_icon_color,
            )
with bar_dot:
    dot_color = "#26a69a" if connected else "#787b86"
    st.markdown(
        f'<div class="status-dot-wrap"><div class="status-dot" style="background:{dot_color};"></div></div>',
        unsafe_allow_html=True,
    )

# spacer so page content isn't hidden behind the fixed top bar
st.markdown('<div class="topbar-spacer"></div>', unsafe_allow_html=True)

if st.session_state.active_tab == "tasks":
    active_df, active_ws, active_label, prefix = tasks_df, tasks_ws, "Task", "tasks"
else:
    active_df, active_ws, active_label, prefix = practice_df, practice_ws, "Practice", "practice"

# ----------------------------------------------------------------------
# ADD FORM (icon-only FAB, fixed bottom-left) — adds to whichever tab is active
# ----------------------------------------------------------------------
with st.expander("➕"):
    if prefix == "practice":
        with st.form("add_form_practice", clear_on_submit=True):
            pc1, pc2, pc3 = st.columns(3)
            with pc1:
                sd = st.date_input("Start date", value=date.today())
            with pc2:
                csd = st.date_input("Chart start date", value=date.today())
            with pc3:
                ced = st.date_input("Chart end date", value=date.today())
            submitted = st.form_submit_button("Save")
            if submitted:
                if ced < csd:
                    st.warning("Chart end date, chart start date se pehle nahi ho sakti.")
                else:
                    new_row = {
                        "id": str(uuid.uuid4())[:8],
                        "start_date": sd.strftime("%Y-%m-%d"),
                        "chart_start_date": csd.strftime("%Y-%m-%d"),
                        "chart_end_date": ced.strftime("%Y-%m-%d"),
                    }
                    active_df = pd.concat([active_df, pd.DataFrame([new_row])], ignore_index=True)
                    if connected:
                        save_practice(active_ws, active_df)
                    st.success("Practice entry add ho gaya ✅")
                    st.rerun()
    else:
        with st.form(f"add_form_{prefix}", clear_on_submit=True):
            name = st.text_input(f"{active_label} name")
            c1, c2 = st.columns(2)
            with c1:
                d = st.date_input("Date", value=date.today())
            with c2:
                t = st.time_input("Time")
            submitted = st.form_submit_button("Save")
            if submitted:
                if not name.strip():
                    st.warning("Naam daalna zaroori hai.")
                else:
                    new_row = {
                        "id": str(uuid.uuid4())[:8],
                        "name": name.strip(),
                        "date": d.strftime("%Y-%m-%d"),
                        "time": t.strftime("%H:%M"),
                        "done": "False",
                    }
                    active_df = pd.concat([active_df, pd.DataFrame([new_row])], ignore_index=True)
                    if connected:
                        save_tasks(active_ws, active_df)
                    st.success(f"{active_label} add ho gaya ✅")
                    st.rerun()

# ----------------------------------------------------------------------
# GROUP TASKS BY DATE
# ----------------------------------------------------------------------
today = date.today()
tomorrow = today + timedelta(days=1)


def parse_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except Exception:
        return today


def fmt_label(d):
    if d == today:
        return "Today"
    if d == tomorrow:
        return "Tomorrow"
    return d.strftime("%b %d")


def color_class(d, done):
    if done:
        return "task-date-gray"
    if d <= today:
        return "task-date-red"
    return "task-date-blue"


def render_task(row):
    d = parse_date(row["date"])
    label = f"{fmt_label(d)}, {row['time']}" if row.get("time") else fmt_label(d)
    cls = color_class(d, row["done"])
    title_cls = "task-title done" if row["done"] else "task-title"
    is_editing = st.session_state.get("task_edit_open") == row["id"]

    c1, c2 = st.columns([0.5, 6.7])
    with c1:
        checked = st.checkbox("", value=bool(row["done"]), key=f"{prefix}_chk_{row['id']}")
        if checked != bool(row["done"]):
            active_df.loc[active_df["id"] == row["id"], "done"] = checked
            if connected:
                save_tasks(active_ws, active_df)
            st.rerun()
    with c2:
        with st.container(key=f"task_wrap_{row['id']}"):
            st.markdown(
                f"""<div class="task-card">
                    <div class="{title_cls}">{row['name']}</div>
                    <div class="{cls}">{label}</div>
                </div>""",
                unsafe_allow_html=True,
            )
            if st.button("", key=f"{prefix}_open_{row['id']}"):
                st.session_state["task_edit_open"] = None if is_editing else row["id"]
                st.rerun()

        if is_editing:
            with st.container(key=f"task_edit_{row['id']}"):
                with st.form(f"edit_form_{prefix}_{row['id']}"):
                    new_name = st.text_input(f"{active_label} name", value=row["name"])
                    ec1, ec2 = st.columns(2)
                    with ec1:
                        new_date = st.date_input("Date", value=d)
                    with ec2:
                        try:
                            t_val = datetime.strptime(str(row["time"]), "%H:%M").time()
                        except Exception:
                            t_val = datetime.now().time()
                        new_time = st.time_input("Time", value=t_val)
                    fc1, fc2, fc3 = st.columns(3)
                    with fc1:
                        save_clicked = st.form_submit_button("💾 Save")
                    with fc2:
                        delete_clicked = st.form_submit_button("🗑️ Delete")
                    with fc3:
                        cancel_clicked = st.form_submit_button("✕ Cancel")

                    if save_clicked:
                        if not new_name.strip():
                            st.warning("Naam daalna zaroori hai.")
                        else:
                            active_df.loc[active_df["id"] == row["id"], "name"] = new_name.strip()
                            active_df.loc[active_df["id"] == row["id"], "date"] = new_date.strftime("%Y-%m-%d")
                            active_df.loc[active_df["id"] == row["id"], "time"] = new_time.strftime("%H:%M")
                            if connected:
                                save_tasks(active_ws, active_df)
                            st.session_state["task_edit_open"] = None
                            st.success(f"{active_label} update ho gaya ✅")
                            st.rerun()
                    if delete_clicked:
                        active_df.drop(active_df[active_df["id"] == row["id"]].index, inplace=True)
                        if connected:
                            save_tasks(active_ws, active_df)
                        st.session_state["task_edit_open"] = None
                        st.rerun()
                    if cancel_clicked:
                        st.session_state["task_edit_open"] = None
                        st.rerun()


if prefix == "practice":
    # ── Targets: Total target (days) × Per day target (chart-din) ──
    with st.popover("🎯 Target set karo"):
        t_col1, t_col2 = st.columns(2)
        with t_col1:
            new_total_target = st.number_input(
                "Total target (days)", min_value=1, step=1,
                value=int(st.session_state.practice_total_target_days),
            )
        with t_col2:
            new_per_day_target = st.number_input(
                "Per day target (chart-din)", min_value=1, step=1,
                value=int(st.session_state.practice_per_day_target),
            )
        st.caption(f"Total target = {int(new_total_target)} × {int(new_per_day_target)} = **{int(new_total_target) * int(new_per_day_target)} din**")
        if st.button("💾 Target save karo", key="save_practice_targets"):
            st.session_state.practice_total_target_days = int(new_total_target)
            st.session_state.practice_per_day_target = int(new_per_day_target)
            if connected:
                save_targets(targets_ws, int(new_total_target), int(new_per_day_target))
            st.success("Target save ho gaya ✅")
            st.rerun()

    total_target_days = st.session_state.practice_total_target_days
    per_day_target = st.session_state.practice_per_day_target
    overall_target = total_target_days * per_day_target

    if not active_df.empty:
        _entry_days = (
            active_df["chart_end_date"].apply(parse_date) - active_df["chart_start_date"].apply(parse_date)
        ).apply(lambda td: td.days)
        achieved_days = int(_entry_days.sum())
    else:
        achieved_days = 0
    progress_pct = min(1.0, achieved_days / overall_target) if overall_target else 0.0

    # streak: consecutive most-recent calendar days that hit the per-day target
    streak = 0
    if not active_df.empty:
        g = active_df.copy()
        g["_sd"] = g["start_date"].apply(parse_date)
        g["_days"] = (
            g["chart_end_date"].apply(parse_date) - g["chart_start_date"].apply(parse_date)
        ).apply(lambda td: td.days)
        daily_totals = g.groupby("_sd")["_days"].sum()
        dates_desc = sorted(daily_totals.index, reverse=True)
        if dates_desc:
            expected = dates_desc[0]
            for dt in dates_desc:
                if dt == expected and daily_totals[dt] >= per_day_target:
                    streak += 1
                    expected = dt - timedelta(days=1)
                else:
                    break

    st.markdown(
        f"""<div class="task-card">
            <div class="practice-row">
                <div class="practice-field"><span class="practice-label">Total target</span>{overall_target} din ({total_target_days}×{per_day_target})</div>
                <div class="practice-field"><span class="practice-label">Achieved</span>{achieved_days} din</div>
                <div class="practice-field"><span class="practice-label">Streak</span>🔥 {streak} din</div>
            </div>
        </div>""",
        unsafe_allow_html=True,
    )
    st.progress(progress_pct)

    if not active_df.empty:
        _chart_df = active_df.copy()
        _chart_df["_sd"] = _chart_df["start_date"].apply(parse_date)
        _chart_df["_days"] = (
            _chart_df["chart_end_date"].apply(parse_date) - _chart_df["chart_start_date"].apply(parse_date)
        ).apply(lambda td: td.days)
        daily_chart = _chart_df.groupby("_sd")["_days"].sum().sort_index()
        daily_chart.index = daily_chart.index.map(lambda d: d.strftime("%d %b"))
        daily_chart.name = "Din practice kiya"
        st.bar_chart(daily_chart)

    if active_df.empty:
        st.info("Abhi koi practice record nahi hai. Neeche '➕' se add karo.")
    else:
        df_sorted = active_df.copy()
        df_sorted["_start_obj"] = df_sorted["start_date"].apply(parse_date)
        df_sorted = df_sorted.sort_values("_start_obj", ascending=False)
        for _, row in df_sorted.iterrows():
            csd = parse_date(row["chart_start_date"])
            ced = parse_date(row["chart_end_date"])
            sd_obj = parse_date(row["start_date"])
            total_days = (ced - csd).days
            is_editing = st.session_state.get("practice_edit_open") == row["id"]
            badge = (
                '<span style="color:#2e9e44;font-weight:700;">✅ Target achieved</span>'
                if total_days >= per_day_target
                else '<span style="color:#d32f2f;font-weight:700;">— Target miss</span>'
            )

            with st.container(key=f"practice_wrap_{row['id']}"):
                st.markdown(
                    f"""<div class="task-card">
                        <div class="practice-row">
                            <div class="practice-field"><span class="practice-label">Start date</span>{row['start_date']}</div>
                            <div class="practice-field"><span class="practice-label">Chart start</span>{row['chart_start_date']}</div>
                            <div class="practice-field"><span class="practice-label">Chart end</span>{row['chart_end_date']}</div>
                            <div class="practice-field"><span class="practice-label">Total practice</span>{total_days} din</div>
                            <div class="practice-field"><span class="practice-label">Status</span>{badge}</div>
                        </div>
                    </div>""",
                    unsafe_allow_html=True,
                )
                if st.button("", key=f"practice_open_{row['id']}"):
                    st.session_state["practice_edit_open"] = None if is_editing else row["id"]
                    st.rerun()

            if is_editing:
                with st.container(key=f"practice_edit_{row['id']}"):
                    with st.form(f"edit_form_practice_{row['id']}"):
                        pec1, pec2, pec3 = st.columns(3)
                        with pec1:
                            new_sd = st.date_input("Start date", value=sd_obj)
                        with pec2:
                            new_csd = st.date_input("Chart start date", value=csd)
                        with pec3:
                            new_ced = st.date_input("Chart end date", value=ced)
                        fc1, fc2, fc3 = st.columns(3)
                        with fc1:
                            save_clicked = st.form_submit_button("💾 Save")
                        with fc2:
                            delete_clicked = st.form_submit_button("🗑️ Delete")
                        with fc3:
                            cancel_clicked = st.form_submit_button("✕ Cancel")

                        if save_clicked:
                            if new_ced < new_csd:
                                st.warning("Chart end date, chart start date se pehle nahi ho sakti.")
                            else:
                                active_df.loc[active_df["id"] == row["id"], "start_date"] = new_sd.strftime("%Y-%m-%d")
                                active_df.loc[active_df["id"] == row["id"], "chart_start_date"] = new_csd.strftime("%Y-%m-%d")
                                active_df.loc[active_df["id"] == row["id"], "chart_end_date"] = new_ced.strftime("%Y-%m-%d")
                                if connected:
                                    save_practice(active_ws, active_df)
                                st.session_state["practice_edit_open"] = None
                                st.success("Practice entry update ho gaya ✅")
                                st.rerun()
                        if delete_clicked:
                            active_df.drop(active_df[active_df["id"] == row["id"]].index, inplace=True)
                            if connected:
                                save_practice(active_ws, active_df)
                            st.session_state["practice_edit_open"] = None
                            st.rerun()
                        if cancel_clicked:
                            st.session_state["practice_edit_open"] = None
                            st.rerun()
else:
    if active_df.empty:
        st.info(f"Abhi koi {active_label.lower()} nahi hai. Neeche '➕' se add karo.")
    else:
        active_df["_date_obj"] = active_df["date"].apply(parse_date)

        overdue = active_df[(active_df["_date_obj"] < today) & (~active_df["done"])]
        today_tasks = active_df[(active_df["_date_obj"] == today) & (~active_df["done"])]
        tomorrow_tasks = active_df[(active_df["_date_obj"] == tomorrow) & (~active_df["done"])]
        future_tasks = active_df[(active_df["_date_obj"] > tomorrow) & (~active_df["done"])]
        done_tasks = active_df[active_df["done"]]

        def sort_group(g):
            return g.sort_values(["date", "time"])

        if not overdue.empty:
            st.markdown('<div class="section-label overdue">Overdue</div>', unsafe_allow_html=True)
            for _, row in sort_group(overdue).iterrows():
                render_task(row)

        if not today_tasks.empty:
            st.markdown('<div class="section-label">Today</div>', unsafe_allow_html=True)
            for _, row in sort_group(today_tasks).iterrows():
                render_task(row)

        if not tomorrow_tasks.empty:
            st.markdown('<div class="section-label">Tomorrow</div>', unsafe_allow_html=True)
            for _, row in sort_group(tomorrow_tasks).iterrows():
                render_task(row)

        if not future_tasks.empty:
            future_tasks = future_tasks.copy()
            future_tasks["_month"] = future_tasks["_date_obj"].apply(lambda d: d.strftime("%B %Y"))
            for month, grp in future_tasks.groupby("_month", sort=True):
                st.markdown(f'<div class="section-label">{month}</div>', unsafe_allow_html=True)
                for _, row in sort_group(grp).iterrows():
                    render_task(row)

        if not done_tasks.empty:
            st.markdown('<div class="section-label">Completed</div>', unsafe_allow_html=True)
            for _, row in sort_group(done_tasks).iterrows():
                render_task(row)
