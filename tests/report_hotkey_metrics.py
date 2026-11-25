#!/usr/bin/env python3
"""Summarize content-free desktop and Gateway recordings by session."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import median


def load(paths):
    seen = set()
    for path in paths:
        with path.open() as stream:
            for line in stream:
                _, marker, payload = line.partition("IV_METRIC ")
                if not marker or payload in seen:
                    continue
                try:
                    record = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(record, dict) and "session" in record and "event" in record:
                    seen.add(payload)
                    yield record


def report(records):
    sessions = {}
    groups = defaultdict(Counter)
    samples = defaultdict(list)

    for record in records:
        if record["event"] == "session_open":
            sessions[record["session"]] = (
                record["task"], "stream" if record["streaming"] else "baseline", record["poll_ms"],
                record.get("ui_poll_ms", 700),
            )
    for session, group in sessions.items():
        groups[group]["sessions"] += 1
    for record in records:
        group = sessions.get(record["session"])
        if group is None:
            continue
        event = record["event"]
        if event == "final_text":
            groups[group]["final"] += 1
            groups[group]["nonempty"] += bool(record.get("useful_text"))
        if event == "first_text":
            groups[group]["visible"] += 1
        if event == "preview_failed":
            groups[group]["preview_failures"] += 1
        if event == "result_available":
            event += ".preview" if record["preview"] else ".confirmed"
        for key, value in record.items():
            if key.endswith("_ms") and key not in {"poll_ms", "ui_poll_ms"} and type(value) in (int, float):
                samples[group, event, key].append(value)

    for group, counts in sorted(groups.items()):
        task, mode, poll, ui = group
        print(f"\n{task} / {mode} / {poll:g} ms HTTP poll / {ui:g} ms UI poll")
        print(f"sessions={counts['sessions']} visible={counts['visible']} final={counts['final']} "
              f"nonempty={counts['nonempty']} preview_failures={counts['preview_failures']}")
        print("stage\tn\tmedian_ms\tmin_ms\tmax_ms")
        for (sample_group, event, key), values in sorted(samples.items()):
            if group == sample_group:
                print(f"{event}.{key}\t{len(values)}\t{median(values):.1f}\t{min(values):.1f}\t{max(values):.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    records = list(load(parser.parse_args().logs))
    if not any(record["event"] == "session_open" for record in records):
        parser.error("No desktop session_open metrics were found in the supplied logs")
    report(records)


if __name__ == "__main__":
    main()
