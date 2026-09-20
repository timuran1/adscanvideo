# AdScanVideo analytics report

This read-only report separates raw traffic from engaged visits and summarizes the
product funnel by analysis mode and input method. It does not request or export
filenames, video URLs, job IDs, analysis text, or contact details.

## Setup

1. Give a Google service account **Viewer** access to GA4 property `543672363`.
2. Enable Google Analytics Data API in the service account's Google Cloud project.
3. Store its JSON key outside this repository. The default local path is
   `~/.config/adscanvideo/ga4-reader.json` with file mode `600`.
4. Install `google-analytics-data` in a virtual environment.

Run:

```sh
python analytics/report.py --output /path/to/report-directory
```

Set `GOOGLE_APPLICATION_CREDENTIALS` to use a different key location. Never add a
service-account JSON file to this repository.

The report flags channel or country rows with at least five sessions and an
engagement rate below 15 percent. This is a review signal, not proof of bot traffic.
