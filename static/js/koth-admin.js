(function () {
  const root = document.getElementById('koth-admin-root') || document.querySelector('.koth-admin-root');
  if (!root) return;

  let currentGameId = null;
  let deployingPollTimer = null;
  // Cached list of {id, name} for the current game's boxes, used to
  // populate the box-scope dropdown when editing a flag inline. Refreshed
  // every time renderFlagEditor runs so it stays in sync with deploys.
  // Kept in closure scope (not on window) so other scripts on the same
  // page can't clobber it.
  let boxesForCurrentGame = [];
  // Live countdown timer for active games with a duration set. Cleared on
  // every re-render so we never accumulate stacked intervals after a poll.
  let countdownTimer = null;
  function cancelCountdown() {
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  }

  // Format an ISO timestamp the way the rest of the brutalist UI expects:
  // local 24h, no seconds. Returns '' on bad input so callers can hide the row.
  function fmtLocalTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString();
  }

  // Format a duration in seconds as "1h 23m 04s" / "23m 04s" / "04s".
  function fmtCountdown(totalSec) {
    if (totalSec <= 0) return '00s';
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    const ss = String(s).padStart(2, '0');
    if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m ${ss}s`;
    if (m > 0) return `${m}m ${ss}s`;
    return `${ss}s`;
  }

  // Render the DURATION / ENDS AT / TIME LEFT block under the game summary.
  // Caller is responsible for cancelling any prior countdown before invoking.
  function timeBlockHtml(g) {
    const durTxt = (g.duration_minutes == null)
      ? 'UNLIMITED'
      : `${g.duration_minutes} MIN`;
    let html = `<div class="koth-time-block">`;
    html += `<div>DURATION: <strong>${escapeHtml(durTxt)}</strong></div>`;
    if (g.ends_at) {
      html += `<div>ENDS AT: <strong>${escapeHtml(fmtLocalTime(g.ends_at))}</strong></div>`;
      if (g.status === 'active') {
        html += `<div>TIME LEFT: <strong class="koth-time-left" id="koth-time-left">…</strong></div>`;
      }
    }
    html += `</div>`;
    return html;
  }

  function startCountdown(endsAtIso) {
    cancelCountdown();
    if (!endsAtIso) return;
    const endMs = new Date(endsAtIso).getTime();
    if (isNaN(endMs)) return;
    const tick = () => {
      const el = document.getElementById('koth-time-left');
      if (!el) { cancelCountdown(); return; }  // DOM was re-rendered out from under us
      const remainSec = Math.max(0, Math.round((endMs - Date.now()) / 1000));
      el.textContent = fmtCountdown(remainSec);
      if (remainSec <= 0) cancelCountdown();
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
  }

  // Wrap an async button click so the button shows a busy state and rejects
  // double-clicks. The label is shown while the action runs; the button
  // re-enables when the action settles (the caller usually triggers a
  // re-render that replaces the button entirely).
  async function withBusy(btn, busyLabel, fn) {
    if (!btn || btn.disabled) return;
    const originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = busyLabel;
    btn.classList.add('koth-btn--busy');
    try {
      return await fn();
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
      btn.classList.remove('koth-btn--busy');
    }
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (r.status === 204) return null;
    let body = null;
    try { body = await r.json(); } catch (_) {}
    if (!r.ok) {
      const err = new Error((body && body.error) || `${path} ${r.status}`);
      // Attach the parsed body + status so callers can react to structured
      // fields (e.g. `force_required: true` on a 409) without string-
      // matching the human-readable message text.
      err.status = r.status;
      err.body = body || {};
      throw err;
    }
    return body;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, ch =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  }

  async function loadDeploymentGroups() {
    const r = await api('/templates');
    const sel = document.getElementById('koth-dg-select');
    sel.innerHTML = '<option value="">— select —</option>';
    for (const g of (r.deployment_groups || []).filter(g => g.koth_mode)) {
      const opt = document.createElement('option');
      opt.value = g.display_name;
      opt.textContent = g.display_name;
      sel.appendChild(opt);
    }
    if (sel.children.length === 1) {
      const opt = document.createElement('option');
      opt.disabled = true;
      opt.textContent = 'No koth_mode groups in templates.yaml';
      sel.appendChild(opt);
    }
  }

  async function loadTeamPicker() {
    const r = await api('/admin/groups');
    const div = document.getElementById('koth-team-picker');
    div.innerHTML = '';
    if (!r.groups || r.groups.length === 0) {
      div.innerHTML = '<div class="koth-empty">no student groups defined</div>';
      return;
    }
    for (const g of r.groups) {
      const wrap = document.createElement('label');
      wrap.innerHTML = `<input type="checkbox" value="${escapeHtml(g.id)}"> ${escapeHtml(g.display_name)}`;
      div.appendChild(wrap);
    }
  }

  function statusTag(status) {
    return `<span class="koth-tag-status koth-tag-status--${escapeHtml(status)}">${escapeHtml(status)}</span>`;
  }

  const cardHeader = (title) => `
    <div class="koth-card-header">
      <span class="koth-bracket-l">[</span>
      <span class="koth-card-title">${title}</span>
      <span class="koth-bracket-r">]</span>
    </div>`;

  function gameSummary(g) {
    return `
      <dl class="koth-game-summary">
        <div><dt>NAME</dt><dd>${escapeHtml(g.name)}</dd></div>
        <div><dt>STATUS</dt><dd>${statusTag(g.status)}</dd></div>
        <div><dt>DEPLOYMENT GROUP</dt><dd>${escapeHtml(g.deployment_group_name)}</dd></div>
        <div><dt>ENTRY VLAN</dt><dd>${escapeHtml(g.entry_vlan_tag)}</dd></div>
        <div><dt>INTERNAL VLAN</dt><dd>${escapeHtml(g.internal_vlan_tag)}</dd></div>
        <div><dt>ITERATION</dt><dd>${escapeHtml(g.scoring_iteration)}</dd></div>
      </dl>`;
  }

  // Editable `scan_interval_sec` — the cadence of one full tick (capture
  // claims, probe services, recompute scores). Editable in both draft and
  // active states because admins want to dial it in before deploy AND
  // during gameplay.
  function pollIntervalControlHtml(g) {
    return `
      <div class="koth-poll-row">
        <label for="koth-poll-input">POLL INTERVAL (s)</label>
        <input id="koth-poll-input" type="number" min="1" max="3600"
               value="${escapeHtml(g.scan_interval_sec)}">
        <button class="koth-btn" id="koth-poll-save"
                title="One tick = capture claims + probe services + score. Lower = snappier claims, higher = less load.">SAVE</button>
        <span class="koth-poll-status" id="koth-poll-status"></span>
      </div>
    `;
  }

  // How the server captures box ownership. BEACON is one extra local TCP
  // connect per box per tick and zero Proxmox API calls; FILE costs one
  // guest-agent call per box per tick and is what the Windows templates
  // use. BOTH takes the beacon when present.
  function claimModeControlHtml(g) {
    const opt = (v, label) =>
      `<option value="${v}"${g.claim_mode === v ? ' selected' : ''}>${label}</option>`;
    return `
      <div class="koth-poll-row">
        <label for="koth-claim-mode">CLAIM MODE</label>
        <select id="koth-claim-mode">
          ${opt('both', 'BOTH (beacon, file fallback)')}
          ${opt('beacon', 'BEACON (TCP, no Proxmox calls)')}
          ${opt('file', 'FILE (guest agent)')}
        </select>
        <input id="koth-claim-port" type="number" min="1024" max="65535"
               value="${escapeHtml(g.claim_port)}" title="Port the claim beacon listens on">
        <button class="koth-btn" id="koth-claim-save"
                title="BEACON costs zero Proxmox API calls per tick; FILE costs one per box per tick.">SAVE</button>
        <span class="koth-poll-status" id="koth-claim-status"></span>
      </div>
    `;
  }

  function attachPollIntervalHandler(gameId) {
    const btn = document.getElementById('koth-poll-save');
    const input = document.getElementById('koth-poll-input');
    const status = document.getElementById('koth-poll-status');
    if (!btn || !input) return;
    btn.onclick = (ev) => {
      const n = parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < 1 || n > 3600) {
        if (status) { status.textContent = '✗ must be 1-3600'; status.className = 'koth-poll-status koth-poll-bad'; }
        return;
      }
      withBusy(ev.currentTarget, 'SAVING…', async () => {
        try {
          await api(`/koth/api/games/${gameId}`,
            { method: 'PATCH', body: JSON.stringify({ scan_interval_sec: n }) });
          if (status) { status.textContent = `✓ ${n}s`; status.className = 'koth-poll-status koth-poll-ok'; }
          // Re-render so other consumers (e.g. the deploying status banner)
          // see the new value. Active games already loop via loadCurrentGame.
          await loadCurrentGame();
        } catch (e) {
          if (status) { status.textContent = '✗ ' + e.message; status.className = 'koth-poll-status koth-poll-bad'; }
        }
      });
    };
  }

  function attachClaimModeHandler(gameId) {
    const btn = document.getElementById('koth-claim-save');
    const sel = document.getElementById('koth-claim-mode');
    const portInput = document.getElementById('koth-claim-port');
    const status = document.getElementById('koth-claim-status');
    if (!btn || !sel || !portInput) return;
    btn.onclick = (ev) => {
      const port = parseInt(portInput.value, 10);
      if (!Number.isFinite(port) || port < 1024 || port > 65535) {
        if (status) { status.textContent = '✗ port must be 1024-65535'; status.className = 'koth-poll-status koth-poll-bad'; }
        return;
      }
      withBusy(ev.currentTarget, 'SAVING…', async () => {
        try {
          await api(`/koth/api/games/${gameId}`, {
            method: 'PATCH',
            body: JSON.stringify({ claim_mode: sel.value, claim_port: port }),
          });
          if (status) { status.textContent = `✓ ${sel.value}:${port}`; status.className = 'koth-poll-status koth-poll-ok'; }
          await loadCurrentGame();
        } catch (e) {
          if (status) { status.textContent = '✗ ' + e.message; status.className = 'koth-poll-status koth-poll-bad'; }
        }
      });
    };
  }

  function renderDraft(div, payload) {
    cancelCountdown();
    const g = payload.game;
    div.innerHTML = `
      ${cardHeader('CURRENT GAME')}
      ${gameSummary(g)}
      ${timeBlockHtml(g)}
      ${pollIntervalControlHtml(g)}
      ${claimModeControlHtml(g)}
      <div class="koth-empty" style="padding:12px 14px; text-align:left;">
        Draft created. Click <strong>DEPLOY</strong> to clone the VMs onto VLAN
        ${escapeHtml(g.entry_vlan_tag)} / ${escapeHtml(g.internal_vlan_tag)} and start the scoring loop.
        Cloning ~7 VMs takes a couple of minutes; the page will switch to a
        live deployment status while it runs.
      </div>
      <div class="koth-game-actions">
        <button class="koth-btn koth-btn--primary" id="b-deploy">DEPLOY</button>
        <button class="koth-btn koth-btn--danger" id="b-delete">DELETE DRAFT</button>
      </div>
    `;
    attachPollIntervalHandler(g.id);
    attachClaimModeHandler(g.id);
    document.getElementById('b-deploy').onclick = (ev) => {
      const btn = ev.currentTarget;
      if (!confirm(`Deploy ${g.name}? This will clone all templates in ${g.deployment_group_name} onto the configured VLANs.`)) return;
      withBusy(btn, 'STARTING…', async () => {
        try {
          await api(`/koth/api/games/${g.id}/deploy`, { method: 'POST' });
        } catch (e) { alert('Deploy failed: ' + e.message); }
        await loadCurrentGame();
      });
    };
    document.getElementById('b-delete').onclick = (ev) => {
      const btn = ev.currentTarget;
      if (!confirm(`Delete draft ${g.name}? VLAN ${g.entry_vlan_tag}/${g.internal_vlan_tag} will be released.`)) return;
      withBusy(btn, 'DELETING…', async () => {
        try {
          await api(`/koth/api/games/${g.id}`, { method: 'DELETE' });
        } catch (e) { alert('Delete failed: ' + e.message); }
        await loadCurrentGame();
      });
    };

    // Teams panel — show tokens immediately; rotate-token still works on drafts.
    document.getElementById('koth-teams-panel').hidden = false;
    renderTeams(payload.teams || [], { revealTokens: true });

    // Flags editor is available on draft so admins can prep the flag set
    // before clicking DEPLOY. Boxes don't exist yet, so per-box scoping
    // (box_name in YAML) won't resolve until after deploy — that's fine,
    // global flags can be loaded in advance.
    renderFlagEditor(payload);
  }

  function renderDeploying(div, payload) {
    cancelCountdown();
    const g = payload.game;
    div.innerHTML = `
      ${cardHeader('CURRENT GAME')}
      ${gameSummary(g)}
      ${timeBlockHtml(g)}
      <div class="koth-deploying-block">
        <div class="koth-spinner" aria-hidden="true"></div>
        <div class="koth-deploying-text">
          <div class="koth-deploying-title">DEPLOYMENT IN PROGRESS</div>
          <div class="koth-deploying-sub">
            Cloning templates onto VLAN ${escapeHtml(g.entry_vlan_tag)} / ${escapeHtml(g.internal_vlan_tag)}.
            This typically takes 1–3 minutes per VM. The page will refresh
            automatically when deployment finishes.
          </div>
        </div>
      </div>
      <div class="koth-game-actions">
        <button class="koth-btn koth-btn--danger" id="b-force-reset">FORCE RESET (STUCK?)</button>
      </div>
    `;
    document.getElementById('b-force-reset').onclick = (ev) => {
      const btn = ev.currentTarget;
      if (!confirm(`Force-reset game '${g.name}' to draft? This destroys any partial VMs and clears the deploying state. Use only if a deploy is genuinely stuck.`)) return;
      withBusy(btn, 'RESETTING…', async () => {
        try { await api(`/koth/api/games/${g.id}/force-reset`, { method: 'POST' }); }
        catch (e) { alert('Force-reset failed: ' + e.message); }
        await loadCurrentGame();
      });
    };
    // The deploy lock is server-side and the user can't safely cancel
    // mid-clone — only the force-reset escape hatch is available. Auto-poll
    // every 3s so the page flips to active automatically when the worker
    // finishes (or back to draft if it crashed).
    document.getElementById('koth-teams-panel').hidden = false;
    renderTeams(payload.teams || [], { revealTokens: true });
    document.getElementById('koth-flags').hidden = true;
    schedulePoll();
  }

  function schedulePoll() {
    if (deployingPollTimer) return;  // already polling
    deployingPollTimer = setInterval(loadCurrentGame, 3000);
  }
  function cancelPoll() {
    if (deployingPollTimer) { clearInterval(deployingPollTimer); deployingPollTimer = null; }
  }

  function renderActive(div, payload) {
    cancelCountdown();
    const g = payload.game;
    div.innerHTML = `
      ${cardHeader('CURRENT GAME')}
      ${gameSummary(g)}
      ${timeBlockHtml(g)}
      ${pollIntervalControlHtml(g)}
      ${claimModeControlHtml(g)}
      <div class="koth-toggle-row">
        <label><input type="checkbox" id="t-pause" ${g.pause_other_deployments ? 'checked' : ''}> PAUSE OTHER DEPLOYMENTS</label>
        <label><input type="checkbox" id="t-score" ${g.show_scoreboard ? 'checked' : ''}> SHOW SCOREBOARD</label>
        <label><input type="checkbox" id="t-map" ${g.show_network_map ? 'checked' : ''}> SHOW NETWORK MAP</label>
      </div>
      <div class="koth-game-actions">
        <button class="koth-btn" id="b-redeploy">REDEPLOY</button>
        <button class="koth-btn" id="b-refresh-ips" title="Re-query Proxmox guest agent for each box's current IPs. Use if claims are returning 403 (KothBox.ips empty)">REFRESH IPS</button>
        <button class="koth-btn koth-btn--danger" id="b-end">END GAME</button>
        <span class="koth-extend-group">
          <input id="koth-extend-input" type="number" min="1" value="15">
          <button class="koth-btn" id="b-extend">EXTEND BY N MIN</button>
        </span>
      </div>
    `;
    // Kick off the live countdown if there's an end time. startCountdown
    // bails cleanly if the #koth-time-left node isn't in the DOM.
    if (g.ends_at) startCountdown(g.ends_at);
    const patch = (body) => api(`/koth/api/games/${g.id}`,
      { method: 'PATCH', body: JSON.stringify(body) }).then(loadCurrentGame);
    document.getElementById('t-pause').onchange = e => patch({ pause_other_deployments: e.target.checked });
    document.getElementById('t-score').onchange = e => patch({ show_scoreboard: e.target.checked });
    document.getElementById('t-map').onchange = e => patch({ show_network_map: e.target.checked });
    attachPollIntervalHandler(g.id);
    attachClaimModeHandler(g.id);
    document.getElementById('b-end').onclick = (ev) => {
      const btn = ev.currentTarget;
      if (!confirm('End game? All KOTH VMs will be destroyed in Proxmox and the scoreboard will freeze.')) return;
      withBusy(btn, 'ENDING…', async () => {
        try { await api(`/koth/api/games/${g.id}/end`, { method: 'POST' }); }
        catch (e) { alert(e.message); }
        await loadCurrentGame();
      });
    };
    document.getElementById('b-redeploy').onclick = (ev) => {
      const btn = ev.currentTarget;
      if (!confirm('Redeploy? VMs will be destroyed and re-cloned. Scores preserved.')) return;
      withBusy(btn, 'REDEPLOYING…', async () => {
        try { await api(`/koth/api/games/${g.id}/redeploy`, { method: 'POST' }); }
        catch (e) { alert(e.message); }
        await loadCurrentGame();
      });
    };
    document.getElementById('b-refresh-ips').onclick = (ev) => {
      const btn = ev.currentTarget;
      withBusy(btn, 'REFRESHING…', async () => {
        try {
          const r = await api(`/koth/api/games/${g.id}/refresh-ips`,
            { method: 'POST' });
          alert(`Updated IPs on ${r.updated} box(es). Check 'em in /koth/api/state if you want to confirm.`);
        } catch (e) { alert('Refresh failed: ' + e.message); }
        await loadCurrentGame();
      });
    };
    document.getElementById('b-extend').onclick = (ev) => {
      const btn = ev.currentTarget;
      const input = document.getElementById('koth-extend-input');
      const n = parseInt(input && input.value, 10);
      if (!Number.isFinite(n) || n <= 0) { alert('Enter a positive number of minutes.'); return; }
      withBusy(btn, 'EXTENDING…', async () => {
        try {
          await api(`/koth/api/games/${g.id}`,
            { method: 'PATCH', body: JSON.stringify({ extend_minutes: n }) });
        } catch (e) { alert('Extend failed: ' + e.message); }
        await loadCurrentGame();
      });
    };

    renderFlagEditor(payload);
    document.getElementById('koth-teams-panel').hidden = false;
    renderTeams(payload.teams || [], { revealTokens: true });
  }

  // Render the list of flags into #koth-flags-table tbody. Each row gets
  // inline EDIT (toggles editable inputs in place + SAVE/CANCEL) and
  // DELETE actions wired against the existing endpoints.
  function renderFlagList(flags, gameId) {
    const tbody = document.querySelector('#koth-flags-table tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!flags || flags.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="koth-empty">no flags yet — add one below or upload a YAML</td></tr>';
      return;
    }
    for (const f of flags) {
      const tr = document.createElement('tr');
      tr.dataset.flagId = String(f.id);
      const scope = f.box_name ? `→ ${f.box_name}` : 'global';
      tr.innerHTML = `
        <td>
          <div class="koth-flag-name">${escapeHtml(f.name)}</div>
          <div class="koth-flag-scope">${escapeHtml(scope)}</div>
        </td>
        <td><code class="koth-flag-value">${escapeHtml(f.value)}</code></td>
        <td class="koth-num">${escapeHtml(f.points)}</td>
        <td class="koth-flag-actions">
          <button type="button" class="koth-btn koth-btn--small" data-action="edit">EDIT</button>
          <button type="button" class="koth-btn koth-btn--small koth-btn--danger" data-action="delete">DELETE</button>
        </td>
      `;
      tr.querySelector('[data-action="delete"]').onclick = (ev) =>
        deleteFlagRow(ev.currentTarget, f, gameId);
      tr.querySelector('[data-action="edit"]').onclick = () =>
        enterFlagEditMode(tr, f, gameId);
      tbody.appendChild(tr);
    }
  }

  async function deleteFlagRow(btn, flag, gameId) {
    if (!confirm(`Delete flag "${flag.name}"?`)) return;
    await withBusy(btn, '…', async () => {
      try {
        await api(`/koth/api/games/${gameId}/flags/${flag.id}`,
          { method: 'DELETE' });
        await loadCurrentGame();
      } catch (e) {
        // 409 with `force_required: true` body means accepted submissions
        // exist. Offer the force=1 path. Match on the structured body
        // field, not the human-readable message text.
        if (e.status === 409 && e.body && e.body.force_required) {
          const n = e.body.accepted_count || 'some';
          if (!confirm(`${n} accepted submission(s) exist for "${flag.name}". Force-delete anyway? Submission history stays in the audit log.`)) return;
          try {
            await api(`/koth/api/games/${gameId}/flags/${flag.id}?force=1`,
              { method: 'DELETE' });
            await loadCurrentGame();
          } catch (e2) { alert('Delete failed: ' + e2.message); }
        } else {
          alert('Delete failed: ' + e.message);
        }
      }
    });
  }

  // Replace the row's display cells with editable inputs. SAVE patches the
  // backend, CANCEL re-renders from current state. Boxes for the box-scope
  // dropdown come from the just-loaded payload via #koth-flags-table dataset.
  function enterFlagEditMode(tr, flag, gameId) {
    const boxOptions = (boxesForCurrentGame || []).map(b =>
      `<option value="${escapeHtml(b.id)}" ${flag.box_id === b.id ? 'selected' : ''}>${escapeHtml(b.name)}</option>`
    ).join('');
    tr.innerHTML = `
      <td>
        <input class="koth-flag-edit-input" data-field="name" value="${escapeAttr(flag.name)}" placeholder="Flag name">
        <select class="koth-flag-edit-input" data-field="box_id">
          <option value="">global</option>
          ${boxOptions}
        </select>
      </td>
      <td><input class="koth-flag-edit-input" data-field="value" value="${escapeAttr(flag.value)}" placeholder="Flag value"></td>
      <td><input class="koth-flag-edit-input koth-num" data-field="points" type="number" step="0.5" value="${escapeHtml(flag.points)}"></td>
      <td class="koth-flag-actions">
        <button type="button" class="koth-btn koth-btn--small" data-action="save">SAVE</button>
        <button type="button" class="koth-btn koth-btn--small" data-action="cancel">CANCEL</button>
      </td>
    `;
    tr.querySelector('[data-action="cancel"]').onclick = () => loadCurrentGame();
    tr.querySelector('[data-action="save"]').onclick = (ev) =>
      saveFlagEdit(ev.currentTarget, tr, flag, gameId);
  }

  async function saveFlagEdit(btn, tr, flag, gameId) {
    const get = (field) => tr.querySelector(`[data-field="${field}"]`).value;
    const boxIdRaw = get('box_id');
    const body = {
      name: get('name').trim(),
      value: get('value').trim(),
      points: parseFloat(get('points')),
      box_id: boxIdRaw === '' ? null : parseInt(boxIdRaw, 10),
    };
    await withBusy(btn, '…', async () => {
      try {
        await api(`/koth/api/games/${gameId}/flags/${flag.id}`, {
          method: 'PATCH', body: JSON.stringify(body),
        });
        await loadCurrentGame();
      } catch (e) { alert('Save failed: ' + e.message); }
    });
  }

  function escapeAttr(s) {
    return String(s).replace(/[&<>"']/g, ch =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  }

  // Reveal the FLAGS card and (re-)wire its upload button. The single-flag
  // ADD FLAG form is wired once at module load (its handler reads
  // `currentGameId` lazily), but the upload button is re-wired here so the
  // closed-over game id matches the freshly-rendered card.
  function renderFlagEditor(payload) {
    const card = document.getElementById('koth-flags');
    if (!card) return;
    card.hidden = false;
    const g = payload.game;
    // Stash the box list in closure scope (not on window) so
    // enterFlagEditMode can populate the box-scope dropdown without
    // re-fetching. Empty list on draft games (no boxes until deploy) is
    // fine — the dropdown still shows the "global" option, which is the
    // only valid scope at that stage.
    boxesForCurrentGame = payload.boxes || [];
    renderFlagList(payload.flags || [], g.id);
    const btn = document.getElementById('koth-upload-btn');
    const fileInput = document.getElementById('koth-upload-file');
    const errBox = document.getElementById('koth-upload-error');
    if (!btn || !fileInput) return;

    const showError = (msg) => {
      if (!errBox) return;
      errBox.textContent = msg;
      errBox.hidden = false;
    };
    const clearError = () => {
      if (!errBox) return;
      errBox.textContent = '';
      errBox.hidden = true;
    };

    btn.onclick = () => { clearError(); fileInput.value = ''; fileInput.click(); };
    fileInput.onchange = async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      clearError();
      let text;
      try { text = await file.text(); }
      catch (e) { showError('Could not read file: ' + e.message); return; }
      await withBusy(btn, 'UPLOADING…', async () => {
        try {
          const r = await api(`/koth/api/games/${g.id}/flags/upload`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-yaml' },
            body: text,
          });
          // Flash a quick confirmation, then re-render so any state that
          // depends on the flag set (e.g. future flag-list rendering) picks
          // up the new rows.
          alert(`Added ${r.added} flag(s).`);
          await loadCurrentGame();
        } catch (e) {
          // Server-side validation errors land here (e.g. "row 3: ...").
          // Render inline rather than alert() so the admin can read and fix
          // a long YAML message without dismissing a modal first.
          showError(e.message);
        }
      });
    };
  }

  function renderEmpty(div) {
    cancelCountdown();
    div.innerHTML = `${cardHeader('CURRENT GAME')}<div class="koth-empty">no current game</div>`;
    document.getElementById('koth-flags').hidden = true;
    document.getElementById('koth-teams-panel').hidden = true;
    // Only show CREATE NEW GAME when there's no game to act on. The
    // backend's singleton constraint would reject a second create anyway,
    // and showing the form between the current-game card and its
    // flags/teams cards visually fragments those properties from their
    // parent — see template comment in templates/koth_admin.html.
    document.getElementById('koth-create-form').hidden = false;
  }

  // Whenever a game is rendered (draft / deploying / active), make sure the
  // create-form is hidden. Cleaner than scattering this hide call across
  // each render function.
  function hideCreateForm() {
    const el = document.getElementById('koth-create-form');
    if (el) el.hidden = true;
  }

  async function loadCurrentGame() {
    const div = document.getElementById('koth-current-game');
    let payload = null;
    try {
      payload = await api('/koth/api/games/current');
    } catch (e) {
      div.innerHTML = `${cardHeader('CURRENT GAME')}<div class="koth-empty">error: ${escapeHtml(e.message)}</div>`;
      currentGameId = null;
      return;
    }
    if (!payload || !payload.game) {
      currentGameId = null;
      cancelPoll();
      renderEmpty(div);
      return;
    }
    currentGameId = payload.game.id;
    const status = payload.game.status;
    // A current game (any non-ended status) hides the CREATE NEW GAME
    // card. Done here in the dispatcher rather than in each render fn so
    // it's impossible to miss a code path.
    hideCreateForm();
    if (status === 'draft') { cancelPoll(); renderDraft(div, payload); }
    else if (status === 'deploying') { renderDeploying(div, payload); }
    else { cancelPoll(); renderActive(div, payload); }
  }

  function renderTeams(teams, { revealTokens } = {}) {
    const tbody = document.querySelector('#koth-teams-table tbody');
    tbody.innerHTML = '';
    if (teams.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="koth-empty">no teams on this game</td></tr>';
      return;
    }
    for (const t of teams) {
      const tr = document.createElement('tr');
      const tokenCell = revealTokens && t.submission_token
        ? `<code>${escapeHtml(t.submission_token)}</code>`
        : '— admin-only —';
      tr.innerHTML =
        `<td>${escapeHtml(t.display_name)}</td>` +
        `<td><span class="koth-swatch" style="background:${escapeHtml(t.color)}"></span>${escapeHtml(t.color)}</td>` +
        `<td class="koth-token-cell" id="tok-${escapeHtml(t.id)}">${tokenCell}</td>` +
        `<td><button class="koth-btn" data-team="${escapeHtml(t.id)}">ROTATE TOKEN</button></td>`;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll('button[data-team]').forEach(btn => {
      btn.onclick = async () => {
        const tid = btn.dataset.team;
        if (!currentGameId) return;
        if (!confirm("Rotate this team's submission token? The old one will stop working immediately.")) return;
        try {
          const r = await api(`/koth/api/games/${currentGameId}/teams/${tid}/rotate-token`, { method: 'POST' });
          document.getElementById('tok-' + tid).innerHTML = `<code>${escapeHtml(r.team.submission_token)}</code>`;
        } catch (e) { alert(e.message); }
      };
    });
  }

  document.getElementById('koth-add-flag').onsubmit = async (e) => {
    e.preventDefault();
    if (!currentGameId) { alert('No active game.'); return; }
    const fd = new FormData(e.target);
    try {
      await api(`/koth/api/games/${currentGameId}/flags`, {
        method: 'POST',
        body: JSON.stringify({
          name: fd.get('name'),
          value: fd.get('value'),
          points: parseFloat(fd.get('points')),
        }),
      });
      e.target.reset();
      alert('Flag added.');
    } catch (err) { alert(err.message); }
  };

  document.getElementById('koth-new-game').onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const team_group_ids = Array.from(
      document.querySelectorAll('#koth-team-picker input:checked'))
      .map(c => parseInt(c.value, 10));
    // duration_minutes is optional. Backend accepts null = unlimited but
    // rejects an empty string with a 400, so coerce blank -> null explicitly.
    const durRaw = (fd.get('duration_minutes') || '').toString().trim();
    const duration_minutes = durRaw === '' ? null : parseInt(durRaw, 10);
    try {
      const created = await api('/koth/api/games', { method: 'POST', body: JSON.stringify({
        name: fd.get('name'),
        deployment_group_name: fd.get('deployment_group_name'),
        entry_vlan_tag: parseInt(fd.get('entry_vlan_tag'), 10),
        internal_vlan_tag: parseInt(fd.get('internal_vlan_tag'), 10),
        duration_minutes,
        team_group_ids,
      })});
      e.target.reset();
      // Hide the create-form synchronously now that a game exists, BEFORE
      // awaiting loadCurrentGame. Without this, if loadCurrentGame errors
      // (network blip, transient 500), the form stays visible alongside the
      // freshly-created draft, which is the exact split-state the rest of
      // this layout fix is trying to prevent.
      hideCreateForm();
      // Scroll the user to the now-rendered current-game card so they see the result.
      await loadCurrentGame();
      const cur = document.getElementById('koth-current-game');
      if (cur && cur.scrollIntoView) cur.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) { alert(err.message); return; }
  };

  Promise.all([loadDeploymentGroups(), loadTeamPicker(), loadCurrentGame()]);
})();
