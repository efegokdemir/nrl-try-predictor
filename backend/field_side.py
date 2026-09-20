"""Jersey number -> field side, shared by the model and the scraper.

Starting jerseys 1-13 map to a fixed side of the field. This underpins the
"edge vulnerability" factor in model.py and the edge attribution the scraper
records per try, so both must read the same table — it used to be copied into
each module with an "keep in sync" comment, which is exactly the kind of
duplication that drifts silently.

Bench players (14+) aren't here: they inherit whichever starter's edge they
came on for, which scraper.py works out from the interchange timeline.
"""

JERSEY_FIELD_SIDE = {
    1: "fullback",  # Fullback — own category, scores across all edges
    2: "right",     # Right Wing
    3: "right",     # Right Centre
    4: "left",      # Left Centre
    5: "left",      # Left Wing
    6: "left",      # Five-Eighth (typically left side)
    7: "right",     # Halfback (typically right side)
    8: "middle",    # Prop
    9: "middle",    # Hooker
    10: "middle",   # Prop
    11: "left",     # Left 2nd Row
    12: "right",    # Right 2nd Row
    13: "middle",   # Lock
}
