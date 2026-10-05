(() => {
  const byId=id=>document.getElementById(id);
  const escape=value=>String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const safeURL=value=>{try{const u=new URL(value);return ['https:','http:'].includes(u.protocol)?u.href:null}catch{return null}};
  const platform=p=>p.startsWith('Reddit')||p.startsWith('r/')?'Reddit':p.startsWith('LinkedIn')?'LinkedIn':p.startsWith('YouTube')?'YouTube':p.startsWith('X')?'X':p;
  let records=[],filtered=[],lastLibrary=null;
  const nav=document.querySelector('.nav');
  const link=document.createElement('a');link.href='#marketing';link.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 9h4l11-4v14L8 15H4V9zm4 6 2 5h3l-2-4"/></svg>Marketing';nav.insertBefore(link,nav.children[2]);
  const mobile=document.createElement('a');mobile.href='#marketing';mobile.className='mobile-marketing';mobile.textContent='Marketing ↗';document.querySelector('.sidebar').append(mobile);
  function highlight(){const marketing=location.hash==='#marketing';document.querySelectorAll('.nav a[href^="#"]').forEach(a=>{if(a.hash===(location.hash||'#overview'))a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});byId('marketing').classList.toggle('hidden',!marketing);document.querySelector('.main > .metrics').classList.toggle('hidden',marketing);document.querySelectorAll('.layout > .panel:not(.marketing-center)').forEach(p=>p.classList.toggle('hidden',marketing));document.querySelector('.topbar h1').textContent=marketing?'Marketing workspace':'Operations overview';mobile.href=marketing?'#overview':'#marketing';mobile.textContent=marketing?'Overview ↗':'Marketing ↗';}
  window.addEventListener('hashchange',highlight);highlight();
  function filter(){
    const query=byId('marketing-search').value.trim().toLowerCase(),chosen=byId('marketing-platform').value,status=byId('marketing-status').value;
    filtered=records.filter(p=>(!chosen||platform(p.platform)===chosen)&&(!status||p.status===status)&&(!query||[p.title,p.platform,p.notes,p.url].join(' ').toLowerCase().includes(query)));
    byId('marketing-count').textContent=`${filtered.length} ${filtered.length===1?'record':'records'} shown · ${records.length} in your library`;
    byId('marketing-export').disabled=!filtered.length;
    byId('marketing-body').innerHTML=filtered.length?filtered.map(p=>{const url=safeURL(p.url),tone=p.status==='Published'?'good':p.status==='Not live'?'bad':'warn';return `<tr><td>${escape(p.date||'Not recorded')}</td><td>${escape(p.platform)}</td><td><span class="post-title">${escape(p.title)}</span><span class="post-note">${escape([p.evidence,p.notes].filter(Boolean).join(' · '))}</span></td><td><span class="pill ${tone}">${escape(p.status)}</span></td><td>${url?`<a class="post-open" href="${escape(url)}" target="_blank" rel="noopener noreferrer" aria-label="${escape('Open '+p.title+' in a new tab')}">Open post ↗</a>`:'<span class="post-note">No link recorded</span>'}</td></tr>`}).join(''):'<tr><td colspan="5" class="empty">No posts match these filters. Try another platform, status or search.</td></tr>';
  }
  window.renderMarketing=(library,traffic)=>{
    if(library&&library!==lastLibrary){lastLibrary=library;records=Array.isArray(library.posts)?library.posts.filter(p=>p&&typeof p.title==='string'&&typeof p.platform==='string'):[];
      const previous=byId('marketing-platform').value;
      byId('marketing-platform').innerHTML='<option value="">All platforms</option>'+[...new Set(records.map(p=>platform(p.platform)))].sort().map(p=>`<option value="${escape(p)}">${escape(p)}</option>`).join('');byId('marketing-platform').value=previous;
      byId('marketing-published').textContent=records.filter(p=>p.status==='Published').length;
      byId('marketing-videos').textContent=records.filter(p=>platform(p.platform)==='YouTube'&&p.status==='Published').length;
      byId('marketing-articles').textContent=records.filter(p=>p.status==='Published'&&(/article|blog/i.test(p.platform)||p.platform==='Medium')).length;
      byId('marketing-review').textContent=records.filter(p=>['URL missing','Needs verification'].includes(p.status)).length;
      byId('marketing-updated').textContent=`Library updated ${library.updated||'date unavailable'}`;filter();
    }else if(!library&&!lastLibrary){byId('marketing-updated').textContent='Library unavailable';byId('marketing-count').textContent='The post library is unavailable. Refresh data to try again.';byId('marketing-body').innerHTML='<tr><td colspan="5" class="empty">The publication library could not be loaded. Operations metrics are still available in Overview.</td></tr>';}
    if(traffic){const sources=traffic.sources||[];const sessions=prefix=>sources.filter(s=>(s.sessionSourceMedium||'').startsWith(prefix)).reduce((n,s)=>n+Number(s.sessions||0),0);byId('marketing-performance').innerHTML='<span>Sessions · last 7 calendar days</span>'+[['Google organic','google / organic'],['ChatGPT','chatgpt.com'],['LinkedIn','linkedin.com'],['Product Hunt','producthunt.com'],['YouTube','youtube.com'],['X','t.co']].map(([label,prefix])=>`<span>${label} <strong>${sessions(prefix).toLocaleString()}</strong></span>`).join('');}
  };
  ['marketing-search','marketing-platform','marketing-status'].forEach(id=>byId(id).addEventListener(id==='marketing-search'?'input':'change',filter));
  byId('marketing-export').addEventListener('click',()=>{const cell=value=>{let s=String(value??'');if(/^[=+@-]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"'};const csv=[['Date','Platform','Title','Status','URL','Evidence','Notes'],...filtered.map(p=>[p.date,p.platform,p.title,p.status,safeURL(p.url)||'',p.evidence,p.notes])].map(row=>row.map(cell).join(',')).join('\r\n');const url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8;'})),a=document.createElement('a');a.href=url;a.download=`adscanvideo-marketing-${new Date().toISOString().slice(0,10)}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
})();
