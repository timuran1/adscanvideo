#!/usr/bin/env python3
"""Write a small GA4 snapshot for the AdScanVideo operations dashboard.

Uses the GA4 Data API directly so the VPS needs only system requests and
cryptography, rather than the heavier Analytics client dependency.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


SCOPE = 'https://www.googleapis.com/auth/analytics.readonly'
PROPERTY = os.getenv('ADSCAN_GA4_PROPERTY', '543672363')


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=').decode()


def access_token(key):
    now = int(time.time())
    header = encoded({'alg': 'RS256', 'typ': 'JWT'})
    claims = encoded({'iss': key['client_email'], 'scope': SCOPE,
                      'aud': key['token_uri'], 'iat': now, 'exp': now + 3600})
    signing_input = (header + '.' + claims).encode()
    private_key = serialization.load_pem_private_key(key['private_key'].encode(), password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = signing_input.decode() + '.' + base64.urlsafe_b64encode(signature).rstrip(b'=').decode()
    response = requests.post(key['token_uri'], data={
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion,
    }, timeout=20)
    response.raise_for_status()
    return response.json()['access_token']


def report(token, dimensions, metrics, start, end='today', limit=100):
    body = {
        'dateRanges': [{'startDate': start, 'endDate': end}],
        'dimensions': [{'name': name} for name in dimensions],
        'metrics': [{'name': name} for name in metrics],
        'limit': str(limit),
        'orderBys': [{'metric': {'metricName': metrics[0]}, 'desc': True}],
    }
    response = requests.post(
        f'https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY}:runReport',
        headers={'Authorization': 'Bearer ' + token}, json=body, timeout=25)
    response.raise_for_status()
    names = dimensions + metrics
    return [dict(zip(names, [v['value'] for v in row.get('dimensionValues', [])] +
                     [v['value'] for v in row.get('metricValues', [])]))
            for row in response.json().get('rows', [])]


def build(key):
    token = access_token(key)
    metrics = ['activeUsers', 'sessions', 'engagedSessions', 'engagementRate']
    current = report(token, [], metrics, '6daysAgo', limit=1)
    previous = report(token, [], metrics, '13daysAgo', '7daysAgo', limit=1)
    acquisition_metrics = ['sessions', 'activeUsers', 'engagedSessions', 'engagementRate']
    countries = report(token, ['country'], acquisition_metrics, '6daysAgo', limit=20)
    sources = report(token, ['sessionSourceMedium'], acquisition_metrics, '6daysAgo', limit=20)
    events = report(token, ['eventName'], ['eventCount'], '6daysAgo', limit=200)
    return {
        'property': PROPERTY,
        'period': 'Last 7 calendar days including today; GA4 may be delayed',
        'overview': current[0] if current else {},
        'previousPeriod': previous[0] if previous else {},
        'countries': countries,
        'sources': sources,
        'funnelTotals': {row['eventName']: int(row['eventCount']) for row in events},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = build(json.loads(args.credentials.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pending = args.output.with_suffix(args.output.suffix + '.pending')
    pending.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.chmod(pending, 0o640)
    pending.replace(args.output)
    print('GA4 dashboard snapshot updated')


if __name__ == '__main__':
    main()
