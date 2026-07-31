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

FONT_SIZES = {"Small": "14px", "Medium": "16px", "Large": "19px"}
ICON_SIZES = {"Small": 40, "Medium": 52, "Large": 66}
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

    /* ── ONE persistent top bar (like the reference chart.html #hdr) ──
       holds: Tasks tab, Practice tab, status dot, settings gear — always
       visible together, no matter which tab is active. Targeted via a
       stable container key (NOT :first-of-type, which also matched the
       unrelated date/time row inside the add-form and broke it). ── */
    .st-key-topbar {{
        position: fixed !important;
        top: 0; left: 0; right: 0;
        z-index: 1000;
        background: {theme['card']};
        padding: 6px 10px;
        box-shadow: 0 2px 8px rgba(0,0,0,.18);
    }}
    .st-key-topbar div[data-testid="stHorizontalBlock"] {{
        align-items: center !important;
    }}
    .st-key-topbar button {{
        min-height: unset !important;
        padding: 6px 4px !important;
    }}
    .st-key-topbar div[data-testid="stPopover"] > div > button {{
        width: {int(icon_px*0.7)}px !important;
        height: {int(icon_px*0.7)}px !important;
        border-radius: 50% !important;
        padding: 0 !important;
        font-size: {int(icon_px*0.32)}px !important;
    }}
    .topbar-spacer {{ height: {int(icon_px*0.7) + 18}px; }}

    /* connection status dot, sits inside the top bar */
    .status-dot {{
        width: 11px;
        height: 11px;
        border-radius: 50%;
        margin: 0 auto;
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
    connected = True
except Exception as e:
    connected = False
    tasks_ws = practice_ws = None
    tasks_df = EMPTY_DF.copy()
    practice_df = EMPTY_PRACTICE_DF.copy()
    st.error(f"Google Sheet se connect nahi ho paya: {e}")

# ----------------------------------------------------------------------
# TOP BAR — one persistent fixed row: Tasks | Practice | ...spacer... | dot | gear
# ----------------------------------------------------------------------
if "active_tab" not in st.session_state:
    st.session_state.active_tab = "tasks"

bar_c1, bar_c2, bar_spacer, bar_dot, bar_gear = st.container(key="topbar").columns([1, 1, 5, 0.6, 1])

with bar_c1:
    if st.button(
        "📋", key="tab_btn_tasks", use_container_width=True,
        type="primary" if st.session_state.active_tab == "tasks" else "secondary",
    ):
        st.session_state.active_tab = "tasks"
        st.rerun()
with bar_c2:
    if st.button(
        "📈", key="tab_btn_practice", use_container_width=True,
        type="primary" if st.session_state.active_tab == "practice" else "secondary",
    ):
        st.session_state.active_tab = "practice"
        st.rerun()
with bar_dot:
    dot_color = "#2e9e44" if connected else "#d32f2f"
    st.markdown(f'<div class="status-dot" style="background:{dot_color};"></div>', unsafe_allow_html=True)
with bar_gear:
    with st.popover("⚙️"):
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
            "Icon size", list(ICON_SIZES.keys()),
            index=list(ICON_SIZES.keys()).index(st.session_state.icon_size),
        )
        st.session_state.bg_theme = st.selectbox(
            "Background", list(THEMES.keys()),
            index=list(THEMES.keys()).index(st.session_state.bg_theme),
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

    c1, c2, c3 = st.columns([0.5, 6, 0.7])
    with c1:
        checked = st.checkbox("", value=bool(row["done"]), key=f"{prefix}_chk_{row['id']}")
        if checked != bool(row["done"]):
            active_df.loc[active_df["id"] == row["id"], "done"] = checked
            if connected:
                save_tasks(active_ws, active_df)
            st.rerun()
    with c2:
        st.markdown(
            f"""<div class="task-card">
                <div class="{title_cls}">{row['name']}</div>
                <div class="{cls}">{label}</div>
            </div>""",
            unsafe_allow_html=True,
        )
    with c3:
        if st.button("🗑️", key=f"{prefix}_del_{row['id']}"):
            active_df.drop(active_df[active_df["id"] == row["id"]].index, inplace=True)
            if connected:
                save_tasks(active_ws, active_df)
            st.rerun()


if prefix == "practice":
    if active_df.empty:
        st.info("Abhi koi practice record nahi hai. Neeche '➕' se add karo.")
    else:
        df_sorted = active_df.copy()
        df_sorted["_start_obj"] = df_sorted["start_date"].apply(parse_date)
        df_sorted = df_sorted.sort_values("_start_obj", ascending=False)
        for _, row in df_sorted.iterrows():
            csd = parse_date(row["chart_start_date"])
            ced = parse_date(row["chart_end_date"])
            total_days = (ced - csd).days
            rc1, rc2 = st.columns([6, 0.7])
            with rc1:
                st.markdown(
                    f"""<div class="task-card">
                        <div class="practice-row">
                            <div class="practice-field"><span class="practice-label">Start date</span>{row['start_date']}</div>
                            <div class="practice-field"><span class="practice-label">Chart start</span>{row['chart_start_date']}</div>
                            <div class="practice-field"><span class="practice-label">Chart end</span>{row['chart_end_date']}</div>
                            <div class="practice-field"><span class="practice-label">Total practice</span>{total_days} din</div>
                        </div>
                    </div>""",
                    unsafe_allow_html=True,
                )
            with rc2:
                if st.button("🗑️", key=f"practice_del_{row['id']}"):
                    active_df.drop(active_df[active_df["id"] == row["id"]].index, inplace=True)
                    if connected:
                        save_practice(active_ws, active_df)
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
