// Relay CRM - small progressive enhancements (no external libraries).
(function () {
  const csrf = document.querySelector('meta[name=csrf]')?.content;

  // Expand long email bodies on the timeline
  document.querySelectorAll('.tl .body').forEach(b => {
    if (b.scrollHeight > b.clientHeight + 4) {
      const a = document.createElement('a');
      a.href = '#'; a.className = 'more'; a.textContent = 'Show more';
      a.onclick = e => { e.preventDefault(); b.classList.toggle('open'); a.textContent = b.classList.contains('open') ? 'Show less' : 'Show more'; };
      b.after(a);
    }
  });

  // Simple tabs: <div class=tabs data-tabs> <button data-tab="x">  + <div data-panel="x">
  document.querySelectorAll('[data-tabs]').forEach(t => {
    const btns = t.querySelectorAll('[data-tab]');
    btns.forEach(b => b.addEventListener('click', () => {
      btns.forEach(x => x.classList.toggle('on', x === b));
      document.querySelectorAll('[data-panel]').forEach(p => p.classList.toggle('panel-hidden', p.dataset.panel !== b.dataset.tab));
    }));
  });

  // Confirm destructive actions
  document.querySelectorAll('form[data-confirm]').forEach(f => f.addEventListener('submit', e => {
    if (!confirm(f.dataset.confirm)) e.preventDefault();
  }));

  // Auto-submit filter selects
  document.querySelectorAll('[data-autosubmit]').forEach(el => el.addEventListener('change', () => el.form.submit()));

  // Pipeline drag & drop
  let dragged = null;
  document.querySelectorAll('.opp[draggable]').forEach(c => {
    c.addEventListener('dragstart', e => { dragged = c; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', c.dataset.id); });
  });
  document.querySelectorAll('.col[data-stage]').forEach(col => {
    col.addEventListener('dragover', e => { e.preventDefault(); col.classList.add('drop'); });
    col.addEventListener('dragleave', () => col.classList.remove('drop'));
    col.addEventListener('drop', async e => {
      e.preventDefault(); col.classList.remove('drop');
      if (!dragged) return;
      const stage = col.dataset.stage;
      const r = await fetch(`/opps/${dragged.dataset.id}/stage`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF': csrf },
        body: JSON.stringify({ stage })
      });
      if (r.ok) location.reload(); else alert('Could not move that opportunity.');
    });
  });

  // Microsoft 365 device-code sign in
  const loginBtn = document.getElementById('m365-login');
  if (loginBtn) {
    const box = document.getElementById('m365-flow');
    const poll = () => fetch('/m365/status').then(r => r.json()).then(s => {
      if (s.status === 'pending') return setTimeout(poll, 3000);
      if (s.status === 'done') { box.innerHTML = `<div class="flash ok">Signed in as ${s.account || 'your account'}. Reloading...</div>`; setTimeout(() => location.reload(), 1200); }
      if (s.status === 'error') box.innerHTML = `<div class="flash error">Sign-in failed: ${s.error || 'unknown error'}</div>`;
    });
    loginBtn.addEventListener('click', async () => {
      loginBtn.disabled = true; box.innerHTML = '<p class="muted">Contacting Microsoft...</p>';
      const r = await fetch('/m365/login', { method: 'POST', headers: { 'X-CSRF': csrf } });
      const s = await r.json();
      loginBtn.disabled = false;
      if (s.status === 'error') { box.innerHTML = `<div class="flash error">${s.error}</div>`; return; }
      box.innerHTML = `<div class="callout"><p><b>1.</b> Open <a href="${s.verification_uri}" target="_blank" rel="noopener">${s.verification_uri}</a> in your browser.</p>
        <p><b>2.</b> Enter this code:</p><p><span class="code">${s.user_code}</span>
        <button type="button" class="btn sm" id="copy-code">Copy code</button></p>
        <p class="muted small">Then sign in with your work account as usual (MFA / PIV / CAC all work). This page updates by itself.</p></div>`;
      document.getElementById('copy-code').onclick = () => navigator.clipboard?.writeText(s.user_code);
      poll();
    });
  }

  // Insert an email template
  const tplSel = document.getElementById('tpl');
  if (tplSel) tplSel.addEventListener('change', () => {
    if (!tplSel.value) return;
    const u = new URL(location.href); u.searchParams.set('template', tplSel.value); location.href = u.toString();
  });
})();
