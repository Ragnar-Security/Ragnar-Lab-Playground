// ============ VM OPERATIONS ============

const IPV4_PATTERN = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;
const DEFAULT_RDP_PORT = 3389;

// Build the Connect cell. RDP templates get a copyable mstsc command, a
// generated .rdp file, and the username; everything else keeps the SSH-only
// presentation it has always had. The cell offers an action only when the
// destination is a real address, the port is valid, and no field can break
// the line-oriented .rdp file.
function connectCell(vm) {
  if (!vm.connect || vm.connect.protocol !== 'rdp') return '<td>SSH via jumpbox</td>';
  // A control character in any raw field could add a directive to the
  // line-oriented .rdp file. Check the raw values before any trim or coercion.
  const rawFields = [vm.ip_address, vm.connect.port, vm.connect.username];
  if (rawFields.some(v => v !== undefined && v !== null && /[\x00-\x1f\x7f]/.test(String(v)))) {
    return '<td><span class="text-danger">unavailable: a connection field has a control character</span></td>';
  }
  const ip = (vm.ip_address || '').split(',')[0].trim();
  if (ip === 'CONFIG ERROR') {
    return '<td><span class="text-danger">unavailable: VM configuration error</span></td>';
  }
  if (/x/i.test(ip) && !IPV4_PATTERN.test(ip)) {
    return '<td><em>address hidden for this lab</em></td>';
  }
  if (!IPV4_PATTERN.test(ip)) return '<td><em>waiting for boot&hellip;</em></td>';

  const portValue = vm.connect.port === undefined || vm.connect.port === null
    ? DEFAULT_RDP_PORT : Number(vm.connect.port);
  if (!Number.isInteger(portValue) || portValue < 1 || portValue > 65535) {
    return '<td><span class="text-danger">unavailable: invalid RDP port in template</span></td>';
  }
  const target = portValue === DEFAULT_RDP_PORT ? ip : `${ip}:${portValue}`;
  const user = String(vm.connect.username || 'student');
  const cmd  = `mstsc /v:${target}`;
  const userHtml = `<div class="muted">user: <code>${escapeHtml(user)}</code></div>`;

  // redirectdrives: expiry deletes the VM, so \\tsclient\ is how work survives.
  const rdp = [
    'full address:s:' + target,
    'username:s:' + user,
    'redirectdrives:i:1',
    'audiomode:i:2',
    'authentication level:i:2',
    'screen mode id:i:2'
  ].join('\r\n');
  const href = 'data:application/x-rdp;charset=utf-8,' + encodeURIComponent(rdp);
  // Three affordances, in the order a student should reach for them.
  //
  // `.rdp` first, because it needs no typing at all: it carries the address
  // AND the username. Then "copy address", which is what belongs in the
  // Computer box of an already-open Remote Desktop window. Then "copy
  // command", for a Run box or a terminal.
  //
  // Both copy buttons exist because one of them used to be alone and it
  // copied the whole `mstsc /v:...` command. Pasted into the Computer box
  // that fails, and typing the address by hand produced `192.0.2.224`
  // from `192.0.2.24`. Remote Desktop reports only "can't connect", which
  // sends the reader looking at the VM instead of at the address.
  return `<td>
    <a class="rdp-download" download="lab.rdp" href="${href}">open .rdp</a>
    <button class="btn btn-sm btn-outline-light btn-copy" data-copy="${escapeHtml(target)}">copy address</button>
    <button class="btn btn-sm btn-outline-light btn-copy" data-copy="${escapeHtml(cmd)}">copy command</button>
    <div class="muted"><code>${escapeHtml(cmd)}</code></div>
    ${userHtml}
    <div><a href="#" class="rdp-help small"
            data-address="${escapeHtml(target)}"
            data-user="${escapeHtml(user)}">how do I connect?</a></div>
  </td>`;
}

// One delegated listener serves every copy button; the text comes from a
// data attribute, so it never passes through inline script.
document.addEventListener('click', function(event) {
  const button = event.target.closest('.btn-copy');
  if (button) navigator.clipboard.writeText(button.dataset.copy);
});

// "how do I connect?" opens one shared modal, filled with THIS row's address
// and user. A student who has never used RDP needs the client named and the
// steps spelled out, and the address has to be the one they actually have.
// Values travel in data attributes and are written with textContent, so a
// hostile field cannot become markup.
document.addEventListener('click', function(event) {
  const link = event.target.closest('.rdp-help');
  if (!link) return;
  event.preventDefault();

  // The same modal opens from two places. From My Labs the row knows its
  // address. From the Create panel the VM does not exist yet, so say where
  // the address will appear instead of showing an empty box.
  const address = link.dataset.address || '';
  const user    = link.dataset.user || 'student';
  const shown   = address || 'shown in the Connect column once the lab is running';

  const addrEl = document.getElementById('rdpHelpAddress');
  const userEl = document.getElementById('rdpHelpUser');
  if (addrEl) addrEl.textContent = shown;
  if (userEl) userEl.textContent = user;
  // The Linux example needs something runnable-looking either way.
  const cmdAddress = address || 'ADDRESS';
  document.querySelectorAll('.rdp-help-address').forEach(el => { el.textContent = cmdAddress; });
  document.querySelectorAll('.rdp-help-user').forEach(el => { el.textContent = user; });

  const el = document.getElementById('rdpHelpModal');
  if (el && window.bootstrap) bootstrap.Modal.getOrCreateInstance(el).show();
});

// Load deployment groups
function loadDeploymentGroups(retryCount = 0) {
    console.log('Loading deployment groups...');
    const deploymentGroupSelect = document.getElementById('deployment_group');

    fetch('/templates')
    .then(response => response.json())
    .then(data => {
        console.log('Deployment groups response:', data);
        if (data.success && data.deployment_groups && data.deployment_groups.length > 0) {
            deploymentGroupSelect.innerHTML = '<option value="">-- Select a deployment group --</option>';
            deploymentGroupSelect.disabled = false;

            // Store deployment groups data for later use
            window.deploymentGroupsData = {};

            data.deployment_groups.forEach(group => {
                const option = document.createElement('option');
                option.value = group.display_name;
                option.textContent = `${group.display_name} (${group.template_count} VM${group.template_count > 1 ? 's' : ''})`;
                deploymentGroupSelect.appendChild(option);

                // Store group data
                window.deploymentGroupsData[group.display_name] = group;
            });

            console.log(`Loaded ${data.deployment_groups.length} deployment groups`);

            // Add change event listener to show deployment details (only once)
            if (!deploymentGroupSelect.dataset.listenerAdded) {
                deploymentGroupSelect.addEventListener('change', function() {
                    const selectedGroup = this.value;
                    const detailsDiv = document.getElementById('deployment-details');
                    const descriptionDiv = document.getElementById('deployment-description');
                    const templatesDiv = document.getElementById('deployment-templates');

                    if (selectedGroup && window.deploymentGroupsData[selectedGroup]) {
                        const group = window.deploymentGroupsData[selectedGroup];

                        // Show description
                        descriptionDiv.innerHTML = `<p class="mb-2">${escapeHtml(group.description || 'No description')}</p>`;

                        // Show templates list
                        let templatesHTML = '<strong>VMs to be created:</strong><ul class="mt-1 mb-0">';
                        group.templates.forEach((template, index) => {
                            const vmLabel = template.name || `vm${index + 1}`;
                            templatesHTML += `<li><code>${escapeHtml(vmLabel)}</code> (${escapeHtml(template.type.toUpperCase())}, ${escapeHtml(template.memory)}MB RAM, ${escapeHtml(template.cores)} core${template.cores > 1 ? 's' : ''})</li>`;
                        });
                        templatesHTML += '</ul>';

                        // An RDP lab needs a word about HOW to reach it, at the
                        // moment the student selects it. The Connect column only
                        // appears once the VM is running, and a student reading
                        // this panel has not got there yet.
                        const rdpTemplate = group.templates.find(
                            t => t.connect && t.connect.protocol === 'rdp');
                        if (rdpTemplate) {
                            const who = escapeHtml(String(rdpTemplate.connect.username || 'student'));
                            templatesHTML += `
                              <div class="mt-2 small">
                                <i class="bi bi-display"></i>
                                This is a <strong>Windows desktop</strong>. You reach it with
                                Remote Desktop, signing in as <code>${who}</code>.
                                You must be on the course VPN.
                                Once it starts, use the <strong>Connect</strong> column in
                                <strong>My Labs</strong> below.
                                <a href="#" class="rdp-help" data-address="" data-user="${who}">Read how to connect</a>.
                              </div>`;
                        }
                        templatesDiv.innerHTML = templatesHTML;

                        detailsDiv.style.display = 'block';

                        toggleAdminGroupModeFields(group);
                    } else {
                        detailsDiv.style.display = 'none';
                        toggleAdminGroupModeFields(null);
                    }
                });
                deploymentGroupSelect.dataset.listenerAdded = 'true';
            }
        } else {
            deploymentGroupSelect.innerHTML = '<option value="">No deployment groups available</option>';
            deploymentGroupSelect.disabled = true;
            console.warn('No deployment groups found:', data);
        }
    })
    .catch(error => {
        console.error('Error loading deployment groups:', error);
        deploymentGroupSelect.innerHTML = '<option value="">Error loading - retrying...</option>';
        deploymentGroupSelect.disabled = true;

        // Auto-retry up to 3 times with 10 second delay
        if (retryCount < 3) {
            console.log(`Retrying in 10 seconds... (attempt ${retryCount + 1}/3)`);
            setTimeout(() => loadDeploymentGroups(retryCount + 1), 10000);
        } else {
            deploymentGroupSelect.innerHTML = '<option value="">Failed to load deployment groups</option>';
        }
    });
}

// Show or hide the admin "deploy on behalf of group" fields based on selected lab.
// Only renders for admins; for everyone else this is a no-op (the block stays hidden).
function toggleAdminGroupModeFields(group) {
    const block = document.getElementById('admin-group-mode-fields');
    if (!block) return;
    const isAdmin = !!(window.currentUser && window.currentUser.is_admin);
    const needsGroup = !!(group && group.group_mode);
    if (!isAdmin || !needsGroup) {
        block.style.display = 'none';
        const slug = document.getElementById('admin_group_slug');
        if (slug) slug.value = '';
        return;
    }
    block.style.display = '';
    const slugSelect = document.getElementById('admin_group_slug');
    if (slugSelect.dataset.populated === 'true') return;
    fetch('/admin/groups')
        .then(r => r.json())
        .then(data => {
            const groups = (data && data.groups) || [];
            slugSelect.innerHTML = '<option value="">-- Select a student group --</option>'
                + groups.map(g => `<option value="${escapeHtml(g.name)}">${escapeHtml(g.display_name)} (${escapeHtml(g.name)})</option>`).join('');
            slugSelect.dataset.populated = 'true';
        })
        .catch(err => console.error('Failed to load student groups:', err));
}

// Create VM Form
document.getElementById('create-form').addEventListener('submit', function(e) {
    e.preventDefault();

    const deploymentGroupSelect = document.getElementById('deployment_group');
    const deploymentGroup = deploymentGroupSelect.value;

    if (!deploymentGroup) {
        showMessage('create-result', '<strong>Error:</strong> Please select a deployment group', 'error');
        return;
    }

    // For admins deploying a group-mode lab, require a group selection up front.
    const groupModeFields = document.getElementById('admin-group-mode-fields');
    if (groupModeFields && groupModeFields.style.display !== 'none') {
        const slug = document.getElementById('admin_group_slug').value;
        if (!slug) {
            showMessage('create-result', '<strong>Error:</strong> Pick the student group to deploy on behalf of.', 'error');
            return;
        }
    }

    // Get group details for display
    const group = window.deploymentGroupsData[deploymentGroup];
    const vmCount = group ? group.template_count : 1;

    // Show progress message
    const resultDiv = document.getElementById('create-result');
    resultDiv.innerHTML = `<div class="alert alert-info">
        <div class="d-flex align-items-center">
            <div class="spinner-border spinner-border-sm me-2" role="status">
                <span class="visually-hidden">Creating...</span>
            </div>
            <div>
                <strong>Creating ${escapeHtml(vmCount)} VM${vmCount > 1 ? 's' : ''}...</strong><br>
                <small>This may take 1-3 minutes per VM. Please wait while the VMs are created, started, and configured.</small>
            </div>
        </div>
    </div>`;

    const formData = new FormData();
    formData.append('deployment_group', deploymentGroup);
    if (groupModeFields && groupModeFields.style.display !== 'none') {
        formData.append('group_slug', document.getElementById('admin_group_slug').value);
    }

    fetch('/create', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            let message = `
                <strong>VM(s) Created Successfully!</strong><br>
                <small>Deployment Group: <code>${escapeHtml(data.deployment_group)}</code></small><br>
                <small>Total VMs: <code>${escapeHtml(data.vm_count)}</code></small><br>
                <small class="text-info"><i class="bi bi-info-circle"></i> VMs are starting up. IP addresses will be available shortly.</small><br>
                <hr class="my-2">
            `;

            // Add details for each VM
            if (data.vms && data.vms.length > 0) {
                message += '<strong>VMs Created:</strong><ul class="mb-0 mt-1">';
                data.vms.forEach(vm => {
                    message += `<li><strong>${escapeHtml(vm.template_name)}:</strong> `;
                    message += `VM ID <code>${escapeHtml(vm.vm_id)}</code>, `;
                    message += `Name <code>${escapeHtml(vm.vm_name)}</code>`;
                    if (vm.ip_address) {
                        message += `, IP <code>${escapeHtml(vm.ip_address)}</code>`;
                    }
                    message += `</li>`;
                });
                message += '</ul>';
            }

            showMessage('create-result', message, 'success');
            document.getElementById('create-form').reset();
            document.getElementById('deployment-details').style.display = 'none';
            toggleAdminGroupModeFields(null);
            loadDeploymentGroups(); // Reload deployment groups
        } else {
            showMessage('create-result', `<strong>Error:</strong> ${escapeHtml(data.error)}`, 'error');
        }
    })
    .catch(error => {
        showMessage('create-result', `<strong>Error:</strong> ${escapeHtml(error.message)}`, 'error');
    });
});

// List VMs - unified My Labs view with inline actions
function loadVMList() {
    clearCountdownTimers();
    showLoading('my-labs-content');

    fetch('/list')
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const students = data.students;
            if (students.length === 0) {
                document.getElementById('my-labs-content').innerHTML = `
                    <div class="text-center py-5">
                        <i class="bi bi-display" style="font-size: 3rem; color: #ccc;"></i>
                        <p class="mt-3 text-muted">No active labs. Go to <strong>Launch Lab</strong> to deploy a new environment.</p>
                    </div>`;
                return;
            }

            let html = '';
            students.forEach(student => {
                student.deployment_groups.forEach(group => {
                    const isExpired = group.time_remaining_seconds <= 0;
                    const isLow = group.time_remaining_seconds > 0 && group.time_remaining_seconds < 3600;
                    const timerClass = isExpired ? 'bg-danger' : (group.time_remaining_seconds < 600 ? 'bg-danger' : group.time_remaining_seconds < 1800 ? 'bg-warning text-dark' : 'bg-success');

                    html += `
                    <div class="lab-card mb-4">
                        <div class="lab-card-header">
                            <div>
                                <h5 class="mb-1">${escapeHtml(group.group_name)}</h5>
                                <span class="lab-meta">
                                    <i class="bi bi-person"></i> ${escapeHtml(student.student_name)}
                                    <span class="mx-2">&middot;</span>
                                    <i class="bi bi-hdd"></i> ${escapeHtml(group.vm_count)} VM${group.vm_count > 1 ? 's' : ''}
                                </span>
                            </div>
                            <div class="d-flex align-items-center gap-2">
                                ${isExpired
                                    ? '<span class="badge bg-danger fs-6">EXPIRED</span>'
                                    : `<span class="countdown-timer badge ${timerClass} fs-6" data-remaining="${escapeHtml(group.time_remaining_seconds)}">...</span>`
                                }
                                <button class="btn btn-sm btn-warning extend-lab-btn" data-group="${escapeHtml(group.group_name)}" data-student="${escapeHtml(student.student_name)}" ${!isLow && !isExpired ? 'disabled' : ''}>
                                    <i class="bi bi-clock"></i> Extend
                                </button>
                                <button class="btn btn-sm btn-danger destroy-lab-btn" data-group="${escapeHtml(group.group_name)}" data-student="${escapeHtml(student.student_name)}">
                                    <i class="bi bi-trash"></i> Destroy
                                </button>
                            </div>
                        </div>
                        <div class="lab-card-body">
                            <table class="table table-sm table-hover mb-0">
                                <thead>
                                    <tr><th>VM Name</th><th>VMID</th><th>Type</th><th>Node</th><th>IP Address</th><th>Connect</th><th>Resources</th></tr>
                                </thead>
                                <tbody>`;

                    group.vms.forEach(vm => {
                        let ipDisplay;
                        if (!vm.ip_address || vm.ip_address === '-') {
                            ipDisplay = '<span class="text-muted">-</span>';
                        } else if (vm.is_entry_point) {
                            ipDisplay = escapeHtml(vm.ip_address);
                        } else {
                            ipDisplay = `<span class="text-warning" title="Internal IP">${escapeHtml(vm.ip_address)} <small>(internal)</small></span>`;
                        }
                        const hasConfigIssues = vm.has_config_issues || vm.status === 'error';
                        const rowClass = hasConfigIssues ? 'table-danger' : '';
                        const statusBadge = hasConfigIssues
                            ? '<span class="badge bg-danger ms-2"><i class="bi bi-exclamation-triangle"></i> CONFIG ERROR</span>'
                            : '';

                        html += `
                                    <tr class="${rowClass}">
                                        <td><code>${escapeHtml(vm.name)}</code>${statusBadge}</td>
                                        <td><code>${escapeHtml(vm.vmid)}</code></td>
                                        <td><span class="badge bg-info">${escapeHtml(vm.type.toUpperCase())}</span></td>
                                        <td>${escapeHtml(vm.node)}</td>
                                        <td>${ipDisplay}</td>
                                        ${connectCell(vm)}
                                        <td><small>${escapeHtml(vm.memory)}MB, ${escapeHtml(vm.cores)} core${vm.cores > 1 ? 's' : ''}</small></td>
                                    </tr>`;
                    });

                    html += `
                                </tbody>
                            </table>
                        </div>
                    </div>`;
                });
            });

            document.getElementById('my-labs-content').innerHTML = html;
            startCountdownTimers();

            // Attach extend handlers
            document.querySelectorAll('.extend-lab-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    const group = this.dataset.group;
                    const student = this.dataset.student;
                    if (!confirm('Extend all VMs in "' + group + '"?')) return;
                    const formData = new FormData();
                    formData.append('deployment_group', group);
                    formData.append('student_name', student);
                    this.disabled = true;
                    this.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Extending...';
                    fetch('/extend', { method: 'POST', body: formData })
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                setTimeout(loadVMList, 1000);
                            } else {
                                alert('Error: ' + (data.error || 'Unknown error'));
                                this.disabled = false;
                                this.innerHTML = '<i class="bi bi-clock"></i> Extend Time';
                            }
                        })
                        .catch(err => {
                            alert('Error: ' + err.message);
                            this.disabled = false;
                            this.innerHTML = '<i class="bi bi-clock"></i> Extend Time';
                        });
                });
            });

            // Attach destroy handlers
            document.querySelectorAll('.destroy-lab-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    const group = this.dataset.group;
                    const student = this.dataset.student;
                    if (!confirm('DESTROY all VMs in "' + group + '"? This cannot be undone!')) return;
                    const formData = new FormData();
                    formData.append('deployment_group', group);
                    formData.append('student_name', student);
                    this.disabled = true;
                    this.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Destroying...';
                    fetch('/destroy', { method: 'POST', body: formData })
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                setTimeout(loadVMList, 1000);
                            } else {
                                alert('Error: ' + (data.error || 'Unknown error'));
                                this.disabled = false;
                                this.innerHTML = '<i class="bi bi-trash"></i> Destroy Lab';
                            }
                        })
                        .catch(err => {
                            alert('Error: ' + err.message);
                            this.disabled = false;
                            this.innerHTML = '<i class="bi bi-trash"></i> Destroy Lab';
                        });
                });
            });
        } else {
            showMessage('my-labs-content', '<strong>Error:</strong> ' + escapeHtml(data.error), 'error');
        }
    })
    .catch(error => {
        showMessage('my-labs-content', '<strong>Error:</strong> ' + escapeHtml(error.message), 'error');
    });
}

// Refresh button handler for My Labs
document.addEventListener('DOMContentLoaded', function() {
    const refreshBtn = document.getElementById('refresh-labs');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', loadVMList);
    }
});

// Load VM details for expandable row in list view
function loadVMDetails(vmid) {
    const detailsRow = document.getElementById(`vm-details-${vmid}`);
    const detailsCell = detailsRow.querySelector('td');

    // Show loading spinner
    detailsCell.innerHTML = `
        <div class="p-3 text-center">
            <div class="spinner-border spinner-border-sm" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
        </div>
    `;

    fetch(`/api/vms/${vmid}`)
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const vm = data.vm;
            let detailsHTML = `
                <div class="p-3">
                    <h6 class="mb-3"><strong>VM Information</strong></h6>
                    <div class="row">
                        <div class="col-md-6">
                            <p><strong>VM ID:</strong> <code>${escapeHtml(vm.vmid)}</code></p>
                            <p><strong>Name:</strong> <code>${escapeHtml(vm.name)}</code></p>
                            <p><strong>Type:</strong> <code>${escapeHtml(vm.type.toUpperCase())}</code></p>
                            <p><strong>Node:</strong> <code>${escapeHtml(vm.node)}</code></p>
                            <p><strong>Status:</strong> <span class="badge bg-${vm.status === 'running' ? 'success' : 'danger'}">${escapeHtml(vm.status)}</span></p>
                        </div>
                        <div class="col-md-6">
                            <p><strong>Memory:</strong> <code>${vm.memory ? escapeHtml(vm.memory) + ' MB' : 'N/A'}</code></p>
                            <p><strong>Cores:</strong> <code>${escapeHtml(vm.cores || 'N/A')}</code></p>
                            <p><strong>Uptime:</strong> <code>${vm.uptime ? Math.floor(vm.uptime / 3600) + 'h' : 'N/A'}</code></p>
                        </div>
                    </div>

                    <h6 class="mt-3"><strong>IP Addresses:</strong></h6>
                    <div class="ps-3">
                        ${vm.ip_addresses.map(ip => `<code>${escapeHtml(ip)}</code><br>`).join('')}
                    </div>
            `;

            // Add database info if available
            if (vm.expires_at) {
                detailsHTML += `
                    <h6 class="mt-3"><strong>Expiration Information:</strong></h6>
                    <div class="ps-3">
                        <p><strong>Created:</strong> <code>${escapeHtml(formatDateEST(vm.created_at))}</code></p>
                        <p><strong>Expires:</strong> <code>${escapeHtml(formatDateEST(vm.expires_at))}</code></p>
                        <p><strong>Time Remaining:</strong> <span class="badge bg-${vm.time_remaining_seconds > 0 ? 'info' : 'warning'}">${escapeHtml(vm.time_remaining || 'EXPIRED')}</span></p>
                    </div>
                `;
            }

            detailsHTML += `</div>`;

            detailsCell.innerHTML = detailsHTML;
        } else {
            detailsCell.innerHTML = `
                <div class="p-3">
                    <div class="alert alert-danger mb-0">
                        <strong>Error:</strong> ${escapeHtml(data.error)}
                    </div>
                </div>
            `;
        }
    })
    .catch(error => {
        detailsCell.innerHTML = `
            <div class="p-3">
                <div class="alert alert-danger mb-0">
                    <strong>Error:</strong> ${escapeHtml(error.message)}
                </div>
            </div>
        `;
    });
}

