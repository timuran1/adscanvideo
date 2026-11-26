// AI studio composer: swap people or objects in a clip, or recreate an ad from its prompt.
// Loaded after the main page script and uses its api(), track(), storage and credit state.
// The section stays hidden unless the API reports the studio as enabled.
(() => {
  const root = document.getElementById('studio');
  if (!root) return;
  const style = document.createElement('style');
  style.textContent = `
#studio{margin:24px 0;padding:24px;border:1px solid var(--border-strong);border-radius:14px;background:var(--surface);box-shadow:var(--shadow-md)}
#studio h3{font-size:1.25rem;letter-spacing:-.02em;margin:0 0 6px}
#studio .studio-example{margin:0 0 18px;max-width:640px}#studio .studio-example img{display:block;width:100%;height:auto;border-radius:10px}#studio .studio-example figcaption{font-size:.8rem;color:var(--ink-dim);margin-top:6px}
#studio .studio-rates,.studio-intro .studio-rates{font-size:.82rem;color:var(--ink-dim);margin:0 0 14px;max-width:70ch}
.studio-intro{max-width:1080px;margin:56px auto;padding:0 24px}.studio-intro h2{font-size:1.7rem;letter-spacing:-.03em;margin:0 0 8px}.studio-intro>p{color:var(--ink-dim);margin:0 0 22px;max-width:62ch}.studio-intro-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}.studio-intro-card{border:1px solid var(--border-strong);border-radius:14px;background:var(--surface);padding:14px;margin:0}.studio-intro-card img{display:block;width:100%;height:auto;border-radius:9px}.studio-intro-card h3{font-size:1.02rem;margin:12px 0 4px}.studio-intro-card p{font-size:.86rem;color:var(--ink-dim);margin:0 0 6px}.studio-intro a.studio-go{display:inline-block;margin-top:20px;text-decoration:none}
#studio .studio-sub{color:var(--ink-dim);font-size:.9rem;margin:0 0 16px;max-width:62ch}
.studio-tabs{display:inline-flex;gap:4px;padding:4px;border-radius:10px;background:var(--surface2);margin-bottom:16px}
.studio-tabs button{border:0;background:none;padding:8px 14px;border-radius:7px;font:600 .85rem var(--font);color:var(--ink-dim);cursor:pointer}
.studio-tabs button[aria-pressed="true"]{background:#fff;color:var(--ink);box-shadow:0 2px 7px rgba(23,31,54,.1)}
.studio-box{border:1px solid #cbd5e3;border-radius:12px;background:#fbfcff;padding:12px}
.studio-chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.studio-chip{display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--border-strong);border-radius:10px;background:#fff;min-height:48px}
.studio-chip img,.studio-chip .studio-kind{width:34px;height:34px;border-radius:7px;object-fit:cover;background:var(--surface2);display:grid;place-items:center;font:600 .58rem var(--mono);color:var(--ink-dim);flex:none}
.studio-chip .studio-name{display:flex;align-items:center;font:700 .82rem var(--font);color:var(--ink)}
.studio-chip input{width:9ch;border:0;border-bottom:1px dashed #b0bed6;background:none;font:700 .82rem var(--font);color:var(--ink);padding:1px 0}
.studio-chip small{display:block;color:var(--ink-muted);font-size:.7rem}
.studio-chip button{border:0;background:none;color:var(--ink-muted);cursor:pointer;font-size:1rem;line-height:1;padding:4px}
.studio-chip.is-busy{opacity:.6}
.studio-add{border:1px dashed #b0bed6;border-radius:10px;background:none;padding:0 14px;min-height:48px;font:600 .82rem var(--font);color:var(--ink-dim);cursor:pointer}
.studio-add:hover{border-color:var(--accent);color:var(--ink)}
#studio textarea,#studio-image-prompt{width:100%;border:0;background:none;resize:vertical;font:inherit;font-size:.95rem;line-height:1.55;color:var(--ink);min-height:110px;outline:none}
#studio-image-row{display:flex;gap:8px;margin:0 0 10px;padding:8px;border-radius:10px;background:var(--surface2)}
#studio-image-prompt{min-height:0;flex:1;background:#fff;border:1px solid #cbd5e3;border-radius:8px;padding:8px 10px;font-size:.88rem}
.studio-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
.studio-controls select{border:1px solid #cbd5e3;border-radius:8px;background:#fff;padding:8px 10px;font:600 .82rem var(--font);color:var(--ink)}
.studio-controls .studio-link{border:0;background:none;color:var(--accent-hover);font:600 .82rem var(--font);cursor:pointer;padding:8px 4px}
#studio-cost{margin-left:auto;color:var(--ink-dim);font-size:.8rem}
.studio-rights{display:flex;gap:8px;align-items:flex-start;margin:14px 0;font-size:.82rem;color:var(--ink-dim);max-width:70ch}
.studio-go{border:0;border-radius:9px;background:var(--ink);color:#fff;padding:13px 22px;font:600 .95rem var(--font);cursor:pointer}
.studio-go:disabled,.studio-secondary:disabled{opacity:.55;cursor:default}
.studio-secondary{border:1px solid var(--border-strong);border-radius:9px;background:#fff;color:var(--ink);padding:9px 14px;font:600 .82rem var(--font);cursor:pointer}
#studio-message{margin:12px 0 0;font-size:.88rem;color:var(--ink-dim)}
#studio-message[data-state="error"]{color:#b42318}
#studio-output{margin-top:16px}
#studio-output video{display:block;max-width:100%;max-height:560px;border-radius:12px;background:#000}
#studio-output a{display:inline-block;margin-top:10px;font:600 .88rem var(--font);color:var(--accent-hover)}
@media (max-width:640px){#studio{padding:16px}#studio-cost{margin-left:0;width:100%}.studio-go{width:100%}}`;
  document.head.append(style);
  root.innerHTML = `
  <h3 id="studio-title">Remake this video with AI</h3>
  <p class="studio-sub">Swap a person or product inside your clip, or recreate the ad as a new video from its prompt. Clips up to 30 seconds.</p>
  <div class="studio-tabs" role="group" aria-label="What to make">
    <button type="button" data-studio-mode="swap" aria-pressed="true">Swap in video</button>
    <button type="button" data-studio-mode="recreate" aria-pressed="false">Recreate</button>
  </div>
  <figure class="studio-example"><img id="studio-example-image" alt="" loading="lazy" width="1200" height="493"><figcaption id="studio-example-text"></figcaption></figure>
  <p id="studio-rates" class="studio-rates"></p>
  <div class="studio-box">
    <div class="studio-chips" id="studio-chips"></div>
    <div id="studio-image-row" hidden>
      <input id="studio-image-prompt" type="text" maxlength="1000" placeholder="Describe the image to create, for example: a red glass bottle on white" aria-label="Image description">
      <button type="button" class="studio-secondary" id="studio-image-go">Create image</button>
    </div>
    <label class="sr-only" for="studio-prompt" style="position:absolute;left:-9999px">Prompt</label>
    <textarea id="studio-prompt" maxlength="2000"></textarea>
    <div class="studio-controls">
      <select id="studio-style" aria-label="Kind of swap"><option value="swap">Swap a person or object</option><option value="motion">Give my character this motion</option></select>
      <select id="studio-duration" aria-label="Length"></select>
      <select id="studio-aspect" aria-label="Aspect ratio"></select>
      <button type="button" class="studio-link" id="studio-use-report">Use my report as the prompt</button>
      <span id="studio-cost" role="status"></span>
    </div>
  </div>
  <label class="studio-rights"><input type="checkbox" id="studio-rights"><span>I have the right to use the people, voices and footage I upload, and their consent where it is needed.</span></label>
  <button type="button" class="studio-go" id="studio-go">Generate</button>
  <button type="button" class="studio-secondary" id="studio-buy" hidden>Buy credits</button>
  <p id="studio-message" role="status" aria-live="polite"></p>
  <div id="studio-output" hidden><video id="studio-video" controls playsinline preload="metadata"></video><a id="studio-download" target="_blank" rel="noopener">Open the video file</a></div>
  <input type="file" id="studio-file" accept="image/jpeg,image/png,image/webp,audio/mpeg,audio/wav,.mp3,.wav" multiple hidden>
  <input type="file" id="studio-source-file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm" hidden>`;

  const $ = id => document.getElementById(id);
  const examples = {
    swap: { src: '/samples/swap-example.jpg', alt: 'Four frames side by side: a reference photo of a woman, the original shot of a man opening a box at a kitchen table, and two frames of the same shot with the woman in his place.',
      title: 'Swap in video', text: 'Reference photo, original shot, then two frames of the result. The kitchen, the box and the hand movement stay; only the person changes. Works for products and objects too.' },
    recreate: { src: '/samples/recreate-example.jpg', alt: 'A reference photo of a man, followed by three frames of a new video in which he opens a box at a kitchen table.',
      title: 'Recreate', text: 'Reference photo, then three frames of a new 1080p video made from one sentence. Add photos, a clip for the camera move, or a voice.' }
  };
  const disclosure = 'Every person in these examples is AI generated.';
  function rates(kind) {
    const per = n => '1 credit per ' + n + ' seconds';
    return kind === 'swap'
      ? 'Swaps: ' + per(config.swap_light_seconds_per_credit) + ' for clips up to ' + config.swap_light_max + ' seconds. Longer clips, up to ' + config.max_seconds + ' seconds, run on a heavier model and cost more: ' + per(config.swap_seconds_per_credit) + '.'
      : 'New videos: ' + per(config.seconds_per_credit) + ', up to ' + config.max_seconds + ' seconds, in ' + config.resolution + '.';
  }
  const placeholders = {
    swap: 'Optional: say what to change. Example: Replace the woman with @image1 and keep the background.',
    recreate: 'Describe the new ad shot by shot. Name your references. Example: @image1 opens the box on a kitchen table, close-up, soft morning light.'
  };
  let config = null, mode = 'swap', working = false, refs = [], counter = 0;
  // source: the clip to edit (swap) or to follow as a motion reference (recreate).
  let source = null, sourceOn = false;

  function say(text, state = '') { const el = $('studio-message'); el.textContent = text; el.dataset.state = state; }
  function clean(name) { return name.replace(/[^A-Za-z0-9_]/g, '').slice(0, 24); }
  function sourceActive() { return Boolean(source) && (mode === 'swap' || sourceOn); }
  function seconds() { const length = Number($('studio-duration').value); return mode === 'swap' ? (source ? Math.min(config.max_seconds, Math.ceil(source.seconds)) : 0) : length + (sourceActive() && config.reference_clip_billed ? Math.max(source.seconds, length * 2 / 3) : 0); }
  function credits() {
    if (!config || !seconds()) return 0;
    const rate = mode !== 'swap' ? config.seconds_per_credit : seconds() <= config.swap_light_max ? config.swap_light_seconds_per_credit : config.swap_seconds_per_credit;
    return Math.ceil(seconds() / rate);
  }

  function clipSeconds(file) {
    return new Promise(resolve => {
      const probe = document.createElement('video'), url = URL.createObjectURL(file);
      const finish = value => { URL.revokeObjectURL(url); resolve(value); };
      probe.preload = 'metadata'; probe.onloadedmetadata = () => finish(probe.duration); probe.onerror = () => finish(0);
      probe.src = url;
    });
  }
  async function setSource(file) {
    if (!file) return;
    if (file.size > 100 * 1024 * 1024 || !/\.(mp4|mov|webm)$/i.test(file.name)) { say('For the AI studio, choose an MP4, MOV or WebM clip up to 100 MB.', 'error'); return; }
    const length = await clipSeconds(file);
    if (!(length >= config.min_seconds && length <= config.max_seconds + .2)) { say('The AI studio works with clips of ' + config.min_seconds + ' to ' + config.max_seconds + ' seconds. Trim your video and choose it again.', 'error'); return; }
    source = { file, seconds: length, asset: null }; sourceOn = true; say(''); render();
  }

  function chip(kindLabel, name, detail, onRemove, editable, thumb) {
    const el = document.createElement('div'); el.className = 'studio-chip';
    const picture = thumb ? Object.assign(document.createElement('img'), { src: thumb, alt: '' }) : Object.assign(document.createElement('span'), { className: 'studio-kind', textContent: kindLabel });
    const text = document.createElement('div'), title = document.createElement('span'); title.className = 'studio-name'; title.append('@');
    let field = null;
    if (editable) { field = document.createElement('input'); field.value = name; field.maxLength = 24; field.setAttribute('aria-label', 'Reference name'); title.append(field); }
    else title.append(name);
    const small = document.createElement('small'); small.textContent = detail;
    text.append(title, small);
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', 'Remove ' + name); remove.onclick = onRemove;
    el.append(picture, text, remove);
    return { el, field };
  }
  function render() {
    const list = $('studio-chips'); list.replaceChildren();
    if (sourceActive()) list.append(chip('video', 'video', (mode === 'swap' ? 'clip to change' : 'motion reference') + ' · ' + Math.round(source.seconds) + ' s', () => { if (mode === 'swap') source = null; sourceOn = false; render(); }, false).el);
    for (const ref of refs) {
      const item = chip(ref.kind === 'audio' ? 'audio' : 'photo', ref.label, ref.busy ? 'uploading…' : ref.kind === 'audio' ? 'voice or sound' : 'photo', () => { refs = refs.filter(r => r !== ref); render(); }, true, ref.thumb);
      item.el.classList.toggle('is-busy', Boolean(ref.busy));
      item.field.onchange = () => { const next = clean(item.field.value); ref.label = next && next.toLowerCase() !== 'video' && !refs.some(r => r !== ref && r.label.toLowerCase() === next.toLowerCase()) ? next : ref.label; item.field.value = ref.label; };
      item.el.querySelector('img,.studio-kind').onclick = () => insert('@' + ref.label);
      list.append(item.el);
    }
    const add = (label, action) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'studio-add'; b.textContent = label; b.onclick = action; list.append(b); };
    if (!sourceActive()) add(mode === 'swap' ? '+ Video to change' : '+ Video as motion reference', () => { if (source) { sourceOn = true; render(); } else $('studio-source-file').click(); });
    add(mode === 'swap' ? '+ Photo' : '+ Photo or audio', () => $('studio-file').click());
    add('+ Create an image', () => { $('studio-image-row').hidden = !$('studio-image-row').hidden; if (!$('studio-image-row').hidden) $('studio-image-prompt').focus(); });
    document.querySelectorAll('[data-studio-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.studioMode === mode)));
    $('studio-prompt').placeholder = placeholders[mode];
    $('studio-example-image').src = examples[mode].src; $('studio-example-image').alt = examples[mode].alt;
    $('studio-example-text').textContent = examples[mode].text + ' ' + disclosure; $('studio-rates').textContent = rates(mode);
    $('studio-duration').hidden = $('studio-aspect').hidden = mode === 'swap'; $('studio-style').hidden = mode !== 'swap';
    const need = credits();
    $('studio-cost').textContent = need ? 'Uses ' + need + (need === 1 ? ' credit' : ' credits') + ' · ' + Math.round(seconds()) + ' s billed · you have ' + paidCredits : mode === 'swap' ? 'Add the video you want to change' : '';
    $('studio-go').textContent = working ? 'Working…' : need ? 'Generate · ' + need + (need === 1 ? ' credit' : ' credits') : 'Generate';
    $('studio-go').disabled = working; $('studio-image-go').disabled = working;
  }
  function insert(text) {
    const area = $('studio-prompt'), at = area.selectionStart ?? area.value.length;
    area.value = area.value.slice(0, at) + text + ' ' + area.value.slice(area.selectionEnd ?? at); area.focus();
  }

  async function upload(file, kind) {
    const form = new FormData(); form.append('kind', kind); form.append('file', file);
    return api('/studio/assets', { method: 'POST', body: form });
  }
  async function addReference(file) {
    const kind = file.type.startsWith('image/') ? 'image' : 'audio';
    if (kind === 'audio' && mode === 'swap') { say('Swap keeps the sound of your video. Switch to Recreate to add a voice or sound.', 'error'); return; }
    if (refs.filter(r => r.kind === kind).length >= (kind === 'image' ? (mode === 'swap' ? config.max_swap_images : config.max_images) : config.max_audio)) { say('You can add up to ' + config.max_images + ' photos and ' + config.max_audio + ' audio files.', 'error'); return; }
    const ref = { kind, label: (kind === 'image' ? 'image' : 'audio') + (++counter), busy: true, asset: null, thumb: kind === 'image' ? URL.createObjectURL(file) : null };
    refs.push(ref); render();
    try { ref.asset = (await upload(file, kind)).asset_id; ref.busy = false; say(''); }
    catch (error) { refs = refs.filter(r => r !== ref); say(error.message, 'error'); }
    render();
  }

  async function wait(id) {
    const deadline = Date.now() + 30 * 60 * 1000; let failures = 0;
    while (Date.now() < deadline) {
      await pause(4000);
      let job;
      try { job = await api('/studio/jobs/' + id); failures = 0; }
      catch (error) { if (error.status === 404 || ++failures >= 8) throw error; continue; }
      if (job.status === 'done') return job;
      if (job.status === 'failed') throw new Error(job.error);
    }
    throw new Error('This is taking longer than expected. Reload the page later to see the result.');
  }
  function needCredits(error) {
    say(error.message + ' Buy credits, then generate again.', 'error'); $('studio-buy').hidden = false;
    track('studio_payment_required', { studio_mode: mode });
  }
  function show(job) {
    $('studio-video').src = job.url; $('studio-download').href = job.url; $('studio-output').hidden = false;
    $('studio-output').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  // The server keeps every job, so a lost reply or a reload can always find it again.
  async function activeJob() { return (await api('/studio/jobs')).jobs.find(job => job.status === 'submitting' || job.status === 'running') || null; }
  async function run(kind, start) {
    working = true; $('studio-buy').hidden = true; render();
    try {
      let job;
      try { job = await start(); }
      catch (error) {
        // No answer, or a generation already running: follow that job instead of starting another.
        if (error.status && error.code !== 'busy') throw error;
        job = await activeJob().catch(() => null);
        if (!job) throw error.status ? error : new Error('The connection dropped. Please try again.');
        say('Generating your video. This usually takes a few minutes.');
      }
      if (job.status === 'failed') throw new Error(job.error);
      const done = await wait(job.job_id);
      track('studio_generation_completed', { studio_mode: kind });
      return done;
    } catch (error) {
      if (error.code === 'payment_required') needCredits(error); else say(error.message, 'error');
      if (error.code !== 'payment_required') track('studio_generation_failed', { studio_mode: kind });
      return null;
    } finally { working = false; await refreshUsage(); render(); }
  }

  $('studio-go').onclick = async () => {
    const prompt = $('studio-prompt').value.trim();
    if (mode === 'swap' && !source) { say('Add the video you want to change.', 'error'); return; }
    if (mode === 'swap' && !refs.some(r => r.kind === 'image')) { say('Add a photo of the person, product or object to put into the video.', 'error'); return; }
    if (mode === 'recreate' && !prompt) { say('Write what the video should show.', 'error'); $('studio-prompt').focus(); return; }
    if (refs.some(r => r.busy)) { say('Wait for your references to finish uploading.', 'error'); return; }
    if (!$('studio-rights').checked) { say('Confirm that you have the right to use what you uploaded.', 'error'); return; }
    track('studio_generate_clicked', { studio_mode: mode, credits: credits() });
    const kind = mode;
    const done = await run(kind, async () => {
      if (sourceActive() && !source.asset) { say('Uploading your video…'); const sent = await upload(source.file, 'video'); source.asset = sent.asset_id; source.seconds = sent.seconds; }
      say('Generating your video. This usually takes a few minutes. You can keep this tab open in the background.');
      const body = { mode: kind, prompt, rights: true, refs: refs.filter(r => kind === 'recreate' || r.kind === 'image').map(r => ({ asset: r.asset, label: r.label })), source: sourceActive() ? source.asset : null };
      if (kind === 'swap') body.style = $('studio-style').value;
      if (kind === 'recreate') { body.duration = Number($('studio-duration').value); body.aspect = $('studio-aspect').value; }
      return api('/studio/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    });
    if (done) { say('Your video is ready. Check it before you publish it.'); show(done); }
  };
  $('studio-image-go').onclick = async () => {
    const prompt = $('studio-image-prompt').value.trim();
    if (!prompt) { $('studio-image-prompt').focus(); return; }
    say('Creating your image…');
    const done = await run('image', () => api('/studio/image', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, aspect: $('studio-aspect').value }) }));
    if (!done) return;
    refs.push({ kind: 'image', label: 'image' + (++counter), busy: false, asset: done.asset_id, thumb: done.url });
    $('studio-image-prompt').value = ''; $('studio-image-row').hidden = true; say('Image added as a reference.'); render();
  };
  $('studio-buy').onclick = () => buyCredit('single');
  $('studio-file').onchange = async event => { const files = [...event.target.files]; event.target.value = ''; for (const file of files) await addReference(file); };
  $('studio-source-file').onchange = event => { const file = event.target.files[0]; event.target.value = ''; setSource(file); };
  $('studio-use-report').onclick = () => {
    // Keep whole lines so a long shot list is not cut in the middle of a shot.
    let text = document.getElementById('result-content').textContent.trim();
    if (text.length > 2000) { text = text.slice(0, 2000); text = text.slice(0, Math.max(text.lastIndexOf('\n'), 1200)).trim(); say('Your report is longer than a prompt can be, so only the first part was used. Edit it down to the shots you want.'); }
    $('studio-prompt').value = text; $('studio-prompt').focus();
  };
  $('studio-duration').onchange = render;
  document.querySelectorAll('[data-studio-mode]').forEach(button => button.onclick = () => { mode = button.dataset.studioMode; sourceOn = false; say(''); render(); });

  // Offer the analyzed upload as the source once a report is on screen.
  new MutationObserver(() => {
    if (!config || root.hidden) return;
    if (reportFile && (!source || source.file !== reportFile)) { source = null; setSource(reportFile).then(() => say('')); }
    render();
  }).observe(document.getElementById('result-content'), { childList: true });

  (async () => {
    try { config = await api('/studio/config'); } catch { return; }
    if (!config.enabled) return;
    for (let s = 5; s <= config.max_seconds; s += 5) $('studio-duration').add(new Option(s + ' s', s, s === 10, s === 10));
    for (const aspect of config.aspects) $('studio-aspect').add(new Option(aspect, aspect));
    root.hidden = false; render();
    // Explain the feature to visitors before they have a report.
    const intro = document.getElementById('studio-intro');
    if (intro) {
      intro.className = 'studio-intro';
      const heading = Object.assign(document.createElement('h2'), { textContent: 'Remake a video with AI' });
      const lead = Object.assign(document.createElement('p'), { textContent: 'After your report, swap a person or product inside your clip, or turn the shot breakdown into a new video. ' + disclosure });
      const grid = Object.assign(document.createElement('div'), { className: 'studio-intro-grid' });
      for (const kind of ['swap', 'recreate']) {
        const card = Object.assign(document.createElement('figure'), { className: 'studio-intro-card' });
        const image = Object.assign(document.createElement('img'), { src: examples[kind].src, alt: examples[kind].alt, loading: 'lazy', width: 1200, height: 493 });
        const caption = document.createElement('figcaption');
        caption.append(Object.assign(document.createElement('h3'), { textContent: examples[kind].title }), Object.assign(document.createElement('p'), { textContent: examples[kind].text }), Object.assign(document.createElement('p'), { className: 'studio-rates', textContent: rates(kind) }));
        card.append(image, caption); grid.append(card);
      }
      const start = Object.assign(document.createElement('a'), { className: 'studio-go', href: '#input-zone', textContent: 'Start with a video' });
      intro.append(heading, lead, grid, start); intro.hidden = false;
    }
    track('studio_available');
    // After a reload, pick up a generation that is still running, or show the last finished video.
    let jobs = [];
    try { jobs = (await api('/studio/jobs')).jobs; } catch { return; }
    const pending = jobs.find(job => job.status === 'submitting' || job.status === 'running');
    const last = jobs.find(job => job.kind !== 'image');
    if (pending) {
      working = true; render(); say('Reconnecting to your generation…');
      try { const done = await wait(pending.job_id); if (done.kind !== 'image') { say('Your video is ready. Check it before you publish it.'); show(done); } else say(''); }
      catch (error) { say(error.message, 'error'); }
      finally { working = false; await refreshUsage(); render(); }
    } else if (last && last.status === 'done') {
      $('studio-video').src = last.url; $('studio-download').href = last.url; $('studio-output').hidden = false; say('Your last generated video is below.');
    }
  })();
})();
