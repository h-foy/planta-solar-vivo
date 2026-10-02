"""Reads one MeterReader / Ace Pilot PRN file into a list of rows of 6 floats.

A full-day file gives 96 rows (00:15 .. 24:00); a partial "_hasta_HHMM" file gives
only the intervals recorded so far. Column order is the PRN order:
  AGRIM02P: [solar export, import, voltage, ...]   DAGSR01P: [export/injected, import/grid, voltage, ...]
"""
import csv


def parse(path):
    rows = []
    with open(path, 'r', encoding='latin-1') as f:
        for i, rec in enumerate(csv.reader(f)):
            if i < 2 or len(rec) < 7:          # line 1 "kwh", line 2 header
                continue
            try:
                rows.append([float(x) for x in rec[1:7]])
            except ValueError:
                continue
    return rows
