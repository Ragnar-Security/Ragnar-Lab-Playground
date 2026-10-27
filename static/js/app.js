// ============ APP INITIALIZATION ============

// Load user info on page load
document.addEventListener('DOMContentLoaded', function() {
    loadUserInfo();
});

// Tab Navigation — only intercept buttons; <a> sidebar entries navigate normally.
document.querySelectorAll('button.list-group-item').forEach(button => {
    button.addEventListener('click', function(e) {
        e.preventDefault();

        // Remove active class from all buttons
        document.querySelectorAll('.list-group-item').forEach(btn => {
            btn.classList.remove('active');
        });

        // Add active class to clicked button
        this.classList.add('active');

        // Hide all tab contents
        document.querySelectorAll('.tab-content').forEach(tab => {
            tab.classList.add('d-none');
        });

        // Show selected tab
        const tabName = this.getAttribute('data-tab');
        let tabElement = document.getElementById(`${tabName}-tab`);

        // Handle special cases for admin tabs
        if (tabName === 'test-alerts') {
            tabElement = document.getElementById('test-alerts-tab-content');
        } else if (tabName === 'assignments') {
            tabElement = document.getElementById('assignments-tab-content');
        } else if (tabName === 'admin-students') {
            tabElement = document.getElementById('admin-students-tab-content');
        } else if (tabName === 'admin-vms') {
            tabElement = document.getElementById('admin-vms-tab-content');
        } else if (tabName === 'admin-groups') {
            tabElement = document.getElementById('admin-groups-tab-content');
        }

        if (tabElement) {
            tabElement.classList.remove('d-none');

            // Auto-load list when switching to my-labs tab
            if (tabName === 'my-labs') {
                loadVMList();
            }

            // Load templates when switching to create tab
            if (tabName === 'create') {
                loadDeploymentGroups();
            }

            // Load assignments when switching to assignments tab
            if (tabName === 'assignments') {
                loadAssignments();
                loadAssignmentFormData();
            }

            // Load admin tabs
            if (tabName === 'admin-students') { loadStudentManagement(); }
            if (tabName === 'admin-vms') { loadAdminVMs(); }
            if (tabName === 'admin-groups' && window.StudentGroupsAdmin) { window.StudentGroupsAdmin.loadGroups(); }
        }
    });
});

// Initialize - load deployment groups on page load
document.addEventListener('DOMContentLoaded', function() {
    // Page is ready
    console.log('Lab Playground dashboard loaded');
    loadDeploymentGroups();
});
