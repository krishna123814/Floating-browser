import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timedelta
import uuid

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
SHEET_ID = "1TC20pKMIINY1zkUJfLxBQL4uTC-oCort1LlMOuVXGx4"
SHEET_NAME = "Sheet1"   # change if your tab has a different name
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

st.set_page_config(page_title="Tasks", page_icon="✅", layout="centered")

# ----------------------------------------------------------------------
# GOOGLE SHEETS CONNECTION
# ----------------------------------------------------------------------
@st.cache_resource
def get_sheet():
    """Connect to Google Sheets using a service account.

    Locally: put the downloaded JSON key file next to this script,
             named 'service_account.json'.
    On Streamlit Cloud: paste the JSON key contents into
             Settings -> Secrets as shown in the README.
    """
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            "service_account.json", scopes=SCOPES
        )
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    try:
        worksheet = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = sh.sheet1
    # make sure header row exists
    values = worksheet.get_all_values()
    if not values:
        worksheet.append_row(["id", "name", "date", "time", "done"])
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


# ----------------------------------------------------------------------
# SETTINGS (kept in session for this browser session)
# ----------------------------------------------------------------------
if "week_start" not in st.session_state:
    st.session_state.week_start = "Sunday"
if "font_size" not in st.session_state:
    st.session_state.font_size = "Medium"
if "bg_theme" not in st.session_state:
    st.session_state.bg_theme = "Sky"

FONT_SIZES = {"Small": "14px", "Medium": "16px", "Large": "19px"}
THEMES = {
    "Sky":   {"bg": "#dff0fb", "card": "#ffffff", "text": "#26313a"},
    "Mint":  {"bg": "#e2f5ea", "card": "#ffffff", "text": "#26313a"},
    "Sand":  {"bg": "#faf3e6", "card": "#ffffff", "text": "#26313a"},
    "Dark":  {"bg": "#1b2430", "card": "#26313f", "text": "#e7edf3"},
    "Plain": {"bg": "#f2f2f2", "card": "#ffffff", "text": "#26313a"},
}

theme = THEMES[st.session_state.bg_theme]
font_px = FONT_SIZES[st.session_state.font_size]

st.markdown(
    f"""
    <style>
    .stApp {{
        background-color: {theme['bg']};
        font-size: {font_px};
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
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------
# LOAD DATA
# ----------------------------------------------------------------------
try:
    worksheet = get_sheet()
    tasks_df = load_tasks(worksheet)
    connected = True
except Exception as e:
    connected = False
    tasks_df = pd.DataFrame(columns=["id", "name", "date", "time", "done"])
    st.error(f"Google Sheet se connect nahi ho paya: {e}")

# ----------------------------------------------------------------------
# HEADER
# ----------------------------------------------------------------------
col1, col2 = st.columns([5, 1])
with col1:
    st.markdown("## 📋 Tasks")
with col2:
    with st.popover("⚙️"):
        st.markdown("**Settings**")
        st.session_state.week_start = st.selectbox(
            "Week starts on", ["Sunday", "Monday"],
            index=["Sunday", "Monday"].index(st.session_state.week_start),
        )
        st.session_state.font_size = st.selectbox(
            "Text size", list(FONT_SIZES.keys()),
            index=list(FONT_SIZES.keys()).index(st.session_state.font_size),
        )
        st.session_state.bg_theme = st.selectbox(
            "Background", list(THEMES.keys()),
            index=list(THEMES.keys()).index(st.session_state.bg_theme),
        )
        if st.button("Apply"):
            st.rerun()

if connected:
    st.caption("🟢 Connected to Google Sheet")

# ----------------------------------------------------------------------
# ADD TASK FORM
# ----------------------------------------------------------------------
with st.expander("➕ Add new task"):
    with st.form("add_task_form", clear_on_submit=True):
        name = st.text_input("Task name")
        c1, c2 = st.columns(2)
        with c1:
            d = st.date_input("Date", value=date.today())
        with c2:
            t = st.time_input("Time")
        submitted = st.form_submit_button("Save task")
        if submitted:
            if not name.strip():
                st.warning("Task name daalna zaroori hai.")
            else:
                new_row = {
                    "id": str(uuid.uuid4())[:8],
                    "name": name.strip(),
                    "date": d.strftime("%Y-%m-%d"),
                    "time": t.strftime("%H:%M"),
                    "done": "False",
                }
                tasks_df = pd.concat([tasks_df, pd.DataFrame([new_row])], ignore_index=True)
                if connected:
                    save_tasks(worksheet, tasks_df)
                st.success("Task add ho gaya ✅")
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


def render_task(row, idx):
    d = parse_date(row["date"])
    label = f"{fmt_label(d)}, {row['time']}" if row.get("time") else fmt_label(d)
    cls = color_class(d, row["done"])
    title_cls = "task-title done" if row["done"] else "task-title"

    c1, c2, c3 = st.columns([0.5, 6, 0.7])
    with c1:
        checked = st.checkbox("", value=bool(row["done"]), key=f"chk_{row['id']}")
        if checked != bool(row["done"]):
            tasks_df.loc[tasks_df["id"] == row["id"], "done"] = checked
            if connected:
                save_tasks(worksheet, tasks_df)
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
        if st.button("🗑️", key=f"del_{row['id']}"):
            tasks_df.drop(tasks_df[tasks_df["id"] == row["id"]].index, inplace=True)
            if connected:
                save_tasks(worksheet, tasks_df)
            st.rerun()


if tasks_df.empty:
    st.info("Abhi koi task nahi hai. Upar '+ Add new task' se ek task add karo.")
else:
    tasks_df["_date_obj"] = tasks_df["date"].apply(parse_date)

    overdue = tasks_df[(tasks_df["_date_obj"] < today) & (~tasks_df["done"])]
    today_tasks = tasks_df[(tasks_df["_date_obj"] == today) & (~tasks_df["done"])]
    tomorrow_tasks = tasks_df[(tasks_df["_date_obj"] == tomorrow) & (~tasks_df["done"])]
    future_tasks = tasks_df[(tasks_df["_date_obj"] > tomorrow) & (~tasks_df["done"])]
    done_tasks = tasks_df[tasks_df["done"]]

    def sort_group(g):
        return g.sort_values(["date", "time"])

    if not overdue.empty:
        st.markdown('<div class="section-label overdue">Overdue</div>', unsafe_allow_html=True)
        for _, row in sort_group(overdue).iterrows():
            render_task(row, row["id"])

    if not today_tasks.empty:
        st.markdown('<div class="section-label">Today</div>', unsafe_allow_html=True)
        for _, row in sort_group(today_tasks).iterrows():
            render_task(row, row["id"])

    if not tomorrow_tasks.empty:
        st.markdown('<div class="section-label">Tomorrow</div>', unsafe_allow_html=True)
        for _, row in sort_group(tomorrow_tasks).iterrows():
            render_task(row, row["id"])

    if not future_tasks.empty:
        future_tasks = future_tasks.copy()
        future_tasks["_month"] = future_tasks["_date_obj"].apply(lambda d: d.strftime("%B %Y"))
        for month, grp in future_tasks.groupby("_month", sort=True):
            st.markdown(f'<div class="section-label">{month}</div>', unsafe_allow_html=True)
            for _, row in sort_group(grp).iterrows():
                render_task(row, row["id"])

    if not done_tasks.empty:
        st.markdown('<div class="section-label">Completed</div>', unsafe_allow_html=True)
        for _, row in sort_group(done_tasks).iterrows():
            render_task(row, row["id"])
