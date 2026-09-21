#!/usr/bin/env python3
"""Submit canonical sitemap URLs to IndexNow after a production deploy."""
import json
from pathlib import Path
import ssl
import urllib.request
import xml.etree.ElementTree as ET

import certifi

ROOT = Path(__file__).resolve().parents[1]
KEY = "3dfc71b07cc6adb64167684ebe175cc1"
HOST = "adscanvideo.com"
namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
tree = ET.parse(ROOT / "sitemap.xml")
urls = [node.text for node in tree.findall("s:url/s:loc", namespace) if node.text]
payload = json.dumps({
    "host": HOST,
    "key": KEY,
    "keyLocation": f"https://{HOST}/{KEY}.txt",
    "urlList": urls,
}).encode()
request = urllib.request.Request(
    "https://api.indexnow.org/indexnow",
    data=payload,
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
context = ssl.create_default_context(cafile=certifi.where())
with urllib.request.urlopen(request, timeout=30, context=context) as response:
    print(f"IndexNow accepted {len(urls)} URLs (HTTP {response.status}).")
