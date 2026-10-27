// ============ AUTHENTICATION ============

// Load user information
function loadUserInfo() {
    fetch('/auth/me')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.user) {
                window.currentUser = data.user;
                document.getElementById('userName').textContent = data.user.login_id;
                const roleSpan = document.getElementById('userRole');
                if (data.user.is_admin) {
                    roleSpan.textContent = 'Admin';
                    roleSpan.className = 'badge bg-danger ms-2';
                    // Show admin-only tabs
                    const testAlertsTab = document.getElementById('test-alerts-tab');
                    if (testAlertsTab) {
                        testAlertsTab.style.display = 'block';
                    }
                    const assignmentsTab = document.getElementById('assignments-tab');
                    if (assignmentsTab) {
                        assignmentsTab.style.display = 'block';
                    }
                    const studentsTab = document.getElementById('admin-students-tab');
                    if (studentsTab) studentsTab.style.display = 'block';
                    const vmsTab = document.getElementById('admin-vms-tab');
                    if (vmsTab) vmsTab.style.display = 'block';
                    const groupsTab = document.getElementById('admin-groups-tab');
                    if (groupsTab) groupsTab.style.display = 'block';
                    const kothTab = document.getElementById('admin-koth-tab');
                    if (kothTab) kothTab.style.display = 'block';
                } else {
                    roleSpan.textContent = 'Student';
                    roleSpan.className = 'badge bg-primary ms-2';
                }

                // Populate Tailscale setup with headscale key
                const tailscaleSetup = document.getElementById('tailscale-setup');
                const tailscaleKeySpan = document.getElementById('tailscale-auth-key');
                if (tailscaleSetup && data.user.headscale_key) {
                    tailscaleKeySpan.textContent = data.user.headscale_key;
                    tailscaleSetup.style.display = 'block';
                } else if (tailscaleSetup) {
                    tailscaleSetup.style.display = 'none';
                }
            } else {
                // Not logged in, redirect to login
                window.location.href = '/';
            }
        })
        .catch(error => {
            console.error('Error loading user info:', error);
            window.location.href = '/';
        });
}

// Logout handler
document.getElementById('logoutBtn').addEventListener('click', function() {
    fetch('/auth/logout', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                window.location.href = '/';
            }
        })
        .catch(error => {
            console.error('Logout error:', error);
            window.location.href = '/';
        });
});
