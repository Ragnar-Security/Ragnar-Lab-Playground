(function () {
  const root = document.getElementById('koth-root');
  if (!root) return;

  let cy = null;
  let lastScores = {};
  let countdownTimer = null;
  let countdownEndsAt = null; // Date | null
  let countdownGameOver = false;

  async function fetchJSON(url) {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (r.status === 204) return null;
    if (!r.ok) throw new Error(`${url} ${r.status}`);
    return r.json();
  }

  function setStatus(text, live) {
    document.getElementById('koth-status-text').textContent = text;
    const dot = document.getElementById('koth-status-dot');
    dot.classList.toggle('koth-live', !!live);
  }

  function setIter(state) {
    document.getElementById('koth-iter').textContent =
      state ? `ITER ${String(state.game.scoring_iteration).padStart(4, '0')}` : 'ITER 0000';
  }

  function pad2(n) { return String(n).padStart(2, '0'); }
  function nowHMS() {
    const d = new Date();
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function clearCountdown() {
    if (countdownTimer !== null) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  function setTimerDigits(text, modifier) {
    const el = document.getElementById('koth-timer-digits');
    if (!el) return;
    el.textContent = text;
    el.classList.remove('koth-timer-unlimited', 'koth-timer-over', 'koth-timer-warn');
    if (modifier) el.classList.add(modifier);
  }

  function tickCountdown() {
    if (countdownGameOver) {
      setTimerDigits('GAME OVER', 'koth-timer-over');
      return;
    }
    if (countdownEndsAt === null) {
      setTimerDigits('UNLIMITED', 'koth-timer-unlimited');
      return;
    }
    const now = Date.now();
    let remainingMs = countdownEndsAt.getTime() - now;
    if (remainingMs <= 0) {
      // Clamp at 0, then flip to GAME OVER
      setTimerDigits('00:00', 'koth-timer-over');
      countdownGameOver = true;
      clearCountdown();
      return;
    }
    const totalSec = Math.floor(remainingMs / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    const text = h >= 1
      ? `${pad2(h)}:${pad2(m)}:${pad2(s)}`
      : `${pad2(m)}:${pad2(s)}`;
    const warn = remainingMs <= 5 * 60 * 1000;
    setTimerDigits(text, warn ? 'koth-timer-warn' : null);
  }

  function updateCountdownFromState(state) {
    const card = document.getElementById('koth-timer');
    if (!card) return;
    if (!state || !state.game) {
      card.hidden = true;
      countdownEndsAt = null;
      countdownGameOver = false;
      clearCountdown();
      return;
    }
    card.hidden = false;
    // Reset game-over state on every fresh state — admin may have extended.
    countdownGameOver = false;
    const endsAt = state.game.ends_at;
    countdownEndsAt = endsAt ? new Date(endsAt) : null;
    // Restart interval cleanly so we never double-register.
    clearCountdown();
    tickCountdown();
    countdownTimer = setInterval(tickCountdown, 1000);
  }

  function markGameEnded() {
    countdownGameOver = true;
    clearCountdown();
    tickCountdown();
  }

  function renderScoreboard(state) {
    const tbody = document.querySelector('#koth-scores tbody');
    tbody.innerHTML = '';
    if (state.hidden.scoreboard) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 3;
      const div = document.createElement('div');
      div.className = 'koth-hidden';
      div.textContent = 'HIDDEN BY INSTRUCTOR';
      td.appendChild(div);
      tr.appendChild(td); tbody.appendChild(tr);
      return;
    }
    const teams = state.teams.map(t => ({ ...t, score: state.scores[t.id] || 0 }));
    teams.sort((a, b) => b.score - a.score);
    teams.forEach((t, idx) => {
      const tr = document.createElement('tr');
      const flash = (lastScores[t.id] !== undefined && lastScores[t.id] !== t.score);
      if (flash) tr.classList.add('koth-flash');
      const rank = document.createElement('span');
      rank.className = idx === 0 ? 'koth-rank koth-rank-1' : 'koth-rank';
      rank.textContent = String(idx + 1).padStart(2, '0');
      const tdRank = document.createElement('td');
      tdRank.appendChild(rank);
      const swatch = document.createElement('span');
      swatch.className = 'koth-swatch';
      swatch.style.background = t.color;
      const tdTeam = document.createElement('td');
      tdTeam.appendChild(swatch);
      tdTeam.appendChild(document.createTextNode(t.display_name));
      const tdScore = document.createElement('td');
      tdScore.className = 'koth-num';
      tdScore.textContent = t.score.toFixed(1);
      tr.append(tdRank, tdTeam, tdScore);
      tbody.appendChild(tr);
      lastScores[t.id] = t.score;
    });
  }

  function renderMap(state) {
    const mapDiv = document.getElementById('cy');
    if (state.hidden.network_map) {
      mapDiv.innerHTML = '<div class="koth-hidden">HIDDEN BY INSTRUCTOR</div>';
      cy = null;
      return;
    }
    if (mapDiv.firstChild && mapDiv.firstChild.classList && mapDiv.firstChild.classList.contains('koth-hidden')) {
      mapDiv.innerHTML = '';
    }
    const teamColor = Object.fromEntries(state.teams.map(t => [t.id, t.color]));
    const elements = state.boxes.map(b => ({
      data: {
        id: 'b' + b.id,
        label: b.display_name || b.name,
        color: teamColor[b.owner_team_id] || '#3a3a45',
        owned: b.owner_team_id ? 'yes' : 'no',
      },
      position: b.position || undefined,
    }));
    if (cy === null) {
      cy = cytoscape({
        container: mapDiv,
        elements,
        layout: { name: 'preset' },
        style: [
          { selector: 'node', style: {
            'background-color': 'data(color)',
            'border-color': '#0a0a0c',
            'border-width': 2,
            'shape': 'round-rectangle',
            'width': 60, 'height': 36,
            'label': 'data(label)',
            'color': '#e5e5e8',
            'font-family': 'JetBrains Mono, monospace',
            'font-size': 10,
            'font-weight': 700,
            'text-valign': 'center',
            'text-halign': 'center',
            'text-outline-color': '#0a0a0c',
            'text-outline-width': 2,
          }},
          { selector: 'node[owned = "yes"]', style: {
            'border-color': 'data(color)',
            'border-width': 3,
          }},
          { selector: 'edge', style: {
            'line-color': '#2a2a32',
            'width': 1,
          }},
        ],
      });
    } else {
      // Update in place so a 12 s tick doesn't tear down the graph or reset
      // the user's pan/zoom. Layout only reruns when the node set changed.
      let changed = false;
      const wanted = new Set(elements.map(el => el.data.id));
      cy.nodes().forEach(n => {
        if (!wanted.has(n.id())) { n.remove(); changed = true; }
      });
      elements.forEach(el => {
        const n = cy.getElementById(el.data.id);
        if (n.empty()) {
          cy.add(el);
          changed = true;
        } else {
          n.data('color', el.data.color);
          n.data('owned', el.data.owned);
          n.data('label', el.data.label);
        }
      });
      if (changed) cy.layout({ name: 'preset' }).run();
    }
  }

  // One row per way a team can prove it owns a box, filtered by the
  // game's claim_mode. The beacon costs the server one extra local TCP
  // connect per box per tick; the file costs one Proxmox API call per
  // box per tick and is what the Windows templates use.
  function claimRows(mode, token, port) {
    const rows = [];
    if (mode === 'beacon' || mode === 'both') {
      rows.push(['LINUX', `while :; do echo '${token}' | nc -lN -p ${port}; done &`]);
      rows.push(['WINDOWS',
        `powershell -c "$l=[Net.Sockets.TcpListener]${port};$l.Start();` +
        `while($true){$c=$l.AcceptTcpClient();$w=New-Object IO.StreamWriter($c.GetStream());` +
        `$w.WriteLine('${token}');$w.Flush();$c.Close()}"`]);
    }
    if (mode === 'file' || mode === 'both') {
      rows.push(['LINUX FILE', `echo '${token}' > /koth/claim`]);
      rows.push(['WINDOWS FILE', `echo ${token} > C:\\koth\\claim`]);
    }
    return rows;
  }

  function renderMyTeam(team, state) {
    const card = document.getElementById('koth-myteam');
    const body = document.getElementById('koth-myteam-body');
    if (!team) { card.hidden = true; return; }
    card.hidden = false;
    const game = (state && state.game) || {};
    const scanInterval = game.scan_interval_sec || 12;
    const mode = game.claim_mode || 'file';
    const port = game.claim_port || 31337;
    const token = team.submission_token;
    const rowsHtml = claimRows(mode, token, port).map(([label, cmd]) => `
        <div class="koth-claim-row">
          <span class="koth-claim-os">${escapeHtml(label)}</span>
          <pre>${escapeHtml(cmd)}</pre>
        </div>`).join('');
    const help = mode === 'beacon'
      ? `Keep the listener running — the server checks every ~${scanInterval}s and a dead listener loses the box.`
      : mode === 'both'
        ? `Checked every ~${scanInterval}s. A live listener beats the file; the file is the fallback.`
        : `Server polls every ~${scanInterval} seconds. Latest write wins.`;
    body.innerHTML = `
      <div class="koth-myteam-info">
        <div>
          <label>TEAM</label>
          <div><span class="koth-swatch" style="background:${escapeAttr(team.color)}"></span>${escapeHtml(team.display_name)}</div>
        </div>
        <div>
          <label>SUBMISSION TOKEN</label>
          <input value="${escapeAttr(token)}" readonly onclick="this.select()">
        </div>
      </div>
      <div class="koth-claim-block">
        <label>CLAIM A BOX (run on the box itself)</label>
        ${rowsHtml}
        <div class="koth-claim-help">${escapeHtml(help)}</div>
      </div>
      <div class="koth-flag-block">
        <label>SUBMIT FLAG</label>
        <form id="koth-flag-form" class="koth-flag-form" autocomplete="off">
          <input id="koth-flag-input" type="text" placeholder="FLAG-UUID" spellcheck="false" autocomplete="off">
          <button id="koth-flag-submit" type="submit">SUBMIT FLAG</button>
          <span id="koth-flag-status" class="koth-flag-status" aria-live="polite"></span>
        </form>
      </div>
    `;
    wireFlagForm();
  }

  function setFlagStatus(kind, text) {
    const el = document.getElementById('koth-flag-status');
    if (!el) return;
    el.classList.remove('koth-flag-ok', 'koth-flag-warn', 'koth-flag-bad');
    if (kind === 'ok') el.classList.add('koth-flag-ok');
    else if (kind === 'warn') el.classList.add('koth-flag-warn');
    else if (kind === 'bad') el.classList.add('koth-flag-bad');
    el.textContent = text || '';
  }

  function wireFlagForm() {
    const form = document.getElementById('koth-flag-form');
    if (!form) return;
    const input = document.getElementById('koth-flag-input');
    const btn = document.getElementById('koth-flag-submit');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const value = (input.value || '').trim();
      if (!value) return;
      btn.disabled = true;
      setFlagStatus(null, '... submitting');
      try {
        const r = await fetch('/koth/api/flags/submit', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value }),
        });
        let data = {};
        try { data = await r.json(); } catch (_) { data = {}; }
        if (r.status === 403 || r.status === 404) {
          setFlagStatus('bad', `✗ ${data.error || 'rejected'}`);
        } else if (r.ok && data.accepted === true) {
          const pts = data.points != null ? data.points : '?';
          const name = data.flag_name || 'flag';
          setFlagStatus('ok', `✓ +${pts} (${name})`);
          input.value = '';
        } else if (r.ok && data.accepted === false && data.reason === 'duplicate') {
          setFlagStatus('warn', '⚠ Already submitted by your team');
        } else if (r.ok && data.accepted === false) {
          setFlagStatus('bad', '✗ Not a valid flag');
        } else {
          setFlagStatus('bad', `✗ Unexpected response (${r.status})`);
        }
      } catch (e) {
        setFlagStatus('bad', `✗ Network error: ${e.message || String(e)}`);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function pushEvent(msg) {
    const ul = document.getElementById('koth-events-list');
    const li = document.createElement('li');
    li.innerHTML = `<span class="koth-evt-time">${nowHMS()}</span><span class="koth-evt-msg">${escapeHtml(msg)}</span>`;
    ul.insertBefore(li, ul.firstChild);
    while (ul.children.length > 40) ul.removeChild(ul.lastChild);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, ch =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  }
  function escapeAttr(s) { return escapeHtml(s); }

  function applyState(state) {
    renderScoreboard(state);
    renderMap(state);
    setIter(state);
    updateCountdownFromState(state);
  }

  // The one place that fetches full state over HTTP. Callers: bootstrap, the
  // admin scan path (admin data is deliberately not on the shared bus), and
  // the degraded-polling fallback. Never call it per-viewer on a scan: that
  // thundering herd exhausted the DB pool.
  async function loadState() {
    const state = await fetchJSON('/koth/api/state');
    if (state) applyState(state);
    else updateCountdownFromState(null);
    return state;
  }

  async function bootstrap() {
    let state = null;
    try {
      state = await loadState();
      if (state) pushEvent(`game ${state.game.name} loaded — ${state.teams.length} teams`);
      else pushEvent('no active game');
    } catch (e) {
      pushEvent(`error loading state: ${e.message}`);
    }
    try {
      const my = await fetchJSON('/koth/api/my-team');
      renderMyTeam(my && my.team, state);
    } catch (_) { /* not on a team */ }
  }

  // Module-level handle so we can close on unload AND so re-entering
  // startStream() (e.g. after a reconnect path) doesn't leak the previous
  // EventSource. Browsers don't gc an EventSource that still has an open
  // socket, so the server-side FD lingers minutes past the user being gone
  // unless we explicitly .close() it.
  let activeStream = null;

  const IS_ADMIN = root.dataset.isAdmin === '1';
  const POLL_AFTER_ERRORS = 3;
  const POLL_INTERVAL_MS = 20000;
  let consecutiveErrors = 0;
  let pollTimer = null;

  function stopPolling() {
    if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; }
  }

  function startPolling() {
    if (pollTimer !== null) return;
    // Poll once right away. The stream just died, so the board on screen
    // is already stale; waiting a full interval to notice is the same
    // frozen-scoreboard symptom this fallback exists to avoid.
    loadState().catch(() => {});
    pollTimer = setInterval(() => { loadState().catch(() => {}); }, POLL_INTERVAL_MS);
  }

  function stopStream() {
    stopPolling();
    if (activeStream) {
      try { activeStream.close(); } catch (_) {}
      activeStream = null;
    }
  }

  function startStream() {
    stopStream();
    consecutiveErrors = 0;
    const es = new EventSource('/koth/api/stream');
    activeStream = es;
    es.onopen = () => {
      consecutiveErrors = 0;
      stopPolling();
      setStatus('LIVE', true);
    };
    es.onerror = () => {
      setStatus('RECONNECTING', false);
      consecutiveErrors += 1;
      // An EventSource whose response is not 200 text/event-stream FAILS
      // the connection: one error event, readyState CLOSED, and it never
      // retries. Two routine cases land there — an expired Flask session
      // (401 JSON from @login_required) and the ~10s during a redeploy
      // when Caddy answers 502 before it re-resolves the container. On
      // those, waiting for a third error would mean waiting forever and
      // the tab would sit on a frozen scoreboard until the student
      // reloaded, which is exactly what this fallback exists to prevent.
      if (es.readyState === EventSource.CLOSED ||
          consecutiveErrors >= POLL_AFTER_ERRORS) startPolling();
    };
    // scan carries the complete student-visible state; render it directly.
    es.addEventListener('scan', (e) => {
      if (IS_ADMIN) {
        loadState().catch(() => {});
        return;
      }
      try { applyState(JSON.parse(e.data)); } catch (_) {}
    });
    // claim/flag only log; the scan that follows carries ownership and scores.
    es.addEventListener('claim', (e) => {
      let msg = 'box claimed';
      try { const d = JSON.parse(e.data); msg = `${d.team_color || 'team'} claimed ${d.box_name || 'box'}`; } catch (_) {}
      pushEvent(msg);
    });
    es.addEventListener('flag', (e) => {
      let msg = 'flag accepted';
      try { const d = JSON.parse(e.data); msg = `flag "${d.flag_name}" accepted (+${d.points})`; } catch (_) {}
      pushEvent(msg);
    });
    es.addEventListener('game_ended', () => {
      markGameEnded();
      pushEvent('game ended');
    });
  }

  // Close the EventSource the moment the page becomes hidden or unloads
  // so the server can release the SSE socket immediately. pagehide is
  // more reliable than beforeunload (mobile Safari, bfcache).
  window.addEventListener('pagehide', stopStream);
  // pagehide fires for bfcache freezes too, so pair it with pageshow:
  // when the user comes back via browser back/forward, restart the
  // stream + refetch state so the scoreboard reflects current reality
  // instead of the frozen snapshot.
  window.addEventListener('pageshow', (e) => {
    if (e.persisted && !activeStream) {
      bootstrap().then(startStream);
    }
  });

  setStatus('CONNECTING', false);
  bootstrap().then(startStream);
})();
