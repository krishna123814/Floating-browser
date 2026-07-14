import streamlit as st
import sqlite3
import pandas as pd
from datetime import date, datetime, timedelta

st.set_page_config(page_title="My Daily Tracker", page_icon="📒", layout="wide")

DB_PATH = "data.db"


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS practice_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            asset TEXT NOT NULL,
            target_desc TEXT,
            completed INTEGER NOT NULL DEFAULT 0,
            notes TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            r_type TEXT NOT NULL,
            r_month INTEGER NOT NULL,
            r_day INTEGER NOT NULL,
            notes TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS milk_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_date TEXT NOT NULL,
            customer TEXT NOT NULL,
            quantity REAL NOT NULL,
            rate REAL NOT NULL,
            total REAL NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


init_db()

st.title("📒 My Daily Tracker")

tab1, tab2, tab3 = st.tabs(["🎯 Target (BankNifty / BTC)", "🎉 Reminders", "🥛 Dudh Detail"])

# ---------------------------------------------------------------------------
# TAB 1: Daily Practice Target - BankNifty & BTC
# ---------------------------------------------------------------------------
with tab1:
    st.subheader("BankNifty & BTC Daily Practice")

    with st.form("practice_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            log_date = st.date_input("Date", value=date.today())
            asset = st.selectbox("Asset", ["BankNifty", "BTC"])
        with col2:
            target_desc = st.text_input("Aaj ka target (e.g. '5 chart analyse karna')")
            completed = st.checkbox("Poora hua? (Completed)")
        notes = st.text_area("Notes (optional)")
        submitted = st.form_submit_button("Save Entry")

        if submitted:
            conn = get_conn()
            conn.execute(
                "INSERT INTO practice_log (log_date, asset, target_desc, completed, notes) VALUES (?, ?, ?, ?, ?)",
                (log_date.isoformat(), asset, target_desc, int(completed), notes),
            )
            conn.commit()
            conn.close()
            st.success("Entry saved!")
            st.rerun()

    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM practice_log ORDER BY log_date DESC, id DESC", conn)
    conn.close()

    if not df.empty:
        st.divider()

        # Quick stats
        col1, col2, col3 = st.columns(3)
        for asset_name, col in [("BankNifty", col1), ("BTC", col2)]:
            asset_df = df[df["asset"] == asset_name]
            total = len(asset_df)
            done = asset_df["completed"].sum()
            pct = (done / total * 100) if total else 0
            col.metric(f"{asset_name} Completion", f"{pct:.0f}%", f"{done}/{total} days")

        # Current streak (consecutive days with at least 1 completed entry, ending today)
        completed_dates = sorted(set(df[df["completed"] == 1]["log_date"]), reverse=True)
        streak = 0
        check_day = date.today()
        completed_dates_set = set(completed_dates)
        while check_day.isoformat() in completed_dates_set:
            streak += 1
            check_day -= timedelta(days=1)
        col3.metric("🔥 Current Streak", f"{streak} days")

        st.divider()
        st.write("**History**")
        asset_filter = st.selectbox("Filter by asset", ["All", "BankNifty", "BTC"], key="asset_filter")
        show_df = df if asset_filter == "All" else df[df["asset"] == asset_filter]
        display_df = show_df.copy()
        display_df["completed"] = display_df["completed"].map({1: "✅", 0: "❌"})
        st.dataframe(
            display_df[["log_date", "asset", "target_desc", "completed", "notes"]],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("Entry delete karni ho to"):
            del_id = st.number_input("Entry ID", min_value=1, step=1, key="del_practice")
            if st.button("Delete Entry", key="del_practice_btn"):
                conn = get_conn()
                conn.execute("DELETE FROM practice_log WHERE id=?", (del_id,))
                conn.commit()
                conn.close()
                st.rerun()
    else:
        st.info("Abhi koi entry nahi hai. Upar se pehli entry add karo.")

# ---------------------------------------------------------------------------
# TAB 2: Reminders - Birthday / Anniversary
# ---------------------------------------------------------------------------
with tab2:
    st.subheader("Birthday & Anniversary Reminders")

    with st.form("reminder_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            r_name = st.text_input("Naam")
            r_type = st.selectbox("Type", ["Birthday", "Anniversary"])
        with col2:
            r_date = st.date_input("Date (saal koi bhi, sirf month/day use hoga)", value=date.today())
        r_notes = st.text_input("Notes (optional)")
        r_submitted = st.form_submit_button("Add Reminder")

        if r_submitted and r_name:
            conn = get_conn()
            conn.execute(
                "INSERT INTO reminders (name, r_type, r_month, r_day, notes) VALUES (?, ?, ?, ?, ?)",
                (r_name, r_type, r_date.month, r_date.day, r_notes),
            )
            conn.commit()
            conn.close()
            st.success("Reminder added!")
            st.rerun()

    conn = get_conn()
    rdf = pd.read_sql_query("SELECT * FROM reminders", conn)
    conn.close()

    if not rdf.empty:
        today = date.today()

        def next_occurrence(month, day):
            try:
                this_year = date(today.year, month, day)
            except ValueError:
                this_year = date(today.year, month, 28)  # handle Feb 29 safely
            if this_year < today:
                try:
                    return date(today.year + 1, month, day)
                except ValueError:
                    return date(today.year + 1, month, 28)
            return this_year

        rdf["next_date"] = rdf.apply(lambda row: next_occurrence(row["r_month"], row["r_day"]), axis=1)
        rdf["days_left"] = rdf["next_date"].apply(lambda d: (d - today).days)
        rdf = rdf.sort_values("days_left")

        st.divider()
        upcoming = rdf[rdf["days_left"] <= 7]
        if not upcoming.empty:
            st.write("**⏰ Is hafte (7 din) me:**")
            for _, row in upcoming.iterrows():
                label = "Aaj!" if row["days_left"] == 0 else f"{row['days_left']} din baaki"
                st.warning(f"🎉 {row['r_type']} - **{row['name']}** ({row['next_date'].strftime('%d %b')}) - {label}")

        st.divider()
        st.write("**Saari Reminders**")
        show_rdf = rdf.copy()
        show_rdf["next_date"] = show_rdf["next_date"].apply(lambda d: d.strftime("%d %b %Y"))
        st.dataframe(
            show_rdf[["id", "name", "r_type", "next_date", "days_left", "notes"]],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("Reminder delete karni ho to"):
            del_rid = st.number_input("Reminder ID", min_value=1, step=1, key="del_reminder")
            if st.button("Delete Reminder", key="del_reminder_btn"):
                conn = get_conn()
                conn.execute("DELETE FROM reminders WHERE id=?", (del_rid,))
                conn.commit()
                conn.close()
                st.rerun()
    else:
        st.info("Abhi koi reminder nahi hai. Upar se add karo.")

# ---------------------------------------------------------------------------
# TAB 3: Dudh Detail - kitna kisko becha
# ---------------------------------------------------------------------------
with tab3:
    st.subheader("Dudh Bikri Detail")

    with st.form("milk_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            m_date = st.date_input("Date", value=date.today(), key="milk_date")
            m_customer = st.text_input("Customer ka naam")
        with col2:
            m_qty = st.number_input("Quantity (litre)", min_value=0.0, step=0.5, format="%.2f")
            m_rate = st.number_input("Rate (₹ per litre)", min_value=0.0, step=1.0, format="%.2f")
        m_paid = st.checkbox("Paisa mil gaya? (Paid)")
        m_submitted = st.form_submit_button("Save Sale")

        if m_submitted and m_customer:
            total = m_qty * m_rate
            conn = get_conn()
            conn.execute(
                "INSERT INTO milk_sales (sale_date, customer, quantity, rate, total, paid) VALUES (?, ?, ?, ?, ?, ?)",
                (m_date.isoformat(), m_customer, m_qty, m_rate, total, int(m_paid)),
            )
            conn.commit()
            conn.close()
            st.success(f"Sale saved! Total: ₹{total:.2f}")
            st.rerun()

    conn = get_conn()
    mdf = pd.read_sql_query("SELECT * FROM milk_sales ORDER BY sale_date DESC, id DESC", conn)
    conn.close()

    if not mdf.empty:
        st.divider()
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Revenue", f"₹{mdf['total'].sum():.2f}")
        col2.metric("Total Litre Sold", f"{mdf['quantity'].sum():.2f} L")
        unpaid = mdf[mdf["paid"] == 0]["total"].sum()
        col3.metric("Pending (Unpaid)", f"₹{unpaid:.2f}")

        st.divider()
        customer_filter = st.selectbox(
            "Filter by customer", ["All"] + sorted(mdf["customer"].unique().tolist())
        )
        show_mdf = mdf if customer_filter == "All" else mdf[mdf["customer"] == customer_filter]

        st.write("**Customer-wise Summary**")
        summary = mdf.groupby("customer").agg(
            total_litre=("quantity", "sum"),
            total_amount=("total", "sum"),
            pending=("total", lambda x: mdf.loc[x.index][mdf.loc[x.index, "paid"] == 0]["total"].sum()),
        ).reset_index()
        st.dataframe(summary, use_container_width=True, hide_index=True)

        st.write("**All Entries**")
        display_mdf = show_mdf.copy()
        display_mdf["paid"] = display_mdf["paid"].map({1: "✅ Paid", 0: "❌ Unpaid"})
        st.dataframe(
            display_mdf[["id", "sale_date", "customer", "quantity", "rate", "total", "paid"]],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("Entry delete karni ho to"):
            del_mid = st.number_input("Sale ID", min_value=1, step=1, key="del_milk")
            if st.button("Delete Sale", key="del_milk_btn"):
                conn = get_conn()
                conn.execute("DELETE FROM milk_sales WHERE id=?", (del_mid,))
                conn.commit()
                conn.close()
                st.rerun()
    else:
        st.info("Abhi koi sale entry nahi hai. Upar se add karo.")

# ---------------------------------------------------------------------------
# Sidebar - backup download
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("💾 Backup")
    st.caption("Streamlit Cloud restart/redeploy hone par data.db reset ho sakta hai. Time-to-time backup download kar lena.")
    try:
        with open(DB_PATH, "rb") as f:
            st.download_button("Download data.db backup", f, file_name="data_backup.db")
    except FileNotFoundError:
        st.caption("Abhi koi data nahi hai backup karne ke liye.")
