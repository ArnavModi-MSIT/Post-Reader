const $ = id => document.getElementById(id);
let token = '', selected = null, currentFile = null, initialized = false, polling = false;
const defaults = {linkedin:'https://www.linkedin.com/jobs/search/?keywords=data%20analyst&location=India',internshala:'https://internshala.com/internships/work-from-home-internships/'};
const sourceLabel = source => source === 'internshala' ? 'Internshala' : 'LinkedIn';
function detectSource() {
  try {
    const host = new URL($('search-url').value.trim()).hostname;
    if(/^(www\.)?internshala\.com$/.test(host)) $('source').value = 'internshala';
    else if(/^(www\.)?linkedin\.com$/.test(host)) $('source').value = 'linkedin';
    defaults[$('source').value] = $('search-url').value.trim();
  } catch (_) { /* The API validates incomplete or unsupported URLs on submit. */ }
}
const number = n => Number(n || 0).toLocaleString('en-IN');
const date = value => value ? new Date(value).toLocaleString('en-IN', {timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}) : '—';
const notice = message => { $('notice').textContent = message || ''; $('notice').hidden = !message; };
const text = (tag, value, cls) => { const el = document.createElement(tag); el.textContent = value || ''; if(cls) el.className = cls; return el; };
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Reader-Token':token},body:JSON.stringify(body)});
  const value = await response.json();
  if(!response.ok) throw new Error(value.error || 'Request failed');
  return value;
}
async function refresh() {
  if(polling) return;
  polling = true;
  try {
    const data = await api('/api/state' + (selected ? '?file=' + encodeURIComponent(selected) : ''));
    token = data.token; currentFile = data.file;
    if(!initialized) {
      $('search-url').value = data.settings.url || '';
      $('limit').value = data.settings.limit || 1000;
      $('delay').value = data.settings.delay || 3;
      detectSource();
      initialized = true;
    }
    for(const key of ['saved','attempted','errors','pending','target','skipped']) $(key).textContent = number(data[key]);
    $('status').textContent = data.status; $('status').className = 'status ' + data.status;
    const percent = data.target ? Math.min(100, data.saved / data.target * 100) : 0;
    $('progress').value = percent; $('percent').textContent = percent.toFixed(1) + '% collected';
    $('updated').textContent = data.updated_at ? 'Updated ' + date(data.updated_at) : 'Waiting for a run';
    $('run-message').textContent = data.message || (data.running ? 'Collecting job descriptions. Progress is saved automatically.' : 'Ready to collect. Your saved jobs stay on this computer.');
    $('output-name').textContent = data.file || 'No result selected';
    $('run-source').textContent = sourceLabel(data.source);
    $('download').href = data.file ? '/api/download?file=' + encodeURIComponent(data.file) : '#';
    $('download').classList.toggle('disabled', !data.file); $('download').setAttribute('aria-disabled', String(!data.file));
    $('start').disabled = data.running; $('stop').disabled = !data.running || data.status === 'stopping';
    $('follow').hidden = !selected;
    $('jobs').replaceChildren();
    if(!data.jobs.length) $('jobs').append(text('p','Saved jobs will appear here.','empty'));
    for(const job of data.jobs) {
      const row = text('div','','job-row');
      row.append(text('span',(job.company || 'JR').slice(0,2).toUpperCase(),'job-icon'));
      const info = text('div','','job-text'); info.append(text('strong',job.title),text('small',job.company)); row.append(info);
      const view = text('button','View description','text-button'); view.type = 'button';
      view.addEventListener('click',()=>showJob(data.file,job.job_id)); row.append(view); $('jobs').append(row);
    }
    $('history').replaceChildren();
    for(const run of data.history) {
      const row = document.createElement('tr'); if(run.file === data.file) row.className = 'selected';
      row.append(text('td',run.file),text('td',sourceLabel(run.source)),text('td',number(run.count)),text('td',number(run.errors)),text('td',run.status,'history-status'),text('td',date(run.updated_at)));
      const cell = document.createElement('td'), view = text('button','Inspect ↗','text-button'); view.type = 'button';
      view.addEventListener('click',()=>{ selected = run.file; refresh(); }); cell.append(view); row.append(cell); $('history').append(row);
    }
    if(!data.history.length) { const row=document.createElement('tr'),cell=text('td','No saved runs yet.'); cell.colSpan=7; row.append(cell); $('history').append(row); }
  } catch(error) { notice(error.message); }
  finally { polling = false; }
}
async function showJob(file,id) {
  try {
    const job = await api('/api/job?file=' + encodeURIComponent(file) + '&id=' + encodeURIComponent(id));
    $('detail-title').textContent = job.title; $('detail-company').textContent = job.company;
    $('detail-meta').textContent = job.location_and_posting_info || '';
    $('detail-description').textContent = job.description || '';
    const link = new URL(job.link);
    $('detail-link').href = link.protocol === 'https:' && /^(www\.)?(linkedin|internshala)\.com$/.test(link.hostname) ? link.href : '#';
    $('detail-link').textContent = 'Open ' + (/internshala\.com$/.test(link.hostname) ? 'Internshala' : 'LinkedIn') + ' listing ↗';
    $('job-dialog').showModal();
  } catch(error) { notice(error.message); }
}
$('setup').addEventListener('submit',async event=>{
  event.preventDefault(); notice(''); $('start').disabled = true;
  try {
    await api('/api/start',{url:$('search-url').value.trim(),limit:Number($('limit').value),delay:Number($('delay').value),fresh:!$('resume').checked});
    selected = null; await refresh();
  } catch(error) { notice(error.message); $('start').disabled = false; }
});
$('stop').addEventListener('click',async()=>{ try { notice(''); await api('/api/stop',{}); await refresh(); } catch(error) { notice(error.message); } });
$('follow').addEventListener('click',()=>{ selected=null; refresh(); });
$('close-dialog').addEventListener('click',()=>$('job-dialog').close());
$('download').addEventListener('click',event=>{if(!currentFile) event.preventDefault();});
$('source').addEventListener('change',()=>{ $('search-url').value = defaults[$('source').value]; });
$('search-url').addEventListener('input',detectSource);
refresh(); setInterval(refresh,2000);
