import io
import os
import zlib
import pandas as pd
from nicegui import app, ui
from supabase import Client, create_client
from room_allocator import allocate_rooms, update_room_assignments_supabase

# ==================================================
# RENDER & SUPABASE CONNECTION SETUP
# ==================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_KEY")

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
    "MONDAY": "Monday",
    "TUESDAY": "Tuesday",
    "WEDNESDAY": "Wednesday",
    "THURSDAY": "Thursday",
    "FRIDAY": "Friday",
    "SATURDAY": "Saturday",
}

TWO_PERIOD_SUBS = {"Makers", "EWS", "AIT", "DTI", "NSS", "HWYS"}
THREE_PERIOD_SUBS = {"BEEE(ECE) Lab", "BEEE(EEE) Lab", "CHE Lab", "PHY Lab", "CP Lab", "DLD Lab"}
EXCLUDE_THEORY_ROOM = {"DTI", "Makers Lab", "EGP", "EWS"}

BI_LABS = [
    {"EC LAB", "EP LAB"},
    {"EP LAB", "NAS LAB"},
]

VALID_2_PERIOD_STARTS = {1, 3, 5, 6}
VALID_3_PERIOD_STARTS = {1, 2, 5}

WEEKLY_TEST_FACULTY = "CLASS COORDINATOR"

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


# ==================================================
# HELPERS & COLOR GENERATORS
# ==================================================
def clean(x):
    if pd.isna(x):
        return ""
    return str(x).strip()


def get_entity_style(entity_name):
    """
    Generates inline CSS styling for subject/class cell cards.
    """
    if not entity_name or entity_name == "UNAVAILABLE":
        return "background-color: #fee2e2; border-left: 4px solid #ef4444; color: #991b1b; padding: 4px 6px; border-radius: 4px; font-weight: 600;"

    clean_str = clean(entity_name).split("\n")[0].split("(")[0].strip().upper()
    
    # Hash-based Hue generator
    hash_val = zlib.crc32(clean_str.encode("utf-8"))
    hue = hash_val % 360

    bg_color = f"hsla({hue}, 75%, 93%, 0.95)"
    border_color = f"hsl({hue}, 70%, 40%)"
    text_color = f"hsl({hue}, 85%, 20%)"

    return f"background-color: {bg_color}; border-left: 4px solid {border_color}; color: {text_color}; padding: 4px 6px; border-radius: 4px; font-weight: 600;"


def fetch_table(table_name):
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


def normalize_columns(df):
    col_map = {
        "class_id": "Class_ID",
        "subject_id": "Subject_ID",
        "faculty_id": "Faculty_ID",
        "faculty_name": "Faculty_Name",
        "lab_subject": "Lab_Subject",
        "hours": "Hours",
        "day": "Day",
        "period": "Period",
        "room": "Room",
    }
    df = df.rename(columns={c: col_map.get(c.lower(), c) for c in df.columns})
    return df


def fetch_master_data():
    try:
        faculty = normalize_columns(fetch_table(TABLE_FACULTY))
        subjects = normalize_columns(fetch_table(TABLE_SUBJECTS))
        classes_df = normalize_columns(fetch_table(TABLE_CLASSES))
        teaching = normalize_columns(fetch_table(TABLE_TEACHING))
        fac_avail = normalize_columns(fetch_table(TABLE_FAC_AVAIL))
        labs_df = normalize_columns(fetch_table(TABLE_LABS))
        rooms_df = normalize_columns(fetch_table(TABLE_ROOMS))
        return faculty, subjects, classes_df, teaching, fac_avail, labs_df, rooms_df
    except Exception as e:
        ui.notify(f"Could not read master data from Supabase: {e}", type="negative")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()


def load_timetable():
    try:
        rows = fetch_table(TABLE_TIMETABLE)
        if rows.empty:
            return []

        formatted_rows = []
        for r in rows.to_dict("records"):
            formatted_rows.append(
                {
                    "Class": clean(r.get("class_id", r.get("Class", ""))),
                    "Subject": clean(r.get("subject", r.get("Subject", ""))),
                    "Faculty": clean(r.get("faculty_id", r.get("Faculty", ""))),
                    "Day": r.get("day", r.get("Day", "")),
                    "Period": int(r.get("period", r.get("Period", 0))),
                    "Room": clean(r.get("room", r.get("Room", ""))),
                }
            )
        return formatted_rows
    except Exception as e:
        ui.notify(f"Could not load timetable from Supabase: {e}", type="negative")
        return []


def save_timetable_entry(entry):
    try:
        supabase.table(TABLE_TIMETABLE).insert(entry).execute()
    except Exception as e:
        ui.notify(f"Could not save timetable entry to Supabase: {e}", type="negative")
        return False
    return True


def delete_timetable_entry(cls, day, period):
    try:
        supabase.table(TABLE_TIMETABLE).delete().match(
            {"class_id": cls, "day": day, "period": int(period)}
        ).execute()
        return True
    except Exception as e:
        ui.notify(f"Could not delete timetable entry from Supabase: {e}", type="negative")
        return False


def subject_duration(sub):
    if clean(sub).upper() == "WEEKLY TEST":
        return 2
    if "LAB" in str(sub).upper():
        return 3
    if sub in THREE_PERIOD_SUBS:
        return 3
    if sub in TWO_PERIOD_SUBS:
        return 2
    return 1


def is_valid_start_slot(sub, start):
    dur = subject_duration(sub)
    if dur == 2 and start not in VALID_2_PERIOD_STARTS:
        return False, "2-period subjects must start at Period 1, 3, or 5."
    if dur == 3 and start not in VALID_3_PERIOD_STARTS:
        return False, "3-period subjects/labs must start at Period 1 or 5."
    return True, ""


# ==================================================
# INITIAL DATA LOADING
# ==================================================
faculty, subjects, classes_df, teaching, fac_avail, labs_df, rooms_df = fetch_master_data()

for data in [faculty, subjects, classes_df, teaching, fac_avail, labs_df, rooms_df]:
    data.columns = [str(c).strip() for c in data.columns]
    for c in data.columns:
        if data[c].dtype == object:
            data[c] = data[c].apply(clean)

teaching_cols_upper = {c.upper(): c for c in teaching.columns}
if "CLASS_ID" in teaching_cols_upper:
    teaching.rename(columns={teaching_cols_upper["CLASS_ID"]: "Class_ID"}, inplace=True)
if "SUBJECT_ID" in teaching_cols_upper:
    teaching.rename(columns={teaching_cols_upper["SUBJECT_ID"]: "Subject_ID"}, inplace=True)
if "FACULTY_ID" in teaching_cols_upper:
    teaching.rename(columns={teaching_cols_upper["FACULTY_ID"]: "Faculty_ID"}, inplace=True)
if "HOURS" in teaching_cols_upper:
    teaching.rename(columns={teaching_cols_upper["HOURS"]: "Hours"}, inplace=True)

fac_id_col = next((c for c in faculty.columns if c.lower() == "faculty_id"), None)
fac_name_col = next((c for c in faculty.columns if c.lower() == "faculty_name"), None)

FAC_NAME = {}
FAC_OPTIONS = {}
if fac_id_col and fac_name_col:
    for _, r in faculty.iterrows():
        fid = clean(r[fac_id_col]).upper()
        fname = clean(r[fac_name_col])
        if fid and fname:
            FAC_NAME[fid] = fname
            FAC_OPTIONS[fid] = f"{fname} ({fid})"

SUB_FAC = (
    {
        (clean(r.Class_ID).upper(), clean(r.Subject_ID).upper()): clean(r.Faculty_ID).upper()
        for _, r in teaching.iterrows()
    }
    if {"Class_ID", "Subject_ID", "Faculty_ID"}.issubset(teaching.columns)
    else {}
)

SUB_MAX_HOURS = {}
if {"Class_ID", "Subject_ID", "Hours"}.issubset(teaching.columns):
    for _, r in teaching.iterrows():
        c_id = clean(r["Class_ID"]).upper()
        s_id = clean(r["Subject_ID"]).upper()
        try:
            SUB_MAX_HOURS[(c_id, s_id)] = int(r["Hours"]) if pd.notna(r["Hours"]) else 0
        except (ValueError, TypeError):
            SUB_MAX_HOURS[(c_id, s_id)] = 0

FAC_BLOCKED = set()
if not fac_avail.empty:
    avail_cols_upper = {c.upper(): c for c in fac_avail.columns}
    f_col = avail_cols_upper.get("FACULTY_ID")
    d_col = avail_cols_upper.get("DAY")
    p_col = avail_cols_upper.get("PERIOD")

    if f_col and d_col and p_col:
        for _, r in fac_avail.iterrows():
            fac_id = clean(r[f_col]).upper()
            raw_day = clean(r[d_col]).upper()
            mapped_day = DAY_MAP.get(raw_day, raw_day.title())
            raw_p = r[p_col]

            if fac_id and mapped_day in DAYS and pd.notna(raw_p):
                try:
                    FAC_BLOCKED.add((fac_id, mapped_day, int(raw_p)))
                except ValueError:
                    pass

LAB_ROOMS = (
    dict(zip(labs_df["Lab_Subject"], labs_df["Room"]))
    if "Lab_Subject" in labs_df.columns and "Room" in labs_df.columns
    else {}
)

ROOM_COLS = [c for c in rooms_df.columns if c.upper().startswith("ROOM")]
ROOM_COL = ROOM_COLS[0] if ROOM_COLS else None
ALL_ROOMS = rooms_df[ROOM_COL].dropna().astype(str).str.strip().unique().tolist() if ROOM_COL else []
PRIMARY_ROOMS = ALL_ROOMS[:14]

CLASSES = (
    sorted(classes_df["Class_ID"].dropna().astype(str).str.strip().unique().tolist())
    if "Class_ID" in classes_df.columns
    else []
)
LOCKED_CLASSES = CLASSES[:14]

TT_DATA = load_timetable()


# ==================================================
# CORE LOGIC & METRICS
# ==================================================
def busy(key, val, day, p):
    key_alt = "class_id" if key == "Class" else "faculty_id" if key == "Faculty" else key.lower()
    return any(
        (clean(r.get(key, "")).upper() == clean(val).upper() or clean(r.get(key_alt, "")).upper() == clean(val).upper())
        and r.get("Day", r.get("day")) == day
        and int(r.get("Period", r.get("period", 0))) == int(p)
        for r in TT_DATA
    )


def calculate_class_stats(cls):
    """Calculates class work completion metrics for the given class."""
    cls_col = next((c for c in teaching.columns if c.lower() == "class_id"), None)
    sub_col = next((c for c in teaching.columns if c.lower() == "subject_id"), None)
    hrs_col = next((c for c in teaching.columns if c.lower() == "hours"), None)

    if not cls_col or not sub_col:
        return 0, 0, []

    cls_mask = teaching[cls_col].astype(str).str.strip().str.upper() == str(cls).strip().upper()
    class_teaching_df = teaching[cls_mask]

    total_target = 0
    total_scheduled = 0
    sub_progress = []

    for _, row in class_teaching_df.iterrows():
        s = clean(row[sub_col])
        if not s:
            continue
        try:
            target = int(row[hrs_col]) if hrs_col and pd.notna(row[hrs_col]) else 0
        except (ValueError, TypeError):
            target = 0

        used = sum(
            1
            for r in TT_DATA
            if clean(r.get("Class")).upper() == clean(cls).upper()
            and clean(r.get("Subject")).upper() == clean(s).upper()
        )

        total_target += target
        total_scheduled += used
        sub_progress.append({"subject": s, "used": used, "target": target})

    return total_scheduled, total_target, sub_progress


def is_bi_lab_pair(sub1, sub2):
    return any({clean(sub1).upper(), clean(sub2).upper()} == {clean(s).upper() for s in b} for b in BI_LABS)


def library_overflow(day, period):
    used = {
        r.get("Class")
        for r in TT_DATA
        if r.get("Room") == "LIBRARY"
        and r.get("Day") == day
        and int(r.get("Period", 0)) == int(period)
    }
    return len(used) >= 3


def room_clash(day, start, dur, room):
    return any(
        clean(r.get("Room")).upper() == clean(room).upper()
        and r.get("Day") == day
        and int(r.get("Period", 0)) in range(start, start + dur)
        for r in TT_DATA
    )


def get_theory_room(cls, day, start, dur):
    if cls in LOCKED_CLASSES:
        return PRIMARY_ROOMS[LOCKED_CLASSES.index(cls)]

    for room in PRIMARY_ROOMS:
        if not room_clash(day, start, dur, room):
            return room
    return None


def suggest_slots(cls, sub, custom_fac=None):
    fac = custom_fac or SUB_FAC.get((clean(cls).upper(), clean(sub).upper()))
    dur = subject_duration(sub)
    suggestions = []

    for d in DAYS:
        for p in PERIODS:
            if p + dur - 1 > 7:
                continue
            valid_slot, _ = is_valid_start_slot(sub, p)
            if not valid_slot:
                continue
            if any(busy("Class", cls, d, x) for x in range(p, p + dur)):
                continue
            if fac and fac != WEEKLY_TEST_FACULTY and any((fac, d, x) in FAC_BLOCKED for x in range(p, p + dur)):
                continue
            suggestions.append((d, p))
    return suggestions[:6]


def add_entry(cls, sub, day, start, override_fac=None):
    valid_slot, slot_err = is_valid_start_slot(sub, start)
    if not valid_slot:
        return slot_err

    fac = override_fac or (WEEKLY_TEST_FACULTY if clean(sub).upper() == "WEEKLY TEST" else SUB_FAC.get((clean(cls).upper(), clean(sub).upper()), "NA"))
    dur = subject_duration(sub)

    if start + dur - 1 > 7:
        return "Invalid period span"

    if "LAB" in str(sub).upper():
        room = LAB_ROOMS.get(sub)
        if not room:
            return f"No room mapped for {sub}"
        if room_clash(day, start, dur, room):
            return f"Lab room clash: {room}"
    else:
        room = get_theory_room(cls, day, start, dur)
        if not room:
            return "No theory room available for this slot."

    for p in range(start, start + dur):
        if fac != WEEKLY_TEST_FACULTY and (fac, day, p) in FAC_BLOCKED:
            return f"{FAC_NAME.get(fac, fac)} unavailable"
        if busy("Class", cls, day, p):
            return "Class clash"
        if fac != WEEKLY_TEST_FACULTY and busy("Faculty", fac, day, p):
            existing = [r for r in TT_DATA if r.get("Day") == day and int(r.get("Period", 0)) == p]
            if not any(is_bi_lab_pair(sub, r.get("Subject")) for r in existing):
                return "Faculty clash"
        if room == "LIBRARY" and library_overflow(day, p):
            return "Library already used by 3 classes"

    used = sum(1 for r in TT_DATA if clean(r.get("Class")).upper() == clean(cls).upper() and clean(r.get("Subject")).upper() == clean(sub).upper())
    maxh = SUB_MAX_HOURS.get((clean(cls).upper(), clean(sub).upper()))

    if maxh is not None and maxh > 0 and used + dur > maxh:
        return "Weekly hours exceeded"

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

        TT_DATA.append({
            "Class": cls,
            "Subject": sub,
            "Faculty": fac,
            "Day": day,
            "Period": p,
            "Room": room,
        })
    return None


# ==================================================
# EXCEL EXPORT
# ==================================================
def grid(data, label):
    g = pd.DataFrame("", index=DAYS, columns=PERIODS)
    for _, r in data.iterrows():
        if r["Day"] in DAYS and int(r["Period"]) in PERIODS:
            g.loc[r["Day"], int(r["Period"])] = label(r)
    return g


def safe_sheet_name(name, prefix="", max_len=31):
    for ch in ["\\", "/", "*", "?", "[", "]"]:
        name = str(name).replace(ch, "_")
    return f"{prefix}{name}"[:max_len]


def create_excel():
    output = io.BytesIO()
    df = pd.DataFrame(TT_DATA)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        if not df.empty and "Class" in df.columns:
            for class_name in df["Class"].dropna().unique():
                tt = grid(df[df["Class"] == class_name], lambda r: f'{r["Subject"]}\n{FAC_NAME.get(clean(r["Faculty"]).upper(), r["Faculty"])}')
                tt.to_excel(writer, sheet_name=safe_sheet_name(class_name, "CLASS_"))

            for fac in df["Faculty"].dropna().unique():
                tt = grid(df[df["Faculty"] == fac], lambda r: r["Class"])
                tt.to_excel(writer, sheet_name=safe_sheet_name(fac, "FAC_"))

            if "Lab_Subject" in labs_df.columns:
                for lab_name in labs_df["Lab_Subject"].dropna().unique():
                    tt = grid(df[df["Subject"] == lab_name], lambda r: r["Class"])
                    tt.to_excel(writer, sheet_name=safe_sheet_name(lab_name, "LAB_"))

            for room_name in df["Room"].dropna().unique():
                if str(room_name).strip():
                    tt = grid(df[df["Room"] == room_name], lambda r: r["Class"])
                    tt.to_excel(writer, sheet_name=safe_sheet_name(room_name, "ROOM_"))

    output.seek(0)
    return output.getvalue()


# ==================================================
# MAIN APPLICATION INTERFACE
# ==================================================
@ui.page("/")
def main_page():
    ui.add_head_html("""
        <style>
            .q-page { padding: 8px !important; }
            .dense-card { padding: 10px !important; }
            .q-table--dense td, .q-table--dense th { padding: 4px 6px !important; height: auto !important; }
            .cell-box { width: 100%; height: 100%; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; font-size: 11px; white-space: pre-line; line-height: 1.2; min-height: 42px; }
        </style>
    """)

    # ==================================================
    # AUTOMATIC ROOM ALLOCATION
    # ==================================================
    allocation_report = ui.column().classes("w-full mb-2")

    def auto_allocate_rooms():
        global TT_DATA, rooms_df, labs_df
        try:
            rooms_df = normalize_columns(fetch_table(TABLE_ROOMS))
            labs_df = normalize_columns(fetch_table(TABLE_LABS))

            allocated_df, metrics, failures = allocate_rooms(
                TT_DATA,
                rooms_df,
                labs_df,
                prefer_theory_rooms=True,
            )

            written = update_room_assignments_supabase(
                supabase,
                allocated_df,
                table_name=TABLE_TIMETABLE,
            )

            TT_DATA = load_timetable()
            refresh_views()

            allocation_report.clear()
            with allocation_report:
                with ui.card().classes("w-full dense-card shadow-sm border border-gray-200"):
                    ui.label("AUTOMATED ROOM ALLOCATION RESULT").classes("text-lg font-bold text-green-700")
                    ui.label(
                        f"Saved: {written} | Allocated: {metrics['allocated periods']}/{metrics['total timetable periods']} "
                        f"({metrics['allocation percentage']}%)"
                    ).classes("text-sm font-bold")
                    ui.label(
                        f"Theory room changes: {metrics['theory room changes per day/class']} | "
                        f"Rooms available: {metrics['rooms available']} | Rooms used: {metrics['rooms used']}"
                    ).classes("text-sm")

                    if failures:
                        ui.label(f"Lab allocation warnings: {len(failures)}").classes("text-sm text-red-700 font-bold")
                    if metrics["unallocated periods"]:
                        ui.label(
                            f"Unallocated timetable periods: {metrics['unallocated periods']}"
                        ).classes("text-sm text-red-700 font-bold")

                    report_df = allocated_df[[
                        "Day", "Period", "Room", "Class", "Subject", "Faculty", "Allocation"
                    ]].copy()
                    report_df = report_df.sort_values(["Day", "Period", "Room", "Class"]).reset_index(drop=True)
                    report_df["allocation_row"] = report_df.index.astype(str)
                    columns = [
                        {"name": c, "label": c.replace("_", " "), "field": c, "align": "left"}
                        for c in report_df.columns
                    ]
                    ui.table(
                        columns=columns,
                        rows=report_df.fillna("").to_dict("records"),
                        row_key="allocation_row",
                    ).props("dense flat bordered")

            ui.notify("Automatic room allocation completed and saved to Supabase.", type="positive")
        except Exception as e:
            ui.notify(f"Automatic room allocation failed: {e}", type="negative")

    # Top Header Toolbar
    with ui.row().classes("w-full items-center justify-between mb-2 bg-white p-3 rounded shadow-xs border"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("calendar_month", size="28px", color="primary")
            ui.label("Timetable Generative System – BS&H").classes("text-xl font-bold text-gray-800")
            ui.badge("Supabase Connected", color="green").classes("ml-2")
        
        with ui.row().classes("gap-2"):
            def refresh_grid():
                global TT_DATA
                TT_DATA = load_timetable()
                refresh_views()
                ui.notify("Refreshed data from Supabase.", type="info")

            def download_excel():
                content = create_excel()
                ui.download(content, "Timetable.xlsx")

            ui.button("Refresh", icon="refresh", on_click=refresh_grid).props("dense outline")
            ui.button("AUTO ROOM ALLOCATION", icon="meeting_room", on_click=auto_allocate_rooms).props("dense color=purple outline")
            ui.button("Export Excel", icon="download", on_click=download_excel).props("dense color=green")

    # Table renderer that renders custom HTML elements in Quasar table body
    def render_table_grid(data_df, formatter_fn, is_faculty=False, faculty_id="", target_cls=None, color_entity_key="Subject"):
        columns = [{"name": "Day", "label": "Day", "field": "Day", "align": "left", "sortable": False}]
        for p in PERIODS:
            columns.append({"name": f"P{p}", "label": f"P{p}", "field": f"P{p}", "align": "center", "sortable": False})

        rows = []

        for day in DAYS:
            row_dict = {"Day": day}
            for p in PERIODS:
                if is_faculty and (faculty_id.upper(), day, p) in FAC_BLOCKED:
                    style = get_entity_style("UNAVAILABLE")
                    row_dict[f"P{p}"] = f'<div style="{style}" class="cell-box">UNAVAILABLE</div>'
                else:
                    row_dict[f"P{p}"] = ""
            rows.append(row_dict)

        if not data_df.empty:
            for _, r in data_df.iterrows():
                d = r.get("Day")
                p = int(r.get("Period", 0))
                entity_value = r.get(color_entity_key, "")

                if d in DAYS and p in PERIODS:
                    val = formatter_fn(r)
                    style = get_entity_style(entity_value)
                    formatted_html = f'<div style="{style}" class="cell-box">{val}</div>'
                    
                    for row in rows:
                        if row["Day"] == d:
                            row[f"P{p}"] = formatted_html

        table = ui.table(columns=columns, rows=rows, row_key="Day").props("dense flat bordered").classes("w-full")

        # HTML slot injection for rendering colors inside table cells
        table.add_slot(
            "body-cell",
            r"""
            <q-td :props="props" @click="$parent.$emit('cell-click', {day: props.row.Day, col: props.col.name})">
                <div v-html="props.value"></div>
            </q-td>
            """,
        )

        def on_cell_click(e):
            day = e.args.get("day")
            col = e.args.get("col", "")
            if col.startswith("P"):
                p_num = int(col[1:])
                if target_cls:
                    del_cls.set_value(target_cls)
                del_day.set_value(day)
                del_per.set_value(p_num)
                ui.notify(f"Selected {day} Period {p_num} for entry deletion.", type="info")

        table.on("cell-click", on_cell_click)

    # Refresh triggers
    def refresh_views():
        update_class_view()
        update_faculty_view()
        update_lab_view()
        update_room_view()

    def update_subjects_dropdown(selected_cls):
        cls_mask = teaching["Class_ID"].astype(str).str.strip().str.upper() == str(selected_cls).strip().upper()
        subs = (
            teaching[cls_mask]["Subject_ID"].dropna().astype(str).str.strip().unique().tolist()
            if "Class_ID" in teaching.columns and "Subject_ID" in teaching.columns
            else []
        )
        if "WEEKLY TEST" not in [s.upper() for s in subs]:
            subs.append("WEEKLY TEST")

        add_sub.set_options(subs)
        if subs:
            add_sub.set_value(subs[0])
        update_faculty_override()

    def update_faculty_override():
        if add_cls.value and add_sub.value:
            mapped = SUB_FAC.get((clean(add_cls.value).upper(), clean(add_sub.value).upper()))
            if mapped and mapped in FAC_OPTIONS:
                add_fac_override.set_value(mapped)
            else:
                add_fac_override.set_value(None)
        update_suggestions()

    def handle_add():
        ov_fac = add_fac_override.value
        err = add_entry(add_cls.value, add_sub.value, add_day.value, int(add_start.value), override_fac=ov_fac)
        if err:
            ui.notify(err, type="warning")
        else:
            ui.notify("Added entry successfully.", type="positive")
            refresh_views()

    def handle_delete():
        global TT_DATA
        dcls = del_cls.value
        dday = del_day.value
        dper = del_per.value

        matching = [
            r for r in TT_DATA
            if clean(r.get("Class")).upper() == clean(dcls).upper()
            and r.get("Day") == dday
            and int(r.get("Period", 0)) == int(dper)
        ]

        if not matching:
            ui.notify("No entry found to delete.", type="warning")
        elif delete_timetable_entry(dcls, dday, dper):
            TT_DATA = [
                r for r in TT_DATA
                if not (
                    clean(r.get("Class")).upper() == clean(dcls).upper()
                    and r.get("Day") == dday
                    and int(r.get("Period", 0)) == int(dper)
                )
            ]
            ui.notify("Deleted entry successfully.", type="positive")
            refresh_views()

    # ==================================================
    # TOP SECTION: ADD AND DELETE FORMS
    # ==================================================
    with ui.row().classes("w-full gap-3 mb-2 items-stretch"):
        # Add Entry Card - Spans 8 Columns
        with ui.card().classes("w-8/12 dense-card shadow-sm border border-gray-200"):
            with ui.row().classes("items-center justify-between w-full mb-1"):
                ui.label("➕ Add Entry").classes("text-md font-bold text-blue-700")
                sugg_container = ui.row().classes("gap-1 items-center")

            with ui.grid(columns=6).classes("w-full gap-2 items-center"):
                add_cls = ui.select(
                    options=CLASSES,
                    label="Class",
                    value=CLASSES[0] if CLASSES else None,
                    on_change=lambda e: update_subjects_dropdown(e.value),
                ).props("dense outlined").classes("col-span-1")

                add_sub = ui.select(options=[], label="Subject").props("dense outlined").classes("col-span-1")
                
                add_fac_override = ui.select(
                    options=FAC_OPTIONS,
                    label="Faculty (Mapped/Override)",
                    with_input=True,
                ).props("dense outlined use-input").classes("col-span-2")

                add_day = ui.select(options=DAYS, label="Day", value=DAYS[0]).props("dense outlined").classes("col-span-1")
                add_start = ui.select(options=PERIODS, label="Start Period", value=PERIODS[0]).props("dense outlined").classes("col-span-1")

            with ui.row().classes("w-full justify-end mt-2"):
                ui.button("ADD ENTRY", icon="add_circle", on_click=handle_add).props("dense color=primary").classes("w-48")

            def update_suggestions():
                sugg_container.clear()
                if add_cls.value and add_sub.value:
                    sugg = suggest_slots(add_cls.value, add_sub.value, custom_fac=add_fac_override.value)
                    with sugg_container:
                        if sugg:
                            ui.label("Free Slots:").classes("text-xs text-gray-500 font-bold")
                            for s_day, s_p in sugg:
                                def set_slot(d=s_day, p=s_p):
                                    add_day.set_value(d)
                                    add_start.set_value(p)
                                ui.chip(f"{s_day[:3]} P{s_p}", on_click=set_slot).props("dense clickable color=blue-1 text-color=blue").classes("text-xs cursor-pointer")
                        else:
                            ui.label("No free slots").classes("text-xs text-red-400")

            add_sub.on_value_change(update_faculty_override)
            add_fac_override.on_value_change(update_suggestions)

        # Delete Entry Card - Spans 4 Columns
        with ui.card().classes("w-4/12 dense-card shadow-sm border border-gray-200"):
            with ui.row().classes("items-center justify-between w-full mb-1"):
                ui.label("❌ Delete Entry").classes("text-md font-bold text-red-700")
                ui.label("Tip: Click cell to auto-fill").classes("text-xs text-gray-400")

            with ui.grid(columns=3).classes("w-full gap-2 items-center"):
                del_cls = ui.select(options=CLASSES, label="Class", value=CLASSES[0] if CLASSES else None).props("dense outlined").classes("col-span-1")
                del_day = ui.select(options=DAYS, label="Day", value=DAYS[0]).props("dense outlined").classes("col-span-1")
                del_per = ui.select(options=PERIODS, label="Period", value=PERIODS[0]).props("dense outlined").classes("col-span-1")

            with ui.row().classes("w-full justify-end mt-2"):
                ui.button("DELETE ENTRY", icon="delete", on_click=handle_delete).props("dense color=negative").classes("w-full")

    # ==================================================
    # MAIN VIEWS (CLASS, FACULTY, LAB, ROOM)
    # ==================================================
    with ui.card().classes("w-full dense-card shadow-sm border border-gray-200"):
        with ui.tabs().classes("w-full dense text-primary") as tabs:
            t1 = ui.tab("📘 Class View")
            t2 = ui.tab("👨‍🏫 Faculty View")
            t3 = ui.tab("🧪 Lab View")
            t4 = ui.tab("🏫 Room View")

        with ui.tab_panels(tabs, value=t1).classes("w-full p-1"):
            # 1. CLASS VIEW WITH PROGRESS MONITOR
            with ui.tab_panel(t1).classes("p-0"):
                with ui.row().classes("items-center justify-between mb-2 w-full"):
                    cv_select = ui.select(options=CLASSES, label="Select Class", value=CLASSES[0] if CLASSES else None).props("dense outlined").classes("w-56")
                    progress_info_container = ui.row().classes("items-center gap-2")

                class_progress_bar_container = ui.column().classes("w-full mb-2")
                class_container = ui.element("div").classes("w-full")

                def update_class_view():
                    class_container.clear()
                    progress_info_container.clear()
                    class_progress_bar_container.clear()

                    cls = cv_select.value
                    df = pd.DataFrame(TT_DATA)
                    cdf = df[df["Class"].astype(str).str.strip().str.upper() == str(cls).strip().upper()] if "Class" in df.columns and not df.empty else pd.DataFrame()

                    # Render Class Work Load Progress Tracker
                    sched, target, sub_progress = calculate_class_stats(cls)
                    percent = (sched / target * 100) if target > 0 else 0.0

                    with class_progress_bar_container:
                        with ui.row().classes("w-full justify-between items-center mb-1"):
                            ui.label(f"📊 {cls} Class Work Completion: {sched} / {target} Total Hours").classes("text-xs font-bold text-gray-700")
                            ui.label(f"{percent:.1f}% Scheduled").classes("text-xs font-bold text-blue-700")
                        
                        ui.linear_progress(value=percent / 100.0, show_value=False).props("stripe rounded size=8px color=primary")

                        with ui.row().classes("w-full gap-1 mt-1 wrap"):
                            for sp in sub_progress:
                                is_done = sp["used"] >= sp["target"] and sp["target"] > 0
                                bg = "bg-green-100 text-green-800 border-green-300" if is_done else "bg-amber-50 text-amber-900 border-amber-200"
                                with ui.card().classes(f"p-1 border text-xs {bg} shadow-2xs"):
                                    ui.label(f"{sp['subject']}: {sp['used']}/{sp['target']} hrs").classes("font-mono text-xs")

                    # Render Grid Table with Subject Color Coding
                    with class_container:
                        render_table_grid(
                            cdf,
                            lambda r: f'{r["Subject"]}\n({FAC_NAME.get(clean(r["Faculty"]).upper(), r["Faculty"])})',
                            target_cls=cls,
                            color_entity_key="Subject",  # Subject color coding
                        )

                cv_select.on_value_change(update_class_view)

            # 2. FACULTY VIEW (Color Code by Class)
            with ui.tab_panel(t2).classes("p-0"):
                fac_name_to_id = {v: k for k, v in FAC_NAME.items()}
                sorted_fac_names = sorted(list(fac_name_to_id.keys()))

                with ui.row().classes("items-center mb-2"):
                    fv_select = ui.select(
                        options=sorted_fac_names,
                        label="Search Faculty Name",
                        value=sorted_fac_names[0] if sorted_fac_names else None,
                        with_input=True,
                    ).props("dense outlined use-input").classes("w-80")

                fac_container = ui.element("div").classes("w-full")

                def update_faculty_view():
                    fac_container.clear()
                    fname = fv_select.value
                    if fname and fname in fac_name_to_id:
                        fid = fac_name_to_id[fname]
                        df = pd.DataFrame(TT_DATA)
                        fdf = df[df["Faculty"].astype(str).str.upper() == fid.upper()] if "Faculty" in df.columns and not df.empty else pd.DataFrame()
                        with fac_container:
                            render_table_grid(
                                fdf,
                                lambda r: f'{r["Class"]}\n[{r["Subject"]}]',
                                is_faculty=True,
                                faculty_id=fid,
                                color_entity_key="Class",  # Class color coding
                            )

                fv_select.on_value_change(update_faculty_view)

            # 3. LAB VIEW
            with ui.tab_panel(t3).classes("p-0"):
                lab_list = sorted(labs_df["Lab_Subject"].dropna().unique()) if "Lab_Subject" in labs_df.columns else []
                with ui.row().classes("items-center mb-2"):
                    lab_select = ui.select(options=lab_list, label="Select Lab", value=lab_list[0] if lab_list else None).props("dense outlined").classes("w-56")
                lab_container = ui.element("div").classes("w-full")

                def update_lab_view():
                    lab_container.clear()
                    lab = lab_select.value
                    if lab:
                        related_labs = [
                            b for pair in BI_LABS
                            if any(lab.lower() in p.lower() or p.lower() in lab.lower() for p in pair)
                            for b in pair
                        ]
                        all_target_labs = list(set([lab] + related_labs))
                        df = pd.DataFrame(TT_DATA)
                        ldf = df[df["Subject"].apply(lambda s: any(t.lower() in str(s).lower() for t in all_target_labs))] if not df.empty and "Subject" in df.columns else pd.DataFrame()

                        with lab_container:
                            if not ldf.empty:
                                render_table_grid(
                                    ldf,
                                    lambda r: f'{r["Class"]}\n{r["Subject"]}',
                                    color_entity_key="Subject",
                                )
                            else:
                                ui.label("No scheduled classes found for this lab.").classes("text-xs text-amber-700 p-2")

                lab_select.on_value_change(update_lab_view)

            # 4. ROOM VIEW
            with ui.tab_panel(t4).classes("p-0"):
                with ui.row().classes("items-center mb-2"):
                    room_radio = ui.radio(CLASSES, value=CLASSES[0] if CLASSES else None).props("inline dense")
                room_container = ui.element("div").classes("w-full")

                def update_room_view():
                    room_container.clear()
                    mirror_cls = room_radio.value
                    df = pd.DataFrame(TT_DATA)
                    mirror = (
                        df[
                            (df["Class"].astype(str).str.strip().str.upper() == str(mirror_cls).strip().upper())
                            & (~df["Subject"].fillna("").astype(str).str.contains("LAB", case=False))
                            & (~df["Subject"].isin(EXCLUDE_THEORY_ROOM))
                        ]
                        if not df.empty and "Class" in df.columns
                        else pd.DataFrame()
                    )

                    with room_container:
                        if not mirror.empty:
                            render_table_grid(
                                mirror,
                                lambda r: f'{r["Class"]}\n{r["Subject"]}',
                                target_cls=mirror_cls,
                                color_entity_key="Subject",
                            )

                room_radio.on_value_change(update_room_view)

    # Initial view load
    if CLASSES:
        update_subjects_dropdown(CLASSES[0])
    refresh_views()


# ==================================================
# APPLICATION STARTUP
# ==================================================
if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.environ.get("PORT", 8080))
    ui.run(host="0.0.0.0", port=port, title="Timetable Generative System")