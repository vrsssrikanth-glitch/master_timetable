import pandas as pd
from collections import defaultdict

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
PERIODS = [1, 2, 3, 4, 5, 6, 7]
THEORY_SHIFT_STARTS = {1, 3, 5}


def clean(x):
    if pd.isna(x):
        return ""
    return str(x).strip()


def normalise_rooms(rooms_df: pd.DataFrame):
    """Return ordered room records using all rooms available in Supabase."""
    if rooms_df is None or rooms_df.empty:
        return []

    df = rooms_df.copy()
    cols = {str(c).strip().lower(): c for c in df.columns}
    room_col = next((cols[k] for k in ("room", "room_no", "room_number", "room_id") if k in cols), None)
    type_col = next((cols[k] for k in ("type", "room_type", "category") if k in cols), None)

    if not room_col:
        raise ValueError("rooms table must contain Room / Room_No / Room_Number / Room_ID")

    rooms = []
    for _, r in df.iterrows():
        name = clean(r.get(room_col))
        if not name:
            continue
        typ = clean(r.get(type_col)).lower() if type_col else ""
        rooms.append({"room": name, "type": typ})

    # Keep database order; remove duplicates.
    seen = set()
    out = []
    for r in rooms:
        key = r["room"].upper()
        if key not in seen:
            out.append(r)
            seen.add(key)
    return out


def is_lab_subject(subject: str):
    return "LAB" in clean(subject).upper()


def lab_room_for(subject: str, labs_df: pd.DataFrame):
    if labs_df is None or labs_df.empty:
        return None
    cols = {str(c).strip().lower(): c for c in labs_df.columns}
    sub_col = next((cols[k] for k in ("lab_subject", "subject", "lab") if k in cols), None)
    room_col = next((cols[k] for k in ("room", "room_no", "room_number") if k in cols), None)
    if not sub_col or not room_col:
        return None

    target = clean(subject).upper()
    for _, r in labs_df.iterrows():
        s = clean(r.get(sub_col)).upper()
        if s == target:
            return clean(r.get(room_col)) or None
    return None


def shift_block(period: int):
    """Theory room may change only at P1, P3 or P5."""
    p = int(period)
    if p in (1, 2):
        return 1
    if p in (3, 4):
        return 3
    if p in (5, 6, 7):
        return 5
    return None


def build_schedule(tt_data):
    """Convert timetable rows to a convenient structure grouped by day/period/class."""
    rows = []
    for r in tt_data:
        day = clean(r.get("Day", r.get("day")))
        cls = clean(r.get("Class", r.get("class_id")))
        subject = clean(r.get("Subject", r.get("subject")))
        faculty = clean(r.get("Faculty", r.get("faculty_id")))
        try:
            p = int(r.get("Period", r.get("period", 0)))
        except (TypeError, ValueError):
            continue
        if day in DAYS and p in PERIODS and cls and subject:
            rows.append({
                "Class": cls,
                "Subject": subject,
                "Faculty": faculty,
                "Day": day,
                "Period": p,
                "Old_Room": clean(r.get("Room", r.get("room"))),
            })
    return rows


def occupied(alloc, day, period, room):
    return any(
        x["Day"] == day and x["Period"] == period and x["Room"].upper() == room.upper()
        for x in alloc
    )


def class_busy(alloc, cls, day, period):
    return any(
        x["Day"] == day and x["Period"] == period and x["Class"].upper() == cls.upper()
        for x in alloc
    )


def room_is_lab_type(room_record):
    typ = room_record.get("type", "")
    return any(k in typ for k in ("lab", "laboratory", "computer", "workshop"))


def allocate_rooms(tt_data, rooms_df, labs_df=None, prefer_theory_rooms=True):
    """
    Automatic room allocation from timetable output.

    Rules:
      1. Existing lab mappings are fixed.
      2. Theory rooms may change only at P1/P3/P5.
      3. A class keeps one theory room inside each shift block (P1-2, P3-4, P5-7).
      4. A lab room is reusable for theory before/after the lab when it is free.
      5. Existing room is retained when valid, which minimizes unnecessary movement.
      6. All rooms in the Supabase rooms table are candidates.
      7. If a clash makes a solution impossible, the row is reported as UNALLOCATED.
    """
    room_records = normalise_rooms(rooms_df)
    if not room_records:
        raise ValueError("No rooms found in Supabase rooms table")

    all_rooms = [r["room"] for r in room_records]
    theory_rooms = [r["room"] for r in room_records if not room_is_lab_type(r)]
    fallback_rooms = [r["room"] for r in room_records if room_is_lab_type(r)]
    if not theory_rooms:
        theory_rooms = all_rooms[:]

    rows = build_schedule(tt_data)
    by_slot = defaultdict(list)
    for row in rows:
        by_slot[(row["Day"], row["Period"])].append(row)

    # First allocate labs because their room is fixed and therefore creates hard constraints.
    alloc = []
    lab_unallocated = []
    for row in sorted(rows, key=lambda x: (DAYS.index(x["Day"]), x["Period"], x["Class"], x["Subject"])):
        if not is_lab_subject(row["Subject"]):
            continue
        room = lab_room_for(row["Subject"], labs_df)
        if not room:
            lab_unallocated.append({**row, "Reason": "No lab room mapping"})
            continue
        if room.upper() not in {x.upper() for x in all_rooms}:
            lab_unallocated.append({**row, "Reason": f"Mapped lab room {room} not present in rooms table"})
            continue
        if occupied(alloc, row["Day"], row["Period"], room):
            lab_unallocated.append({**row, "Reason": f"Lab room clash in {room}"})
            continue
        item = {**row, "Room": room, "Allocation": "LAB-FIXED", "Shift_Block": None}
        alloc.append(item)

    # Candidate theory blocks.
    theory_rows = [r for r in rows if not is_lab_subject(r["Subject"])]
    # Group a class's theory periods by the allowed room-shift block.
    groups = defaultdict(list)
    for row in theory_rows:
        groups[(row["Day"], row["Class"], shift_block(row["Period"]))].append(row)

    # Schedule harder groups first: more periods, then earlier periods.
    group_items = sorted(
        groups.items(),
        key=lambda kv: (-len(kv[1]), DAYS.index(kv[0][0]), kv[0][2], kv[0][1])
    )

    last_room = {}  # (class) -> previous assigned theory room
    block_room = {}  # (day, class, shift_block) -> room

    for (day, cls, block), group in group_items:
        periods = sorted({int(r["Period"]) for r in group})
        old_rooms = [clean(r["Old_Room"]) for r in group if clean(r["Old_Room"])]
        old_room = old_rooms[0] if old_rooms and len(set(x.upper() for x in old_rooms)) == 1 else ""

        # Candidate ranking: keep old room, then previous block room, then primary/theory rooms,
        # then lab-type fallback rooms. A free room is mandatory for every period in this group.
        candidates = []
        for room in theory_rooms + [r for r in fallback_rooms if r not in theory_rooms]:
            if room.upper() in {c[1].upper() for c in candidates}:
                continue
            if all(not occupied(alloc, day, p, room) for p in periods):
                score = 0
                if old_room and room.upper() == old_room.upper():
                    score += 10000
                if last_room.get(cls, "").upper() == room.upper():
                    score += 5000
                if block_room.get((day, cls, block), "").upper() == room.upper():
                    score += 4000
                if prefer_theory_rooms and room in theory_rooms:
                    score += 1000
                # Stable room ordering avoids arbitrary movement.
                score -= (theory_rooms + fallback_rooms).index(room)
                candidates.append((score, room))

        if candidates:
            room = max(candidates, key=lambda x: x[0])[1]
            block_room[(day, cls, block)] = room
            last_room[cls] = room
            for row in group:
                alloc.append({**row, "Room": room, "Allocation": "THEORY-AUTO", "Shift_Block": block})
        else:
            for row in group:
                alloc.append({**row, "Room": "UNALLOCATED", "Allocation": "FAILED", "Shift_Block": block,
                              "Reason": "No common room available for the theory block"})

    # Preserve original timetable order and prepare output.
    order = {(r["Day"], r["Period"], r["Class"], r["Subject"], r["Faculty"]): i for i, r in enumerate(rows)}
    alloc.sort(key=lambda x: order.get((x["Day"], x["Period"], x["Class"], x["Subject"], x["Faculty"]), 10**9))

    # Metrics.
    class_day_blocks = defaultdict(set)
    theory_changes = 0
    previous = {}
    for r in alloc:
        if r["Room"] == "UNALLOCATED" or is_lab_subject(r["Subject"]):
            continue
        key = (r["Class"], r["Day"])
        b = r["Shift_Block"]
        room = r["Room"].upper()
        class_day_blocks[key].add((b, room))
    for key, pairs in class_day_blocks.items():
        for b, room in sorted(pairs):
            if key in previous and previous[key] != room:
                theory_changes += 1
            previous[key] = room

    total = len(alloc)
    allocated = sum(r["Room"] != "UNALLOCATED" for r in alloc)
    labs = sum(is_lab_subject(r["Subject"]) for r in alloc)
    theory = total - labs
    fallback_theory = sum(
        (not is_lab_subject(r["Subject"])) and
        any(rr["room"].upper() == r["Room"].upper() and room_is_lab_type(rr) for rr in room_records)
        for r in alloc if r["Room"] != "UNALLOCATED"
    )

    room_usage = defaultdict(int)
    for r in alloc:
        if r["Room"] != "UNALLOCATED":
            room_usage[r["Room"]] += 1

    metrics = {
        "total timetable periods": total,
        "allocated periods": allocated,
        "unallocated periods": total - allocated,
        "lab periods (fixed rooms)": labs,
        "theory periods": theory,
        "theory periods using lab-type rooms": fallback_theory,
        "theory room changes per day/class": theory_changes,
        "rooms available": len(all_rooms),
        "rooms used": len(room_usage),
        "allocation percentage": round((allocated / total) * 100, 2) if total else 0.0,
    }
    return pd.DataFrame(alloc), metrics, sorted(lab_unallocated, key=lambda x: (x["Day"], x["Period"], x["Class"]))


def update_room_assignments_supabase(supabase, timetable_df, table_name="timetable"):
    """Write only the room field back to Supabase; subject/day/period/class remain unchanged."""
    if timetable_df is None or timetable_df.empty:
        return 0
    written = 0
    for _, r in timetable_df.iterrows():
        if clean(r.get("Room")) in ("", "UNALLOCATED"):
            continue
        payload = {"room": clean(r["Room"])}
        supabase.table(table_name).update(payload).match({
            "class_id": clean(r["Class"]),
            "day": clean(r["Day"]),
            "period": int(r["Period"]),
            "subject": clean(r["Subject"]),
        }).execute()
        written += 1
    return written


if __name__ == "__main__":
    print("room_allocator.py loaded")
    print("Use allocate_rooms(TT_DATA, rooms_df, labs_df) from the main timetable program.")
