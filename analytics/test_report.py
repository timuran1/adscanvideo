import unittest

from report import build_report, markdown


class FakeReporter:
    def run(self, dimensions, metrics, start, end="today", limit=100):
        if not dimensions:
            return [{"activeUsers": "2", "sessions": "3", "engagedSessions": "2", "engagementRate": "0.66", "screenPageViews": "4", "eventCount": "6", "keyEvents": "0"}]
        if dimensions == ["sessionDefaultChannelGroup"]:
            return []
        if dimensions == ["sessionSourceMedium"]:
            return []
        if dimensions == ["sessionCampaignName", "sessionSourceMedium"]:
            return [{"sessionCampaignName": "2026w44_upload_troubleshooting", "sessionSourceMedium": "linkedin / organic_social", "sessions": "2", "engagedSessions": "2", "engagementRate": "1", "activeUsers": "2"}]
        if dimensions == ["country"] or dimensions == ["landingPagePlusQueryString"]:
            return []
        if "eventName" in dimensions:
            return [
                {"eventName": "analysis_submission_failed", "customEvent:analysis_mode": "shots", "customEvent:input_method": "upload", "customEvent:failure_type": "connection", "eventCount": "2", "activeUsers": "1"},
                {"eventName": "report_export_preview_available", "customEvent:analysis_mode": "ads", "customEvent:input_method": "upload", "customEvent:failure_type": "(not set)", "eventCount": "3", "activeUsers": "3"},
            ]
        raise AssertionError(f"Unexpected report dimensions: {dimensions}")


class ReportTest(unittest.TestCase):
    def test_campaign_and_failure_detail_are_reported(self):
        data = build_report(FakeReporter())
        self.assertEqual(data["campaigns"][0]["sessionCampaignName"], "2026w44_upload_troubleshooting")
        self.assertEqual(data["submissionFailureRows"][0]["customEvent:failure_type"], "connection")
        output = markdown(data)
        self.assertIn("Campaign-tagged sessions", output)
        self.assertIn("Pre-start submission failure detail", output)
        self.assertIn("A connection error can reflect network, browser, DNS, CORS or edge/API transport", output)


if __name__ == "__main__":
    unittest.main()
