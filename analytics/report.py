#!/usr/bin/env python3
"""Generate a privacy-safe GA4 traffic and AdScanVideo funnel report."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, OrderBy, RunReportRequest

PROPERTY_ID = os.environ.get("ADSCAN_GA4_PROPERTY", "543672363")
DEFAULT_CREDENTIALS = Path("~/.config/adscanvideo/ga4-reader.json").expanduser()
FUNNEL_EVENTS = {
    "analyzer_engaged", "analysis_cta_clicked", "analysis_started",
    "analysis_completed", "analysis_failed", "analysis_submission_failed",
    "analysis_validation_failed", "quota_blocked", "result_copied",
    "result_downloaded", "begin_checkout", "payment_confirmed",
}


def number(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


class Reporter:
    def __init__(self, credentials: Path):
        self.client = BetaAnalyticsDataClient.from_service_account_file(str(credentials))
        self.property = f"properties/{PROPERTY_ID}"

    def run(self, dimensions, metrics, start, end="today", limit=100):
        response = self.client.run_report(RunReportRequest(
            property=self.property,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name=name) for name in dimensions],
            metrics=[Metric(name=name) for name in metrics],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name=metrics[0]), desc=True)],
            limit=limit,
        ))
        headers = dimensions + metrics
        return [dict(zip(headers, [v.value for v in row.dimension_values] + [v.value for v in row.metric_values])) for row in response.rows]


def overview(reporter, start, end="today"):
    rows = reporter.run([], ["activeUsers", "sessions", "engagedSessions", "engagementRate", "screenPageViews", "eventCount", "keyEvents"], start, end, 1)
    return rows[0] if rows else {}


def quality_flags(rows, label_key):
    flags = []
    for row in rows:
        sessions = number(row.get("sessions"))
        engagement = number(row.get("engagementRate"))
        if sessions >= 5 and engagement < 0.15:
            flags.append({"segment": row.get(label_key, "(not set)"), "sessions": int(sessions), "engagementRate": engagement})
    return flags


def build_report(reporter):
    current = overview(reporter, "7daysAgo")
    previous = overview(reporter, "14daysAgo", "8daysAgo")
    channels = reporter.run(["sessionDefaultChannelGroup"], ["sessions", "engagedSessions", "engagementRate", "activeUsers"], "7daysAgo")
    sources = reporter.run(["sessionSourceMedium"], ["sessions", "engagedSessions", "engagementRate", "activeUsers"], "7daysAgo")
    countries = reporter.run(["country"], ["sessions", "engagedSessions", "engagementRate", "activeUsers"], "7daysAgo")
    pages = reporter.run(["landingPagePlusQueryString"], ["sessions", "engagedSessions", "engagementRate", "activeUsers"], "7daysAgo")
    event_rows = reporter.run(["eventName", "customEvent:analysis_mode", "customEvent:input_method"], ["eventCount", "activeUsers"], "7daysAgo", limit=500)
    funnel = [row for row in event_rows if row.get("eventName") in FUNNEL_EVENTS]
    totals = {}
    for row in funnel:
        totals[row["eventName"]] = totals.get(row["eventName"], 0) + int(number(row["eventCount"]))
    started = totals.get("analysis_started", 0)
    completed = totals.get("analysis_completed", 0)
    failed = totals.get("analysis_failed", 0) + totals.get("analysis_submission_failed", 0)
    return {
        "generated": date.today().isoformat(), "property": PROPERTY_ID,
        "period": "last 7 days through today", "overview": current,
        "previousPeriod": previous, "channels": channels, "sources": sources,
        "countries": countries, "landingPages": pages, "funnelRows": funnel,
        "funnelTotals": totals,
        "completionRate": completed / started if started else None,
        "failureRate": failed / started if started else None,
        "qualityFlags": {
            "sources": quality_flags(sources, "sessionSourceMedium"),
            "countries": quality_flags(countries, "country"),
        },
    }


def markdown(data):
    current = data["overview"]
    previous = data["previousPeriod"]
    totals = data["funnelTotals"]
    lines = [
        "# AdScanVideo weekly analytics", "",
        f"Generated {data['generated']} for GA4 property `{data['property']}`.", "",
        "## Audience and engagement", "",
        f"- Active users: **{current.get('activeUsers', '0')}** (previous period: {previous.get('activeUsers', '0')})",
        f"- Sessions: **{current.get('sessions', '0')}**; engaged sessions: **{current.get('engagedSessions', '0')}**",
        f"- Engagement rate: **{pct(number(current.get('engagementRate')))}**",
        f"- Page views: **{current.get('screenPageViews', '0')}**", "",
        "## Product funnel", "",
        f"- Analyses started: **{totals.get('analysis_started', 0)}**",
        f"- Analyses completed: **{totals.get('analysis_completed', 0)}**",
        f"- Completion rate: **{pct(data['completionRate']) if data['completionRate'] is not None else 'No starts recorded'}**",
        f"- Failure rate: **{pct(data['failureRate']) if data['failureRate'] is not None else 'No starts recorded'}**",
        f"- Result copies/downloads: **{totals.get('result_copied', 0) + totals.get('result_downloaded', 0)}**",
        f"- Confirmed payments: **{totals.get('payment_confirmed', 0)}**", "",
        "## Acquisition", "", "| Source / medium | Sessions | Engaged | Engagement |", "|---|---:|---:|---:|",
    ]
    for row in data["sources"][:10]:
        lines.append(f"| {row['sessionSourceMedium']} | {row['sessions']} | {row['engagedSessions']} | {pct(number(row['engagementRate']))} |")
    lines += ["", "## Low-quality traffic review", ""]
    flags = data["qualityFlags"]["sources"] + data["qualityFlags"]["countries"]
    if flags:
        for flag in flags:
            lines.append(f"- Review **{flag['segment']}**: {flag['sessions']} sessions with {pct(flag['engagementRate'])} engagement.")
    else:
        lines.append("No source or country met the low-engagement review threshold.")
    lines += ["", "These flags indicate traffic worth reviewing. They do not prove that a visitor is a bot. GA4 reports aggregate devices and sessions, not verified identities.", "", "## Funnel detail by mode and input", "", "| Event | Mode | Input | Count | Users |", "|---|---|---|---:|---:|"]
    for row in data["funnelRows"]:
        lines.append(f"| {row['eventName']} | {row['customEvent:analysis_mode']} | {row['customEvent:input_method']} | {row['eventCount']} | {row['activeUsers']} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("analytics-output"))
    parser.add_argument("--credentials", type=Path, default=Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", DEFAULT_CREDENTIALS)))
    args = parser.parse_args()
    if not args.credentials.exists():
        raise SystemExit(f"Credential file not found: {args.credentials}")
    args.output.mkdir(parents=True, exist_ok=True)
    data = build_report(Reporter(args.credentials))
    (args.output / "adscanvideo-weekly.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (args.output / "adscanvideo-weekly.md").write_text(markdown(data), encoding="utf-8")
    print(args.output / "adscanvideo-weekly.md")


if __name__ == "__main__":
    main()
