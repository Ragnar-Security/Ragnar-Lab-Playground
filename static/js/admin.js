// ============ ADMIN FUNCTIONS ============

// ---- Test Alerts ----

// Test Alerts Form (Admin Only)
document.getElementById('test-alerts-form').addEventListener('submit', function(e) {
    e.preventDefault();

    const testEmail = document.getElementById('test_email').checked;
    const testWebhook = document.getElementById('test_webhook').checked;

    if (!testEmail && !testWebhook) {
        showMessage('test-alerts-result', '<strong>Warning:</strong> Please select at least one alert type to test (email or webhook)', 'warning');
        return;
    }

    showLoading('test-alerts-result');

    const formData = new FormData();
    formData.append('test_email', testEmail ? 'true' : 'false');
    formData.append('test_webhook', testWebhook ? 'true' : 'false');

    fetch('/admin/test-alerts', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success || data.results) {
            // Build detailed results message
            let message = '<strong>Test Alert Results:</strong><br><br>';

            const results = data.results || {};

            // File logging result
            if (results.file) {
                const fileIcon = results.file.logged ? '✅' : '❌';
                message += `${fileIcon} <strong>File Logging:</strong> ${results.file.logged ? 'Success' : 'Failed'}`;
                if (results.file.error) {
                    message += `<br>&nbsp;&nbsp;&nbsp;&nbsp;<small class="text-danger">Error: ${escapeHtml(results.file.error)}</small>`;
                }
                message += '<br>';
            }

            // Database alert result
            if (results.database) {
                const dbIcon = results.database.created ? '✅' : '❌';
                message += `${dbIcon} <strong>Database Alert:</strong> ${results.database.created ? 'Success' : 'Failed'}`;
                if (results.database.error) {
                    message += `<br>&nbsp;&nbsp;&nbsp;&nbsp;<small class="text-danger">Error: ${escapeHtml(results.database.error)}</small>`;
                }
                message += '<br>';
            }

            // Email result
            if (testEmail && results.email) {
                const emailIcon = results.email.sent ? '✅' : '❌';
                message += `${emailIcon} <strong>Email Alert:</strong> ${results.email.sent ? 'Success' : 'Failed'}`;
                if (results.email.error) {
                    message += `<br>&nbsp;&nbsp;&nbsp;&nbsp;<small class="text-danger">Error: ${escapeHtml(results.email.error)}</small>`;
                }
                message += '<br>';
            }

            // Webhook result
            if (testWebhook && results.webhook) {
                const webhookIcon = results.webhook.sent ? '✅' : '❌';
                message += `${webhookIcon} <strong>Webhook Alert:</strong> ${results.webhook.sent ? 'Success' : 'Failed'}`;
                if (results.webhook.error) {
                    message += `<br>&nbsp;&nbsp;&nbsp;&nbsp;<small class="text-danger">Error: ${escapeHtml(results.webhook.error)}</small>`;
                }
                message += '<br>';
            }

            message += '<br><small class="text-muted">Check your email inbox and Discord/Slack channel for test notifications.</small>';

            const hasErrors = (results.file && !results.file.logged) ||
                            (results.database && !results.database.created) ||
                            (testEmail && results.email && !results.email.sent) ||
                            (testWebhook && results.webhook && !results.webhook.sent);

            showMessage('test-alerts-result', message, hasErrors ? 'warning' : 'success');
        } else {
            showMessage('test-alerts-result', `<strong>Error:</strong> ${escapeHtml(data.error || 'Unknown error')}`, 'error');
        }
    })
    .catch(error => {
        showMessage('test-alerts-result', `<strong>Error:</strong> ${escapeHtml(error.message)}`, 'error');
    });
});

// ---- Lab Assignments ----

let assignmentStudents = [];
let assignmentGroups = [];

function loadAssignmentFormData() {
    // Load students
    fetch('/admin/students')
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                assignmentStudents = data.students;
                const sel = document.getElementById('assign_students');
                if (sel) {
                    sel.innerHTML = data.students
                        .filter(s => !s.is_admin)
                        .map(s => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.login_id)}${s.full_name ? ' (' + escapeHtml(s.full_name) + ')' : ''}</option>`)
                        .join('');
                }
            }
        })
        .catch(err => console.error('Error loading students:', err));

    // Load deployment groups for the assign dropdown
    fetch('/templates')
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                assignmentGroups = data.deployment_groups;
                const options = data.deployment_groups
                    .map(g => `<option value="${escapeHtml(g.display_name)}">${escapeHtml(g.display_name)}</option>`)
                    .join('');
                const assignGroup = document.getElementById('assign_group');
                if (assignGroup) assignGroup.innerHTML = '<option value="">-- Select group --</option>' + options;
                const filterGroup = document.getElementById('filter_group');
                if (filterGroup) filterGroup.innerHTML = '<option value="">-- All groups --</option>' + options;
                const randomGroups = document.getElementById('random_assign_groups');
                if (randomGroups) randomGroups.innerHTML = options;
            }
        })
        .catch(err => console.error('Error loading groups:', err));
}

function loadAssignments() {
    const filterGroup = document.getElementById('filter_group');
    const group = filterGroup ? filterGroup.value : '';
    const url = group ? `/admin/assignments?group=${encodeURIComponent(group)}` : '/admin/assignments';

    fetch(url)
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById('assignments-table-container');
            if (!data.success || !data.assignments.length) {
                container.innerHTML = '<p class="text-muted">No assignments found.</p>';
                const clearBtn = document.getElementById('clear-assignments-btn');
                if (clearBtn) clearBtn.style.display = 'none';
                return;
            }
            let html = `<table class="table table-hover">
                <thead><tr>
                    <th>Student</th><th>Name</th><th>Deployment Group</th><th>Assigned At</th><th>Action</th>
                </tr></thead><tbody>`;
            data.assignments.forEach(a => {
                const date = new Date(a.assigned_at).toLocaleString();
                html += `<tr>
                    <td>${escapeHtml(a.user_login_id || 'N/A')}</td>
                    <td>${escapeHtml(a.user_full_name || '')}</td>
                    <td>${escapeHtml(a.deployment_group_name)}</td>
                    <td>${escapeHtml(date)}</td>
                    <td><button class="btn btn-sm btn-outline-danger js-delete-assignment" data-id="${escapeHtml(a.id)}"><i class="bi bi-trash"></i></button></td>
                </tr>`;
            });
            html += '</tbody></table>';
            container.innerHTML = html;
            container.querySelectorAll('.js-delete-assignment').forEach(btn =>
                btn.addEventListener('click', () => deleteAssignment(btn.dataset.id)));

            // Show clear button if filtered
            const clearBtn = document.getElementById('clear-assignments-btn');
            if (clearBtn) {
                clearBtn.style.display = group ? 'inline-block' : 'none';
            }
        })
        .catch(err => {
            document.getElementById('assignments-table-container').innerHTML =
                `<p class="text-danger">Error loading assignments: ${escapeHtml(err.message)}</p>`;
        });
}

function deleteAssignment(id) {
    if (!confirm('Remove this assignment?')) return;
    fetch(`/admin/assignments/${id}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.success) loadAssignments();
            else alert('Error: ' + (data.error || 'Unknown error'));
        })
        .catch(err => alert('Error: ' + err.message));
}

// ---- Student Management ----

function loadStudentManagement() {
    fetch('/admin/students/full')
        .then(r => r.json())
        .then(data => {
            if (!data.success) return;
            window._allStudents = data.students;
            renderStudentTable(data.students);
        });
}

function renderStudentTable(students, filter) {
    const filtered = filter
        ? students.filter(s => s.login_id.includes(filter) || (s.full_name || '').toLowerCase().includes(filter.toLowerCase()))
        : students;
    let html = '<table class="table table-hover table-sm"><thead><tr><th>Login ID</th><th>Name</th><th>Role</th><th>VLANs</th><th>Last Login</th><th>VMs</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
    filtered.forEach(s => {
        const role = s.is_admin
            ? '<span class="badge" style="background:rgba(255,210,0,0.15);color:#FFD200;border:1px solid rgba(255,210,0,0.3)">Admin</span>'
            : '<span class="badge" style="background:rgba(99,140,255,0.15);color:#8babff;border:1px solid rgba(99,140,255,0.3)">Student</span>';
        const status = s.disabled ? '<span class="badge bg-secondary">Disabled</span>' : '<span class="badge bg-success">Active</span>';
        const lastLogin = s.last_login ? new Date(s.last_login).toLocaleDateString() : 'Never';
        html += `<tr><td>${escapeHtml(s.login_id)}</td><td>${escapeHtml(s.full_name || '')}</td><td>${role}</td><td>${escapeHtml(s.entry_vlan_tag || '-')}/${escapeHtml(s.internal_vlan_tag || '-')}</td><td>${escapeHtml(lastLogin)}</td><td>${escapeHtml(s.active_vm_count)}</td><td>${status}</td><td>
            <button class="btn btn-sm btn-outline-warning js-toggle-admin" data-id="${escapeHtml(s.id)}" data-value="${s.is_admin ? 'false' : 'true'}" style="border:1px solid rgba(255,210,0,0.5)" title="${s.is_admin ? 'Remove admin' : 'Make admin'}"><i class="bi bi-shield"></i></button>
            <button class="btn btn-sm btn-outline-${s.disabled ? 'success' : 'danger'} js-toggle-disabled" data-id="${escapeHtml(s.id)}" data-value="${s.disabled ? 'false' : 'true'}" title="${s.disabled ? 'Enable' : 'Disable'}"><i class="bi bi-${s.disabled ? 'check-circle' : 'x-circle'}"></i></button>
            <button class="btn btn-sm btn-outline-info js-reset-link" data-id="${escapeHtml(s.id)}" data-login="${escapeHtml(s.login_id)}" title="Create reset link"><i class="bi bi-key"></i></button>
        </td></tr>`;
    });
    html += '</tbody></table>';
    const container = document.getElementById('students-table-container');
    container.innerHTML = html;
    container.querySelectorAll('.js-toggle-admin').forEach(btn =>
        btn.addEventListener('click', () => toggleAdmin(Number(btn.dataset.id), btn.dataset.value === 'true')));
    container.querySelectorAll('.js-toggle-disabled').forEach(btn =>
        btn.addEventListener('click', () => toggleDisabled(Number(btn.dataset.id), btn.dataset.value === 'true')));
    container.querySelectorAll('.js-reset-link').forEach(btn =>
        btn.addEventListener('click', () => createResetLink(Number(btn.dataset.id), btn.dataset.login)));
}

function toggleAdmin(userId, value) {
    if (!confirm(value ? 'Grant admin privileges?' : 'Remove admin privileges?')) return;
    fetch('/admin/students/' + userId, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({is_admin: value}) })
        .then(r => r.json()).then(data => { if (data.success) loadStudentManagement(); });
}

function toggleDisabled(userId, value) {
    if (!confirm(value ? 'Disable this account?' : 'Enable this account?')) return;
    fetch('/admin/students/' + userId, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({disabled: value}) })
        .then(r => r.json()).then(data => { if (data.success) loadStudentManagement(); });
}

function createResetLink(userId, loginId) {
    if (!confirm(`Create a password reset link for ${loginId}? Any earlier unused link for this account stops working.`)) return;
    fetch(`/admin/students/${userId}/reset-link`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' })
        .then(r => r.json())
        .then(data => {
            if (data.success) showResetLink(data);
            else showMessage('reset-link-result', `<strong>Error:</strong> ${escapeHtml(data.error || 'Unknown error')}`, 'error');
        })
        .catch(err => showMessage('reset-link-result', `<strong>Error:</strong> ${escapeHtml(err.message)}`, 'error'));
}

// The server returns the link exactly once and keeps only its hash, so this
// panel is the admin's only chance to copy it. Built with DOM APIs: the link
// is a URL and a credential, never markup.
function showResetLink(data) {
    const url = new URL(data.reset_url, window.location.origin).href;
    const container = document.getElementById('reset-link-result');

    const box = document.createElement('div');
    box.className = 'alert alert-warning';
    const heading = document.createElement('div');
    heading.innerHTML = `<strong>Reset link for ${escapeHtml(data.login_id)}</strong>: shown only now. `
        + `It works once and expires ${escapeHtml(new Date(data.expires_at).toLocaleString())}. `
        + 'Give it to the student directly.';

    const group = document.createElement('div');
    group.className = 'input-group mt-2';
    const input = document.createElement('input');
    input.type = 'text';
    input.readOnly = true;
    input.id = 'reset-link-url';
    input.className = 'form-control font-monospace';
    input.value = url;
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'btn btn-outline-dark';
    copy.textContent = 'Copy';
    copy.addEventListener('click', () => {
        input.select();
        // The Clipboard API exists only in secure contexts (HTTPS, localhost).
        const copied = navigator.clipboard
            ? navigator.clipboard.writeText(url)
            : Promise.resolve(document.execCommand('copy'));
        copied.then(() => { copy.textContent = 'Copied'; });
    });
    const done = document.createElement('button');
    done.type = 'button';
    done.className = 'btn btn-outline-secondary';
    done.textContent = 'Done';
    done.addEventListener('click', () => container.replaceChildren());

    group.append(input, copy, done);
    box.append(heading, group);
    container.replaceChildren(box);
    input.select();
}

// ---- VM Overview (Admin) ----

function loadAdminVMs() {
    const student = document.getElementById('vm-filter-student') ? document.getElementById('vm-filter-student').value : '';
    const node = document.getElementById('vm-filter-node') ? document.getElementById('vm-filter-node').value : '';
    const status = document.getElementById('vm-filter-status') ? document.getElementById('vm-filter-status').value : '';
    let url = '/admin/vms?';
    if (student) url += 'student=' + encodeURIComponent(student) + '&';
    if (node) url += 'node=' + encodeURIComponent(node) + '&';
    if (status) url += 'status=' + encodeURIComponent(status) + '&';

    fetch(url).then(r => r.json()).then(data => {
        if (!data.success) return;
        const container = document.getElementById('admin-vms-table-container');
        if (!data.vms.length) { container.innerHTML = '<p class="text-muted">No VMs found.</p>'; return; }
        const students = [...new Set(data.vms.map(v => v.student_name))];
        const nodes = [...new Set(data.vms.map(v => v.node))];
        const studentSel = document.getElementById('vm-filter-student');
        if (studentSel && studentSel.options.length <= 1) students.forEach(s => studentSel.add(new Option(s, s)));
        const nodeSel = document.getElementById('vm-filter-node');
        if (nodeSel && nodeSel.options.length <= 1) nodes.forEach(n => nodeSel.add(new Option(n, n)));

        let html = '<table class="table table-hover table-sm"><thead><tr><th><input type="checkbox" id="select-all-vms"></th><th>VMID</th><th>Name</th><th>Student</th><th>Group</th><th>Node</th><th>Status</th><th>Expires</th></tr></thead><tbody>';
        data.vms.forEach(v => {
            const vmStatus = v.destroyed ? '<span class="badge bg-secondary">Destroyed</span>' : '<span class="badge bg-success">Active</span>';
            const expires = v.destroyed ? '-' : new Date(v.expires_at).toLocaleString();
            html += '<tr><td><input type="checkbox" class="vm-checkbox" value="' + escapeHtml(v.id) + '"' + (v.destroyed ? ' disabled' : '') + '></td><td>' + escapeHtml(v.vmid) + '</td><td>' + escapeHtml(v.vm_name) + '</td><td>' + escapeHtml(v.student_name) + '</td><td>' + escapeHtml(v.template_name) + '</td><td>' + escapeHtml(v.node) + '</td><td>' + vmStatus + '</td><td>' + escapeHtml(expires) + '</td></tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;

        document.getElementById('select-all-vms').addEventListener('change', function() {
            document.querySelectorAll('.vm-checkbox:not(:disabled)').forEach(cb => cb.checked = this.checked);
            updateBulkActions();
        });
        document.querySelectorAll('.vm-checkbox').forEach(cb => cb.addEventListener('change', updateBulkActions));
    });
}

function updateBulkActions() {
    const checked = document.querySelectorAll('.vm-checkbox:checked').length;
    const el = document.getElementById('bulk-actions');
    if (el) el.style.display = checked > 0 ? 'block' : 'none';
}

function getSelectedVmIds() {
    return Array.from(document.querySelectorAll('.vm-checkbox:checked')).map(cb => parseInt(cb.value));
}

// ---- Admin DOMContentLoaded handlers ----

document.addEventListener('DOMContentLoaded', function() {
    // Assign button handler
    const assignBtn = document.getElementById('assign-btn');
    if (assignBtn) {
        assignBtn.addEventListener('click', function() {
            const group = document.getElementById('assign_group').value;
            const studentSelect = document.getElementById('assign_students');
            const selectedStudents = Array.from(studentSelect.selectedOptions).map(o => parseInt(o.value));

            if (!group) { alert('Please select a deployment group'); return; }
            if (!selectedStudents.length) { alert('Please select at least one student'); return; }

            fetch('/admin/assignments/bulk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_ids: selectedStudents, deployment_group_names: [group] })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showMessage('assignments-table-container',
                        `<strong>Success:</strong> ${escapeHtml(data.message)}`, 'success');
                    setTimeout(loadAssignments, 1500);
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => alert('Error: ' + err.message));
        });
    }

    // Filter change handler
    const filterGroup = document.getElementById('filter_group');
    if (filterGroup) {
        filterGroup.addEventListener('change', loadAssignments);
    }

    // Clear all button
    const clearBtn = document.getElementById('clear-assignments-btn');
    if (clearBtn) {
        clearBtn.addEventListener('click', function() {
            const group = document.getElementById('filter_group').value;
            if (!group) return;
            if (!confirm(`Clear ALL assignments for "${group}"?`)) return;

            fetch('/admin/assignments/clear', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ deployment_group_name: group })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) loadAssignments();
                else alert('Error: ' + (data.error || 'Unknown error'));
            })
            .catch(err => alert('Error: ' + err.message));
        });
    }

    // Student search input handler
    const studentSearch = document.getElementById('student-search');
    if (studentSearch) {
        studentSearch.addEventListener('input', function() {
            if (window._allStudents) {
                renderStudentTable(window._allStudents, this.value);
            }
        });
    }

    // VM filter apply button
    const vmFilterApply = document.getElementById('vm-filter-apply');
    if (vmFilterApply) {
        vmFilterApply.addEventListener('click', loadAdminVMs);
    }

    // Bulk destroy button
    const bulkDestroyBtn = document.getElementById('bulk-destroy-btn');
    if (bulkDestroyBtn) {
        bulkDestroyBtn.addEventListener('click', function() {
            const ids = getSelectedVmIds();
            if (!ids.length) return;
            if (!confirm('Destroy ' + ids.length + ' selected VM(s)? This cannot be undone!')) return;
            fetch('/admin/bulk-destroy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ vm_ids: ids })
            })
            .then(r => r.json())
            .then(data => { if (data.success) loadAdminVMs(); else alert('Error: ' + (data.error || 'Unknown error')); })
            .catch(err => alert('Error: ' + err.message));
        });
    }

    // Bulk extend button
    const bulkExtendBtn = document.getElementById('bulk-extend-btn');
    if (bulkExtendBtn) {
        bulkExtendBtn.addEventListener('click', function() {
            const ids = getSelectedVmIds();
            if (!ids.length) return;
            if (!confirm('Extend ' + ids.length + ' selected VM(s) by 24 hours?')) return;
            fetch('/admin/bulk-extend', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ vm_ids: ids })
            })
            .then(r => r.json())
            .then(data => { if (data.success) loadAdminVMs(); else alert('Error: ' + (data.error || 'Unknown error')); })
            .catch(err => alert('Error: ' + err.message));
        });
    }

    // Random assign button
    const randomBtn = document.getElementById('random-assign-btn');
    if (randomBtn) {
        randomBtn.addEventListener('click', function() {
            const groupSelect = document.getElementById('random_assign_groups');
            const groups = Array.from(groupSelect.selectedOptions).map(o => o.value);
            if (groups.length < 2) { alert('Select at least 2 groups'); return; }
            fetch('/admin/students').then(r => r.json()).then(data => {
                if (!data.success) return;
                const students = data.students.filter(s => !s.is_admin);
                if (!confirm('Randomly assign ' + students.length + ' students across ' + groups.length + ' groups?')) return;
                const clearPromises = groups.map(g => fetch('/admin/assignments/clear', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({deployment_group_name: g}) }));
                Promise.all(clearPromises).then(() => {
                    const shuffled = students.sort(() => Math.random() - 0.5);
                    const byGroup = {};
                    shuffled.forEach((s, i) => { const g = groups[i % groups.length]; if (!byGroup[g]) byGroup[g] = []; byGroup[g].push(s.id); });
                    return Promise.all(Object.entries(byGroup).map(([group, userIds]) => fetch('/admin/assignments/bulk', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_ids: userIds, deployment_group_names: [group]}) })));
                }).then(() => { alert('Random assignment complete!'); loadAssignments(); });
            });
        });
    }
});
