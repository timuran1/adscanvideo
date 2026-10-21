/* Authenticated support inbox. Message content is always escaped plain text. */
(()=>{
  let contacts=[],selected='',offset=0,total=0,requestId='';
  const date=value=>value?new Date(value*1000).toLocaleString():'Not verified';
  async function api(path,options={}){
    const response=await fetch(API+'/admin/'+path,{cache:'no-store',...options,headers:{'Content-Type':'application/json',Authorization:'Bearer '+dashboardToken,...options.headers}});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);
    return data;
  }
  function messageHtml(messages){
    return messages.length?messages.map(m=>`<article class="support-message ${m.direction==='incoming'?'incoming':'outgoing'}"><header><span>${m.direction==='incoming'?'Reply received':['scheduled','retry','sending','unknown'].includes(m.status)?'Outgoing email':'Sent message'} · ${esc(m.provider)} · ${esc(m.status)}</span><time>${esc(date(m.created))}</time></header><h4>${esc(m.subject)}</h4><pre>${esc(m.body)}</pre>${m.error?'<p class="inbox-error">'+esc(m.error)+'</p>':''}</article>`).join(''):'<p class="empty">No saved messages yet.</p>';
  }
  function state(data){
    const sync=(data.sync||[]).find(s=>s.provider==='gmail');
    $('inbox-sync').textContent=data.automatic_receiving?'Automatic reply capture is connected for support emails sent through this dashboard. New replies appear here within 30 seconds while this page is open. Earlier Gmail conversations remain in Gmail.':`Replies go to ${data.reply_to||'the connected Gmail mailbox'}. ${sync?'Gmail last checked '+date(sync.synced)+'. ':'Gmail has not been synced yet. '}Refreshing this dashboard reloads saved history; it does not check Gmail. Open the mailbox for the newest replies.`;
    if(data.welcome_enabled)$('inbox-sync').textContent+=' Welcome email enabled: sent once, about two minutes after a new user verifies their email. Replies return here. Existing accounts are not backfilled.';
  }
  function list(){
    $('contact-list').innerHTML=contacts.length?contacts.map(c=>`<button type="button" class="contact-row" data-contact="${esc(c.id)}" aria-pressed="${c.id===selected}"><strong>${esc(c.email)}</strong><small>${c.verified?'Verified '+esc(date(c.verified)):'Report email only'}<br>${c.marketing_opt_in?'Marketing opted in':'No confirmed marketing opt in'}</small><small class="${c.credit_checkouts+c.subscription_checkouts&&!c.confirmed_payments?'contact-checkout':''}">${count(c.credit_checkouts+c.subscription_checkouts)} checkout starts · ${count(c.confirmed_payments)} confirmed payments</small><small>${count(c.messages_sent)} support messages · ${count(c.replies)} replies</small></button>`).join(''):'<p class="empty">No matching saved emails.</p>';
    $('contact-count').textContent=`${count(total)} saved contacts`;
    $('inbox-page').textContent=total?`${offset+1}–${Math.min(offset+100,total)} of ${total}`:'0 contacts';
    $('inbox-prev').disabled=offset===0;$('inbox-next').disabled=offset+100>=total;
  }
  async function select(ident){
    selected=ident;list();$('contact-detail').innerHTML='<p class="empty">Loading message history…</p>';
    try{
      const data=await api('contacts/'+ident+'/messages');if(selected!==ident)return;state(data);
      const c=contacts.find(x=>x.id===ident);requestId=crypto.randomUUID();
      $('contact-detail').innerHTML=`<h3>${esc(data.email)}</h3><p class="note">${c?.verified?'Verified signup '+esc(date(c.verified)):'Saved from report email delivery.'}${c?.last_checkout?'<br>Latest checkout: '+esc(date(c.last_checkout)):''}</p><a href="https://mail.google.com/mail/u/?authuser=${encodeURIComponent(data.reply_to||'')}&amp;view=tl&amp;search=query&amp;q=${encodeURIComponent('{from:'+data.email+' to:'+data.email+'}')}" target="_blank" rel="noreferrer">Open this conversation in Gmail</a><div class="message-history" aria-label="Message history">${messageHtml(data.messages)}</div><form id="support-compose" class="support-compose"><h3>Send individual support email</h3><p class="note">Help with this person’s use of AdScanVideo. Signup alone does not authorize marketing. ${data.automatic_receiving?'Replies return to this dashboard automatically.':'Replies go to '+esc(data.reply_to||'the configured mailbox')+'.'} Provider acceptance does not confirm delivery.</p><label>Subject<input id="support-subject" maxlength="200" required></label><label>Message<textarea id="support-body" maxlength="10000" required></textarea></label><label class="compose-confirm"><input id="support-confirm" type="checkbox" required>I reviewed the recipient and message. This is individual support, not a marketing campaign.</label><button class="button" ${data.sending_enabled?'':'disabled'}>Send support email</button><p id="support-result" role="status" class="note">${data.sending_enabled?'':'Support sending is not configured.'}</p></form>`;
      $('support-compose').addEventListener('submit',async event=>{
        event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;
        try{
          const result=await api('contacts/'+ident+'/messages',{method:'POST',body:JSON.stringify({request_id:requestId,subject:$('support-subject').value,body:$('support-body').value,confirmed:$('support-confirm').checked,purpose:'support'})});
          if(['unknown','sending'].includes(result.status)){$('support-result').textContent='This send is unconfirmed. Check the mailbox before trying another message.';return;}
          await select(ident);await loadInbox(false);
        }catch(error){$('support-result').textContent=error.message;/* preserve the same send identifier on retry */button.disabled=false;}
      });
    }catch(error){$('contact-detail').textContent=error.message;}
  }
  async function loadInbox(refreshThread=true){
    $('inbox-result').textContent='Loading contacts…';
    try{
      const data=await api('contacts?q='+encodeURIComponent($('contact-search').value)+'&offset='+offset);
      contacts=data.contacts;total=data.total;state(data);list();$('inbox-result').textContent='';
      if(!contacts.some(c=>c.id===selected)){selected='';$('contact-detail').innerHTML='<p class="empty">Choose a contact to see checkout activity and saved messages.</p>';}
      else if(refreshThread)await select(selected);
    }catch(error){$('inbox-result').textContent=error.message;}
  }
  let polling=false;
  setInterval(async()=>{
    if(polling||document.hidden||!dashboardToken||$('dashboard-view').hidden)return;
    polling=true;
    try{
      await loadInbox(false);
      const ident=selected;
      if(ident){
        const data=await api('contacts/'+ident+'/messages');
        if(ident===selected){
          const history=$('contact-detail').querySelector('.message-history');
          const html=messageHtml(data.messages);
          if(history&&history.innerHTML!==html){const top=history.scrollTop;const nearEnd=history.scrollHeight-history.clientHeight-top<50;history.innerHTML=html;history.scrollTop=nearEnd?history.scrollHeight:top;}
        }
      }
    }catch{/* Keep the last saved view on a temporary connection failure. */}
    finally{polling=false;}
  },30000);
  window.loadContactInbox=loadInbox;
  $('contact-list').addEventListener('click',event=>{const button=event.target.closest('[data-contact]');if(button)select(button.dataset.contact);});
  $('contact-search-form').addEventListener('submit',event=>{event.preventDefault();offset=0;loadInbox();});
  $('inbox-prev').addEventListener('click',()=>{offset=Math.max(0,offset-100);loadInbox();});
  $('inbox-next').addEventListener('click',()=>{offset+=100;loadInbox();});
  $('inbox-refresh').addEventListener('click',()=>loadInbox());
})();
