#!/usr/bin/env python3
"""Read GA4 ordered user funnels, not report cohorts or Stripe revenue."""
import argparse,json,os
from datetime import datetime,timezone
from pathlib import Path
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

JOURNEYS={
 'completion_to_preview':['analysis_completed','report_export_preview_opened','begin_checkout'],
 'completion_to_unlock':['analysis_completed','report_export_unlock_clicked','begin_checkout','payment_confirmed'],
 'export_checkout':['report_export_unlock_clicked','checkout_access_step_viewed','begin_checkout','checkout_redirected'],
}
def run(start,end,output,credentials_path=None,quiet=False):
 credentials=Credentials.from_service_account_file(str(credentials_path) if credentials_path else os.getenv('GOOGLE_APPLICATION_CREDENTIALS',str(Path('~/.config/adscanvideo/ga4-reader.json').expanduser())),scopes=['https://www.googleapis.com/auth/analytics.readonly'])
 session=AuthorizedSession(credentials);property_id=os.getenv('ADSCAN_GA4_PROPERTY','543672363')
 result={'queriedAt':datetime.now(timezone.utc).isoformat(),'start':start,'end':end,'scope':'Ordered GA4 users, closed funnel, indirectly followed steps; includes founder activity. Not same-report cohorts. GA4 can be delayed. Access-step event added October 9.','journeys':{}}
 for name,events in JOURNEYS.items():
  request={'dateRanges':[{'startDate':start,'endDate':end}],'funnel':{'isOpenFunnel':False,'steps':[{'name':event,'filterExpression':{'funnelEventFilter':{'eventName':event}}} for event in events]}}
  response=session.post(f'https://analyticsdata.googleapis.com/v1alpha/properties/{property_id}:runFunnelReport',json=request,timeout=30)
  response.raise_for_status();data=response.json();rows=data.get('funnelVisualization',{}).get('rows',[])
  counts={row['dimensionValues'][0]['value'].split('. ',1)[-1]:int(row['metricValues'][0]['value']) for row in rows}
  result['journeys'][name]={'steps':[{'event':event,'users':counts.get(event,0)} for event in events],'metadata':data.get('funnelVisualization',{}).get('metadata',{})}
 session.close();output.mkdir(parents=True,exist_ok=True)
 (output/'ordered-funnel.json').write_text(json.dumps(result,indent=2)+'\n')
 lines=['# AdScanVideo ordered conversion funnel','',result['scope'],'',f'Period: {start} through {end}.','']
 for name,journey in result['journeys'].items():
  lines+=[f'## {name}','']+[f"- {step['event']}: {step['users']} users" for step in journey['steps']]+['']
 (output/'ordered-funnel.md').write_text('\n'.join(lines))
 if not quiet: print(json.dumps(result,indent=2))
 return result
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--start',default='6daysAgo');p.add_argument('--end',default='today');p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.start,a.end,a.output)
