import io
import os

import pandas as pd
import streamlit as st
from supabase import create_client, Client

st.set_page_config(layout="wide")

# ==================================================
# SUPABASE CONNECTION
# ==================================================
# Add these in Streamlit Cloud -> Settings -> Secrets:
#
# SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
# SUPABASE_KEY = "YOUR_SUPABASE_ANON_KEY"
#
# The program no longer reads any CSV file from the local machine.
# All master data and timetable data are read/written in Supabase.

SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL"))
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY"))

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error(
        "Supabase credentials are missing. Add SUPABASE_URL and SUPABASE_KEY "
        "to Streamlit Secrets."
    )
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==================================================
# CONSTANTS
# ==================================================
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
PERIODS = [1, 2, 3, 4, 5, 6, 7]

DAY_MAP = {
    "MON": "Monday",
    "TUE": "Tuesday",
    "WED": "Wednesday",
    "THU": "Thursday",
    "FRI": "Friday",
    "SAT": "Saturday",
}

TWO_PERIOD_SUBS = {"EWS", "ITWS", "EGT", "NSS", "HWYS"}
THREE_PERIOD_SUBS = {"EEEWS(ECE)", "EEEWS(EEE)", "EGP"}
EXCLUDE_THEORY_ROOM = {"ITWS", "EWS", "EGP"}

BI_LABS = [
    {"EC LAB", "EP LAB"},
    {"EP LAB", "NAS LAB"},
]

CONTINUOUS_SLOTS = {(1, 2), (3, 4), (1, 4), (5, 7)}

WEEKLY_TEST_FACULTY = "WEEKLY_TEST_FACULTY"

# ==================================================
# SUPABASE TABLE NAMES
# ==================================================
TABLE_FACULTY = "faculty"
TABLE_SUBJECTS = "subjects"
TABLE_CLASSES = "classes"
TABLE_TEACHING = "teaching_load"
TABLE_FAC_AVAIL = "faculty_availability"
TABLE_LABS = "labs"
TABLE_ROOMS = "rooms"
TABLE_TIMETABLE = "timetable"
TABLE_ROOM_LOCKS = "class_room_locks"


# ==================================================
# HELPERS
# ==================================================
def clean(x):
    if pd.isna(x):
        return "NA"
    return str(x).strip().upper()


def fetch_table(table_name):
    """Read all rows from a Supabase table, including tables > 1000 rows."""
    rows = []
    start = 0
    page_size = 1000

    while True:
        response = (
            supabase.table(table_name)
            .select("*")
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = response.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return pd.DataFrame(rows)


def fetch_master_data():
    try:
        faculty = fetch_table(TABLE_FACULTY)
        subjects = fetch_table(TABLE_SUBJECTS)
        classes_df = fetch_table(TABLE_CLASSES)
        teaching = fetch_table(TABLE_TEACHING)
        fac_avail = fetch_table(TABLE_FAC_AVAIL)
        labs_df = fetch_table(TABLE_LABS)
        rooms_df = fetch_table(TABLE_ROOMS)
        return faculty, subjects, classes_df, teaching, fac_avail, labs_df, rooms_df
    except Exception as e:
        st.error(f"Could not read master data from Supabase: {e}")
        st.stop()


def load_timetable():
    try:
        rows = fetch_table(TABLE_TIMETABLE)
        if rows.empty:
            return []

        # The database ID is useful for deletion but not needed in the UI.
        return rows.to_dict("records")
    except Exception as e:
        st.error(f"Could not load timetable from Supabase: {e}")
        st.stop()


def save_timetable_entry(entry):
    try:
        supabase.table(TABLE_TIMETABLE).insert(entry).execute()
    except Exception as e:
        st.error(f"Could not save timetable entry to Supabase: {e}")
        return False
    return True


def delete_timetable_entry(cls, day, period):
    try:
        supabase.table(TABLE_TIMETABLE).delete().match(
            {"class_id": cls, "day": day, "period": int(period)}
        ).execute()
        return True
    except Exception as e:
        st.error(f"Could not delete timetable entry from Supabase: {e}")
        return False


def load_room_locks():
    try:
        rows = fetch_table(TABLE_ROOM_LOCKS)
        if rows.empty:
            return {}

        return {
            str(r["class_id"]): str(r["room"])
            for r in rows.to_dict("records")
        }
    except Exception as e:
        st.error(f"Could not load room locks from Supabase: {e}")
        st.stop()


def lock_class_to_room(cls, room):
    try:
        supabase.table(TABLE_ROOM_LOCKS).upsert(
            {"class_id": cls, "room": room},
            on_conflict="class_id",
        ).execute()
        return True
    except Exception as e:
        st.error(f"Could not save room lock: {e}")
        return False


def unlock_class_room(cls):
    try:
        supabase.table(TABLE_ROOM_LOCKS).delete().eq("class_id", cls).execute()
        return True
    except Exception as e:
        st.error(f"Could not remove room lock: {e}")
        return False


def ensure_weekly_tests():
    """Create Monday P1 weekly-test rows once for classes that do not have one."""
    existing = st.session_state.TT

    for cls in CLASSES:
        already_exists = any(
            r.get("Class") == cls
            and r.get("Subject") == "WEEKLY TEST"
            and r.get("Day") == "Monday"
            and int(r.get("Period", 0)) == 1
            for r in existing
        )

        if not already_exists:
            entry = {
                "class_id": cls,
                "subject": "WEEKLY TEST",
                "faculty_id": WEEKLY_TEST_FACULTY,
                "day": "Monday",
                "period": 1,
                "room": "",
            }

            if save_timetable_entry(entry):
                st.session_state.TT.append(
                    {
                        "Class": cls,
                        "Subject": "WEEKLY TEST",
                        "Faculty": WEEKLY_TEST_FACULTY,
                        "Day": "Monday",
                        "Period": 1,
                        "Room": "",
                    }
                )


def subject_duration(sub):
    if sub.endswith("LAB"):
        return 3
    if sub in THREE_PERIOD_SUBS:
        return 3
    if sub in TWO_PERIOD_SUBS:
        return 2
    return 1


def subject_progress(cls, sub):
    used = sum(
        1
        for r in st.session_state.TT
        if r["Class"] == cls and r["Subject"] == sub
    )
    total = SUB_MAX_HOURS.get((cls, sub), 0)
    return f"{used}/{total}"


def pending_load_row(cls):
    subs = teaching[teaching["Class_ID"] == cls]["Subject_ID"].unique()
    parts = []

    for s in subs:
        used = sum(
            1
            for r in st.session_state.TT
            if r["Class"] == cls and r["Subject"] == s
        )
        total = SUB_MAX_HOURS.get((cls, s), 0)

        if used < total:
            parts.append(f"{s}: {used}/{total}")

    return " | ".join(parts) if parts else "All load completed"


def library_overflow(day, period):
    used = {
        r["Class"]
        for r in st.session_state.TT
        if r.get("Room") == "LIBRARY"
        and r["Day"] == day
        and int(r["Period"]) == int(period)
    }

    # Maximum 3 classes in the library at one period.
    return len(used) >= 3


# ==================================================
# LOAD ALL DATA FROM SUPABASE
# ==================================================
faculty, subjects, classes_df, teaching, fac_avail, labs_df, rooms_df = (
    fetch_master_data()
)

for data in [
    faculty,
    subjects,
    classes_df,
    teaching,
    fac_avail,
    labs_df,
    rooms_df,
]:
    for c in data.columns:
        if data[c].dtype == object:
            data[c] = data[c].apply(clean)

# ==================================================
# LOOKUPS
# ==================================================
FAC_NAME = dict(zip(faculty["Faculty_ID"], faculty["Faculty_Name"]))

SUB_FAC = {
    (r.Class_ID, r.Subject_ID): r.Faculty_ID
    for _, r in teaching.iterrows()
}

SUB_MAX_HOURS = {
    (r.Class_ID, r.Subject_ID): int(r.Hours)
    for _, r in teaching.iterrows()
}

FAC_BLOCKED = {
    (
        r.Faculty_ID,
        DAY_MAP.get(str(r.Day).upper(), r.Day),
        int(r.Period),
    )
    for _, r in fac_avail.iterrows()
}

LAB_ROOMS = dict(zip(labs_df["Lab_Subject"], labs_df["Room"]))

# ==================================================
# ROOMS
# ==================================================

# Room_ID = actual room/lab name
if "Room" not in rooms_df.columns:
    st.error(
        "❌ Supabase 'rooms' table must contain a 'Room_ID' column. "
        f"Available columns: {list(rooms_df.columns)}"
    )
    st.stop()

if "Type" not in rooms_df.columns:
    st.error(
        "❌ Supabase 'rooms' table must contain a 'Type' column. "
        f"Available columns: {list(rooms_df.columns)}"
    )
    st.stop()

# Clean room information
rooms_df["Room_ID"] = (
    rooms_df["Room_ID"]
    .astype(str)
    .str.strip()
    .str.upper()
)

rooms_df["Type"] = (
    rooms_df["Type"]
    .astype(str)
    .str.strip()
    .str.upper()
)

# All rooms
ALL_ROOMS = (
    rooms_df["Room_ID"]
    .dropna()
    .unique()
    .tolist()
)

# Theory rooms only
THEORY_ROOMS = (
    rooms_df.loc[
        rooms_df["Type"].isin(["THEORY", "THEORY ROOM", "CLASSROOM"]),
        "Room_ID"
    ]
    .dropna()
    .unique()
    .tolist()
)

# Lab rooms only
LAB_ROOMS_FROM_DB = (
    rooms_df.loc[
        rooms_df["Type"].isin(["LAB", "LAB ROOM", "LABORATORY"]),
        "Room_ID"
    ]
    .dropna()
    .unique()
    .tolist()
)

# First 14 theory rooms are the default primary rooms
PRIMARY_ROOMS = THEORY_ROOMS[:14]

CLASSES = sorted(classes_df["Class_ID"].dropna().unique().tolist())
LOCKED_CLASSES = CLASSES[:14]
FLEX_CLASSES = CLASSES[14:]

if not CLASSES:
    st.error("No classes found in Supabase table 'classes'.")
    st.stop()

# ==================================================
# SESSION STATE
# ==================================================
if "TT" not in st.session_state:
    st.session_state.TT = load_timetable()

    # Add weekly test only if it is not already in Supabase.
    ensure_weekly_tests()

if "CLASS_ROOM_LOCK" not in st.session_state:
    st.session_state.CLASS_ROOM_LOCK = load_room_locks()

# ==================================================
# CORE CHECKS
# ==================================================
def busy(key, val, day, p):
    return any(
        r[key] == val
        and r["Day"] == day
        and int(r["Period"]) == int(p)
        for r in st.session_state.TT
    )


def is_bi_lab_pair(sub1, sub2):
    return any({sub1, sub2} == b for b in BI_LABS)


def room_clash(day, start, dur, room):
    return any(
        r["Room"] == room
        and r["Day"] == day
        and int(r["Period"]) in range(start, start + dur)
        for r in st.session_state.TT
    )


def is_continuous(start, dur):
    return (start, start + dur - 1) in CONTINUOUS_SLOTS


# ==================================================
# THEORY ROOM ALLOCATION
# ==================================================
def get_theory_room(cls, day, start, dur):
    # 1. Explicit lock from Room View
    if cls in st.session_state.CLASS_ROOM_LOCK:
        return st.session_state.CLASS_ROOM_LOCK[cls]

    # 2. Default locked classes (first 14)
    if cls in LOCKED_CLASSES:
        return PRIMARY_ROOMS[LOCKED_CLASSES.index(cls)]

    # 3. Excess classes -> free primary room only in continuous slots
    if not is_continuous(start, dur):
        return None

    for room in PRIMARY_ROOMS:
        if not room_clash(day, start, dur, room):
            return room

    return None


# ==================================================
# ADD ENTRY
# ==================================================
def add_entry(cls, sub, day, start):
    fac = SUB_FAC.get((cls, sub), "NA")
    dur = subject_duration(sub)

    if start + dur - 1 > 7:
        return "Invalid period span"

    # ---------- ROOM ----------
    if sub.endswith("LAB"):
        room = LAB_ROOMS.get(sub)

        if not room:
            return f"No room mapped for {sub}"

        if room_clash(day, start, dur, room):
            return f"Lab room clash: {room}"
    else:
        room = get_theory_room(cls, day, start, dur)

        if not room:
            return (
                "No theory room available. Excess classes can use rooms "
                "only in continuous slots (1-2, 3-4, 1-4, 5-7)."
            )

    # ---------- PERIOD CHECKS ----------
    for p in range(start, start + dur):
        if (fac, day, p) in FAC_BLOCKED:
            return f"{FAC_NAME.get(fac, fac)} unavailable"

        if busy("Class", cls, day, p):
            return "Class clash"

        if busy("Faculty", fac, day, p):
            existing = [
                r
                for r in st.session_state.TT
                if r["Day"] == day and int(r["Period"]) == p
            ]

            if not any(
                is_bi_lab_pair(sub, r["Subject"])
                for r in existing
            ):
                return "Faculty clash"

        if room == "LIBRARY" and library_overflow(day, p):
            return "Library already used by 3 classes"

    # ---------- HOURS ----------
    used = sum(
        1
        for r in st.session_state.TT
        if r["Class"] == cls and r["Subject"] == sub
    )

    maxh = SUB_MAX_HOURS.get((cls, sub))

    if maxh is None or used + dur > maxh:
        return "Weekly hours exceeded"

    # ---------- INSERT INTO SUPABASE ----------
    for p in range(start, start + dur):
        db_entry = {
            "class_id": cls,
            "subject": sub,
            "faculty_id": fac,
            "day": day,
            "period": p,
            "room": room,
        }

        if not save_timetable_entry(db_entry):
            return "Could not save entry to Supabase"

        st.session_state.TT.append(
            {
                "Class": cls,
                "Subject": sub,
                "Faculty": fac,
                "Day": day,
                "Period": p,
                "Room": room,
            }
        )

    return None


# ==================================================
# AI SUPPORT - SUGGESTIONS ONLY
# ==================================================
def suggest_slots(cls, sub):
    fac = SUB_FAC.get((cls, sub))
    dur = subject_duration(sub)
    suggestions = []

    for d in DAYS:
        for p in PERIODS:
            if p + dur - 1 > 7:
                continue

            if any(
                busy("Class", cls, d, x)
                for x in range(p, p + dur)
            ):
                continue

            if any(
                (fac, d, x) in FAC_BLOCKED
                for x in range(p, p + dur)
            ):
                continue

            suggestions.append(f"{d} P{p}")

    return suggestions[:3]


# ==================================================
# UI
# ==================================================
st.title("Timetable Generative System – Department of BS&H - VIEW")
st.caption("☁️ All master data and timetable records are stored in Supabase.")

c1, c2 = st.columns(2)

with c1:
    st.subheader("➕ Add Entry")

    with st.form("add"):
        cls = st.selectbox("Class", CLASSES)

        subs = (
            teaching[teaching["Class_ID"] == cls]["Subject_ID"]
            .dropna()
            .unique()
            .tolist()
        )

        if not subs:
            st.warning("No subjects found for this class.")
            sub = None
        else:
            sub = st.selectbox("Subject", subs)

        day = st.selectbox("Day", DAYS)
        start = st.selectbox("Start Period", PERIODS)

        if st.form_submit_button("ADD") and sub:
            err = add_entry(cls, sub, day, start)

            if err:
                st.warning(err)
            else:
                st.success("Added and saved to Supabase.")

        if sub:
            sugg = suggest_slots(cls, sub)
            if sugg:
                st.info("Suggested slots: " + ", ".join(sugg))

with c2:
    st.subheader("❌ Delete Entry")

    with st.form("del"):
        dcls = st.selectbox("Class", CLASSES, key="dcls")
        dday = st.selectbox("Day", DAYS, key="dday")
        dper = st.selectbox("Period", PERIODS, key="dper")

        if st.form_submit_button("DELETE"):
            matching = [
                r
                for r in st.session_state.TT
                if r["Class"] == dcls
                and r["Day"] == dday
                and int(r["Period"]) == int(dper)
            ]

            if not matching:
                st.warning("No timetable entry found.")
            elif matching[0]["Subject"] == "WEEKLY TEST":
                st.warning("Weekly Test cannot be deleted from this screen.")
            elif delete_timetable_entry(dcls, dday, dper):
                st.session_state.TT = [
                    r
                    for r in st.session_state.TT
                    if not (
                        r["Class"] == dcls
                        and r["Day"] == dday
                        and int(r["Period"]) == int(dper)
                    )
                ]
                st.success("Deleted from Supabase.")

df = pd.DataFrame(st.session_state.TT)

st.markdown("---")
st.info(f"📌 Pending load → {pending_load_row(cls)}")

# ==================================================
# GRID
# ==================================================
def grid(data, label):
    g = pd.DataFrame("", index=DAYS, columns=PERIODS)

    for _, r in data.iterrows():
        g.loc[r["Day"], int(r["Period"])] = label(r)

    return g


def safe_sheet_name(name, prefix="", max_len=31):
    bad = ["\\", "/", "*", "?", "[", "]"]

    for ch in bad:
        name = str(name).replace(ch, "_")

    return f"{prefix}{name}"[:max_len]


def faculty_grid_with_availability(data, faculty_id):
    g = pd.DataFrame("", index=DAYS, columns=PERIODS)
    style = pd.DataFrame("", index=DAYS, columns=PERIODS)

    for _, r in data.iterrows():
        g.loc[r["Day"], int(r["Period"])] = r["Class"]

    for day in DAYS:
        for p in PERIODS:
            if (faculty_id, day, p) in FAC_BLOCKED:
                style.loc[day, p] = "background-color: #ffcccc"

    return g.style.apply(lambda _: style, axis=None)


# ==================================================
# FOUR VIEWS
# ==================================================
tab1, tab2, tab3, tab4 = st.tabs(
    ["📘 Class View", "👨‍🏫 Faculty View", "🧪 Lab View", "🏫 Room View"]
)

with tab1:
    cls_v = st.selectbox("Class", CLASSES, key="cv")
    cdf = df[df["Class"] == cls_v]

    st.dataframe(
        grid(
            cdf,
            lambda r: (
                f'{r["Subject"]} | CLASS COORDINATOR'
                if r["Faculty"] == WEEKLY_TEST_FACULTY
                else f'{r["Subject"]} | {FAC_NAME.get(r["Faculty"], r["Faculty"])}'
            ),
        ),
        use_container_width=True,
    )

with tab2:
    fname = st.selectbox("Faculty", sorted(FAC_NAME.values()))
    fid = [k for k, v in FAC_NAME.items() if v == fname][0]

    fdf = df[df["Faculty"] == fid]

    st.dataframe(
        faculty_grid_with_availability(fdf, fid),
        use_container_width=True,
    )

    st.caption("🔴 Red cells indicate faculty unavailable slots")

with tab3:
    lab = st.selectbox(
        "Lab",
        sorted(labs_df["Lab_Subject"].dropna().unique()),
    )

    related_labs = [
        b
        for pair in BI_LABS
        if lab in pair
        for b in pair
    ]

    ldf = df[df["Subject"].isin([lab] + related_labs)]

    st.dataframe(
        grid(
            ldf,
            lambda r: f'{r["Class"]} | {FAC_NAME.get(r["Faculty"], r["Faculty"])}',
        ),
        use_container_width=True,
    )

with tab4:
    st.subheader("Theory Room Planning")

    mirror_cls = st.radio(
        "Select Class (Theory Only)",
        CLASSES,
        horizontal=True,
    )

    room = st.radio(
        "Select Room (Primary Rooms)",
        PRIMARY_ROOMS,
        horizontal=True,
    )

    locked_room = st.session_state.CLASS_ROOM_LOCK.get(mirror_cls)

    if locked_room:
        st.info(f"🔒 {mirror_cls} is currently locked to room {locked_room}")
    else:
        st.warning(f"⚠️ {mirror_cls} is not locked to any room")

    c_lock, c_unlock = st.columns(2)

    with c_lock:
        if st.button("🔒 Lock this Class to this Room"):
            if lock_class_to_room(mirror_cls, room):
                st.session_state.CLASS_ROOM_LOCK[mirror_cls] = room
                st.success(f"{mirror_cls} locked to {room}")

    with c_unlock:
        if locked_room and st.button("🔓 Unlock this Class"):
            if unlock_class_room(mirror_cls):
                del st.session_state.CLASS_ROOM_LOCK[mirror_cls]
                st.success(f"{mirror_cls} unlocked from {locked_room}")

    st.divider()

    mirror = df[
        (df["Class"] == mirror_cls)
        & (~df["Subject"].fillna("").astype(str).str.endswith("LAB"))
        & (~df["Subject"].isin(EXCLUDE_THEORY_ROOM))
    ]

    st.dataframe(
        grid(
            mirror,
            lambda r: (
                f'{r["Class"]} | {r["Subject"]} | '
                f'{FAC_NAME.get(r["Faculty"], r["Faculty"])}'
            ),
        ),
        use_container_width=True,
    )

    st.caption(
        "📌 Policy: Locked classes always use their locked room. "
        "Excess classes may occupy free rooms only in continuous slots "
        "(1–2, 3–4, 1–4, 5–7)."
    )

# ==================================================
# DOWNLOAD EXCEL
# ==================================================
def create_excel():
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:

        # CLASS TIMETABLES
        for class_name in df["Class"].dropna().unique():
            tt = grid(
                df[df["Class"] == class_name],
                lambda r: f'{r["Subject"]}\n{r["Faculty"]}',
            )
            tt.to_excel(
                writer,
                sheet_name=safe_sheet_name(class_name, "CLASS_"),
            )

        # FACULTY TIMETABLES
        for fac in df["Faculty"].dropna().unique():
            tt = grid(
                df[df["Faculty"] == fac],
                lambda r: r["Class"],
            )
            tt.to_excel(
                writer,
                sheet_name=safe_sheet_name(fac, "FAC_"),
            )

        # LAB TIMETABLES
        for lab_name in labs_df["Lab_Subject"].dropna().unique():
            tt = grid(
                df[df["Subject"] == lab_name],
                lambda r: r["Class"],
            )
            tt.to_excel(
                writer,
                sheet_name=safe_sheet_name(lab_name, "LAB_"),
            )

        # ROOM TIMETABLES
        for room_name in df["Room"].dropna().unique():
            if str(room_name).strip() == "":
                continue

            tt = grid(
                df[df["Room"] == room_name],
                lambda r: r["Class"],
            )
            tt.to_excel(
                writer,
                sheet_name=safe_sheet_name(room_name, "ROOM_"),
            )

    output.seek(0)
    return output.getvalue()


st.markdown("---")

if st.button("🔄 Refresh from Supabase"):
    st.session_state.pop("TT", None)
    st.session_state.pop("CLASS_ROOM_LOCK", None)
    st.rerun()

excel_data = create_excel()

st.download_button(
    "📥 Download Excel",
    data=excel_data,
    file_name="Timetable.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
