/* Student groups admin UI module. */

(function () {
    "use strict";

    function qs(id) { return document.getElementById(id); }

    async function fetchJSON(url, opts) {
        const res = await fetch(url, Object.assign({
            headers: {'Content-Type': 'application/json'}
        }, opts || {}));
        const body = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, body };
    }

    async function loadGroups() {
        const { ok, body } = await fetchJSON('/admin/groups');
        if (!ok) return;
        const table = qs('groups-table');
        if (!table) return;
        const tbody = table.querySelector('tbody');
        tbody.innerHTML = '';
        (body.groups || []).forEach(g => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><code>${escapeHtml(g.name)}</code></td>
                <td>${escapeHtml(g.display_name)}</td>
                <td>${escapeHtml(g.member_count)}</td>
                <td>${escapeHtml(g.entry_vlan_tag)}</td>
                <td>${escapeHtml(g.internal_vlan_tag)}</td>
                <td class="text-end">
                    <div class="btn-group btn-group-sm" role="group">
                        <button data-id="${escapeHtml(g.id)}" class="btn btn-outline-light js-view" title="Edit members and VLANs">
                            <i class="bi bi-pencil-square"></i> Edit
                        </button>
                        <button data-id="${escapeHtml(g.id)}" class="btn btn-outline-danger js-delete" title="Delete this group">
                            <i class="bi bi-trash"></i> Delete
                        </button>
                    </div>
                </td>`;
            tbody.appendChild(tr);
        });
        tbody.querySelectorAll('.js-view').forEach(btn =>
            btn.addEventListener('click', (ev) => {
                ev.currentTarget.blur();
                viewGroup(btn.dataset.id);
            }));
        tbody.querySelectorAll('.js-delete').forEach(btn =>
            btn.addEventListener('click', () => deleteGroup(btn.dataset.id)));
    }

    async function viewGroup(id) {
        const { ok, body } = await fetchJSON(`/admin/groups/${id}`);
        if (!ok) return;
        const g = body.group;
        const panel = qs('group-detail');
        if (!panel) return;
        panel.style.display = '';
        qs('group-detail-title').textContent = `${g.display_name} (${g.name})`;
        const activeCount = g.active_vm_count || 0;
        const vlanDisabled = activeCount > 0;
        qs('group-detail-body').innerHTML = `
            <p class="text-muted small mb-3">
                Slug <code>${escapeHtml(g.name)}</code> is permanent and appears in every VM name (<code>group-${escapeHtml(g.name)}-&lt;template&gt;</code>).
                ${activeCount > 0 ? `<span class="text-warning">This group has ${escapeHtml(activeCount)} active VM${activeCount === 1 ? '' : 's'}; VLAN changes are locked until they are destroyed.</span>` : ''}
            </p>

            <form id="group-edit-form" class="mb-4">
                <div class="mb-3">
                    <label for="sg-display-name" class="form-label">Display name</label>
                    <input id="sg-display-name" name="display_name" value="${escapeHtml(g.display_name)}" class="form-control" />
                    <div class="form-text">Shown in listings. Can be edited anytime.</div>
                </div>
                <div class="mb-3">
                    <label for="sg-description" class="form-label">Description</label>
                    <textarea id="sg-description" name="description" class="form-control" rows="2" placeholder="Optional — e.g. &quot;Midterm Team 1 members&quot;">${escapeHtml(g.description)}</textarea>
                </div>
                <div class="row g-2 mb-2">
                    <div class="col-sm-6">
                        <label for="sg-entry-vlan" class="form-label">Entry VLAN</label>
                        <input id="sg-entry-vlan" type="number" name="entry_vlan_tag" value="${escapeHtml(g.entry_vlan_tag)}"
                            class="form-control" ${vlanDisabled ? 'disabled' : ''} />
                    </div>
                    <div class="col-sm-6">
                        <label for="sg-internal-vlan" class="form-label">Internal VLAN</label>
                        <input id="sg-internal-vlan" type="number" name="internal_vlan_tag" value="${escapeHtml(g.internal_vlan_tag)}"
                            class="form-control" ${vlanDisabled ? 'disabled' : ''} />
                    </div>
                </div>
                <div class="form-text mb-3">
                    Shared VLAN pair used by all VMs in this group's deployments. Leave at defaults unless overriding for a reserved range.
                </div>
                <button type="submit" class="btn btn-primary btn-sm">
                    <i class="bi bi-save"></i> Save details
                </button>
            </form>

            <div class="mb-2">
                <h6 class="text-uppercase small fw-bold text-muted border-bottom pb-2 mb-3">
                    <i class="bi bi-people"></i> Members
                </h6>
                <p class="text-muted small">Any member can deploy, destroy, or extend this group's shared VMs. A student can only belong to one group at a time.</p>
                <div id="group-members"></div>
            </div>
        `;
        qs('group-edit-form').addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const data = Object.fromEntries(new FormData(ev.target));
            if (data.entry_vlan_tag === '') delete data.entry_vlan_tag;
            else data.entry_vlan_tag = Number(data.entry_vlan_tag);
            if (data.internal_vlan_tag === '') delete data.internal_vlan_tag;
            else data.internal_vlan_tag = Number(data.internal_vlan_tag);
            const res = await fetchJSON(`/admin/groups/${id}`, {
                method: 'PUT', body: JSON.stringify(data),
            });
            if (!res.ok) alert(res.body.error || 'Save failed');
            else loadGroups().then(() => viewGroup(id));
        });
        renderMembers(id, qs('group-members'));
    }

    async function deleteGroup(id) {
        let r = await fetchJSON(`/admin/groups/${id}`, { method: 'DELETE' });
        if (r.status === 409) {
            const count = r.body.active_vm_count || 0;
            if (!confirm(`Group has ${count} active VM(s). Destroy them and delete the group?`)) return;
            r = await fetchJSON(`/admin/groups/${id}?confirm_destroy=1`, { method: 'DELETE' });
        }
        if (!r.ok) alert(r.body.error || 'Delete failed');
        else loadGroups();
    }

    async function renderMembers(groupId, panel) {
        const { body } = await fetchJSON(`/admin/groups/${groupId}/members`);
        const members = body.members || [];
        panel.innerHTML = `
            <form class="d-flex gap-2 mb-3" autocomplete="off">
                <input name="login_id" class="form-control form-control-sm" placeholder="Canvas login_id (e.g. jdoe1)" style="max-width: 320px;" />
                <button type="submit" class="btn btn-sm btn-outline-light">
                    <i class="bi bi-person-plus"></i> Add member
                </button>
            </form>
            ${members.length === 0
                ? '<p class="text-muted small fst-italic mb-0">No members yet.</p>'
                : `<table class="table table-sm mb-0">
                    <thead><tr><th>Login ID</th><th>Name</th><th class="text-end"></th></tr></thead>
                    <tbody>
                        ${members.map(m => `
                            <tr>
                                <td><code>${escapeHtml(m.user_login_id)}</code></td>
                                <td>${m.user_full_name == null ? '<span class="text-muted">(no name)</span>' : escapeHtml(m.user_full_name)}</td>
                                <td class="text-end"><button class="btn btn-sm btn-outline-danger js-remove"
                                    data-uid="${escapeHtml(m.user_id)}" title="Remove from group"><i class="bi bi-person-dash"></i> Remove</button></td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>`}`;
        panel.querySelector('form').addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const login = ev.target.login_id.value.trim().toLowerCase();
            if (!login) return;
            const r = await fetchJSON(`/admin/groups/${groupId}/members`, {
                method: 'POST', body: JSON.stringify({ login_id: login }),
            });
            if (!r.ok) alert(r.body.error || 'Add failed');
            else renderMembers(groupId, panel);
        });
        panel.querySelectorAll('.js-remove').forEach(btn => {
            btn.addEventListener('click', async () => {
                const uid = btn.dataset.uid;
                let r = await fetchJSON(`/admin/groups/${groupId}/members/${uid}`, { method: 'DELETE' });
                if (r.status === 409) {
                    if (!confirm(r.body.error + ' Proceed?')) return;
                    r = await fetchJSON(`/admin/groups/${groupId}/members/${uid}?confirm_revoke=1`, { method: 'DELETE' });
                }
                if (!r.ok) alert(r.body.error || 'Remove failed');
                else renderMembers(groupId, panel);
            });
        });
    }

    let pendingFile = null;
    let pendingToken = null;

    async function previewYaml(file) {
        const form = new FormData();
        form.append('file', file);
        const res = await fetch('/admin/groups/yaml/preview', { method: 'POST', body: form });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) { alert(body.error || 'Preview failed'); return; }
        pendingFile = file;
        pendingToken = body.plan_token;
        renderDiff(body);
    }

    function renderDiffRow(g, action, members) {
        const memberChips = (members && members.length)
            ? members.map(m => `<span class="yaml-diff__chip">${escapeHtml(m)}</span>`).join('')
            : '<span class="yaml-diff__chip yaml-diff__chip--empty">no members</span>';
        const actionLabel = action === 'add' ? '+ add' : '~ update';
        return `
            <div class="yaml-diff__row yaml-diff__row--${action}">
                <div>
                    <div class="yaml-diff__row-head">
                        <span class="yaml-diff__action yaml-diff__action--${action}">${actionLabel}</span>
                        <span class="yaml-diff__slug">${escapeHtml(g.name)}</span>
                        <span class="yaml-diff__name">${escapeHtml(g.display_name)}</span>
                    </div>
                </div>
                <div class="yaml-diff__stats">
                    <span class="yaml-diff__stat">
                        <span class="yaml-diff__stat-value">${(members || []).length}</span>
                        <span class="yaml-diff__stat-label">members</span>
                    </span>
                    <span class="yaml-diff__vlan" title="entry VLAN / internal VLAN">
                        ${escapeHtml(g.entry_vlan_tag)}<span class="yaml-diff__vlan-sep">/</span>${escapeHtml(g.internal_vlan_tag)}
                    </span>
                </div>
                <div class="yaml-diff__chips">
                    <span class="yaml-diff__chips-label">members</span>
                    ${memberChips}
                </div>
            </div>`;
    }

    function renderDiff(body) {
        const panel = qs('groups-yaml-panel');
        if (!panel) return;
        panel.style.display = '';

        const adds = body.diff.groups_to_add || [];
        const updates = body.diff.groups_to_update || [];
        const members = body.diff.members_to_set_by_group || {};
        const errors = body.errors || [];
        const warnings = body.warnings || [];

        const summary = `
            <div class="yaml-diff__summary">
                <span>plan</span>
                <span class="yaml-diff__summary-pill" data-kind="${adds.length ? 'add' : 'zero'}">
                    <i class="bi bi-plus-circle"></i>${adds.length} add
                </span>
                <span class="yaml-diff__summary-pill" data-kind="${updates.length ? 'update' : 'zero'}">
                    <i class="bi bi-pencil"></i>${updates.length} update
                </span>
                ${errors.length ? `<span class="yaml-diff__summary-pill" style="background:rgba(239,68,68,0.12);border-color:rgba(239,68,68,0.35);color:#f87171;"><i class="bi bi-x-octagon"></i>${errors.length} error${errors.length === 1 ? '' : 's'}</span>` : ''}
                ${warnings.length ? `<span class="yaml-diff__summary-pill" style="background:rgba(251,191,36,0.12);border-color:rgba(251,191,36,0.35);color:var(--warning);"><i class="bi bi-exclamation-triangle"></i>${warnings.length} warning${warnings.length === 1 ? '' : 's'}</span>` : ''}
            </div>`;

        const rows = [
            ...adds.map(g => renderDiffRow(g, 'add', members[g.name])),
            ...updates.map(g => renderDiffRow(g, 'update', members[g.name])),
        ].join('');

        const empty = (adds.length === 0 && updates.length === 0)
            ? `<div class="yaml-diff__empty">
                   <i class="bi bi-check-circle-fill"></i>
                   No changes — uploaded YAML matches current state.
               </div>`
            : '';

        const errBlock = errors.length ? `
            <div class="yaml-diff__alert yaml-diff__alert--error">
                <div class="yaml-diff__alert-head"><i class="bi bi-x-octagon-fill"></i> Errors — apply blocked</div>
                <ul>${errors.map(e => `<li>${escapeHtml(e)}</li>`).join('')}</ul>
            </div>` : '';

        const warnBlock = warnings.length ? `
            <div class="yaml-diff__alert yaml-diff__alert--warning">
                <div class="yaml-diff__alert-head"><i class="bi bi-exclamation-triangle-fill"></i> Warnings — apply allowed</div>
                <ul>${warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}</ul>
            </div>` : '';

        const alertsBlock = (errors.length || warnings.length)
            ? `<div class="yaml-diff__alerts">${errBlock}${warnBlock}</div>` : '';

        const footerNote = errors.length
            ? 'fix errors above, re-upload the YAML, and try again'
            : (adds.length + updates.length === 0
                ? 'nothing to apply'
                : `ready to apply ${adds.length + updates.length} change${adds.length + updates.length === 1 ? '' : 's'}`);

        qs('groups-yaml-body').innerHTML = `
            <div class="yaml-diff">
                ${summary}
                ${rows}
                ${empty}
                ${alertsBlock}
                <div class="yaml-diff__footer">
                    <span class="yaml-diff__footer-note">${footerNote}</span>
                    <button id="btn-apply-yaml" class="btn btn-primary btn-sm"
                        ${errors.length || (adds.length + updates.length === 0) ? 'disabled' : ''}>
                        <i class="bi bi-check2-circle"></i> Apply changes
                    </button>
                </div>
            </div>`;
        qs('btn-apply-yaml').addEventListener('click', applyYaml);
    }

    async function applyYaml() {
        if (!pendingFile || !pendingToken) return;
        const form = new FormData();
        form.append('file', pendingFile);
        form.append('plan_token', pendingToken);
        const res = await fetch('/admin/groups/yaml/apply', { method: 'POST', body: form });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            alert((body.errors || [body.error || 'Apply failed']).join('\n'));
            return;
        }
        alert(`Applied. Created ${body.created?.length || 0}, updated ${body.updated?.length || 0}.`);
        qs('groups-yaml-panel').style.display = 'none';
        pendingFile = null;
        pendingToken = null;
        loadGroups();
    }

    function init() {
        qs('btn-new-group')?.addEventListener('click', async () => {
            const name = prompt('Group slug (lowercase, digits, dashes):');
            if (!name) return;
            const display = prompt('Display name:', name);
            if (!display) return;
            const r = await fetchJSON('/admin/groups', {
                method: 'POST',
                body: JSON.stringify({ name, display_name: display }),
            });
            if (!r.ok) alert(r.body.error || 'Create failed');
            else loadGroups();
        });
        qs('btn-upload-groups-yaml')?.addEventListener('click', () => {
            const input = document.createElement('input');
            input.type = 'file';
            input.accept = '.yaml,.yml';
            input.addEventListener('change', async () => {
                const file = input.files[0];
                if (!file) return;
                await previewYaml(file);
            });
            input.click();
        });
    }

    window.StudentGroupsAdmin = { init, loadGroups };
    document.addEventListener('DOMContentLoaded', () => {
        if (qs('admin-groups-tab-content')) {
            init();
            loadGroups();
        }
    });
})();
