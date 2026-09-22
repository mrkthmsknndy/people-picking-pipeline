#!/usr/bin/env python3
"""
Generates a small, entirely FICTIONAL example dataset shaped like a raw
Qualtrics export of the People Picking survey, so anyone cloning this repo
can run the pipeline immediately without needing real student data.

Run:
    python examples/make_example_data.py

Writes:
    examples/example_raw_qualtrics.csv   -- fake "raw download"
    examples/example_roster.csv          -- fake full-cohort roster (18 people;
                                             only 14 of them "respond")
"""
import csv
import os
import random

random.seed(7)

FAKE_NAMES = [
    "Priya Sharma", "Liam O'Connor", "Yuki Tanaka", "Fatima Al-Sayed",
    "Marco Rossi", "Chidinma Okoro", "Noah Bergström", "Elena Petrova",
    "Diego Fernandez", "Amara Nwosu", "Hana Kobayashi", "Tobias Weber",
    "Grace Mensah", "Ravi Patel", "Sofia Almeida", "Lucas Dubois",
    "Mei Lin", "Kwame Asante",
]

PARTY_TYPES = [
    "COZY. For up to 12; a sit-down dinner with table service, white linens, excellent food and drinks, and a sound system for play-your-own music.",
    "MIXER. For 20-30; a stand-up dining buffet with great food; a bar serving beer, wine, and soft drinks; and a sound system for play-your-own music.",
    "PARTY! For 50-100; servers offering finger food; a bar serving beer, wine and soft drinks; and a dance floor with a professional DJ.",
]
GLIST1 = [
    "TIGHT. Only your best and closest friends.",
    "EXCLUSIVE. The most famous or influential people you can attract.",
    "EXPANSIVE. A cool mix of people who do not (yet!) know each other well.",
]


def make_row(fname, lname, cohort, i):
    others = [n for n in cohort if n != f"{fname} {lname}"]
    def picks(k):
        return ",".join(random.sample(others, k=min(k, len(others))))
    return {
        "StartDate": "2026-01-10 09:00:00", "EndDate": "2026-01-10 09:12:00",
        "Status": "IP Address", "IPAddress": "", "Progress": "100",
        "Duration (in seconds)": "720", "Finished": "True",
        "RecordedDate": "2026-01-10 09:12:01", "ResponseId": f"R_example_{i:03d}",
        "RecipientLastName": lname, "RecipientFirstName": fname,
        "RecipientEmail": f"{fname.lower()}.{lname.lower().replace(' ', '')}@example.edu",
        "ExternalReference": "", "LocationLatitude": "", "LocationLongitude": "",
        "DistributionChannel": "email", "UserLanguage": "EN-GB",
        "Last Seen Flow Element ID": "FL_EOS_ID", "Last Seen Question IDs": "",
        "pname": "Example launch party", "ptype": random.choice(PARTY_TYPES),
        "glist1": random.choice(GLIST1), "glist2": "", "glistmix": "",
        "friendship": picks(6), "advice": picks(4),
        "design": picks(5), "lobby": picks(4), "implement": picks(5),
        "picklogic ": "",
    }


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    respondents = FAKE_NAMES[:14]  # first 14 of 18 "respond"; 4 stay non-respondents

    fieldnames = [
        "StartDate", "EndDate", "Status", "IPAddress", "Progress",
        "Duration (in seconds)", "Finished", "RecordedDate", "ResponseId",
        "RecipientLastName", "RecipientFirstName", "RecipientEmail",
        "ExternalReference", "LocationLatitude", "LocationLongitude",
        "DistributionChannel", "UserLanguage", "Last Seen Flow Element ID",
        "Last Seen Question IDs", "pname", "ptype", "glist1", "glist2",
        "glistmix", "friendship", "advice", "design", "lobby", "implement",
        "picklogic ",
    ]

    rows = []
    # Qualtrics' two label rows (kept for structural realism; the pipeline drops them)
    rows.append({f: f for f in fieldnames})
    rows.append({f: f'{{"ImportId":"{f}"}}' for f in fieldnames})
    # one preview/test row the pipeline should drop by default
    preview = {f: "" for f in fieldnames}
    preview.update({"DistributionChannel": "preview", "Finished": "True", "Progress": "100"})
    rows.append(preview)

    for i, full in enumerate(respondents, start=1):
        fname, lname = full.split(" ", 1)
        row = make_row(fname, lname, respondents, i)
        if i == 5:
            row["Finished"] = "False"
            row["Progress"] = "62"
        rows.append(row)

    raw_path = os.path.join(out_dir, "example_raw_qualtrics.csv")
    with open(raw_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()  # real header row -- clean_raw() expects this at header=0
        for r in rows:
            w.writerow(r)
    print(f"Wrote {raw_path}")

    roster_path = os.path.join(out_dir, "example_roster.csv")
    with open(roster_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["roster_order", "full_name"])
        for i, name in enumerate(FAKE_NAMES, start=1):
            w.writerow([i, name])
    print(f"Wrote {roster_path}")


if __name__ == "__main__":
    main()
