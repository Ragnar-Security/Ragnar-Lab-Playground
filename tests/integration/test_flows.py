"""
Integration tests for critical user flows.
Requires: ``uv pip install -r requirements.txt``
Run: ``uv run --no-sync python -m pytest tests/integration/
--base-url http://localhost:5099``

These tests verify frontend-to-backend integration by driving a real browser.
Start the test server first with ``docker compose -f docker-compose.test.yml
up -d --build`` or run Flask locally as described in this directory's README.
"""
import re

import pytest
from playwright.sync_api import Page, expect

# Seeded by docker-compose.test.yml (TEST_STUDENT_PASSWORD).
TEST_STUDENT_PASSWORD = 'student-pass-123'


# ============ AUTH FLOW TESTS ============

class TestLoginFlow:
    """Test the login/logout flow end-to-end"""

    def test_login_page_loads(self, page: Page, base_url: str):
        """Login page renders with current portal branding."""
        page.goto(base_url)
        expect(page.locator('h3')).to_contain_text('Lab Playground')
        expect(page.locator('input#loginUsername')).to_be_visible()
        expect(page.locator('input#loginPassword')).to_be_visible()

    def test_login_page_uses_accessible_accent(self, page: Page, base_url: str):
        """Login page uses the high-contrast red accent, not the old purple."""
        page.goto(base_url)
        card_header = page.locator('.card-header')
        style = card_header.evaluate("el => getComputedStyle(el).backgroundImage")
        assert '102, 30' not in style or '226, 24, 51' in style

    def test_login_with_invalid_credentials(self, page: Page, base_url: str):
        """Login with wrong password shows error"""
        page.goto(base_url)
        page.fill('#loginUsername', 'nonexistent')
        page.fill('#loginPassword', 'wrongpassword')
        page.click('button[type="submit"]')
        # Should show error alert
        page.wait_for_selector('.alert-danger', timeout=5000)
        expect(page.locator('.alert-danger')).to_be_visible()

    def test_register_page_loads(self, page: Page, base_url: str):
        """Register page renders correctly"""
        page.goto(f"{base_url}/register")
        expect(page.locator('h3')).to_contain_text('Student Registration')
        expect(page.locator('input#registerLoginId')).to_be_visible()

    def test_register_page_password_min_length(self, page: Page, base_url: str):
        """Register page enforces 8 character minimum password"""
        page.goto(f"{base_url}/register")
        password_input = page.locator('input#registerPassword')
        # HTML5 minlength attribute should be 8
        minlength = password_input.get_attribute('minlength')
        assert minlength == '8', f"Expected minlength=8, got {minlength}"

    def test_reset_password_page_loads(self, page: Page, base_url: str):
        """Reset password page renders correctly"""
        page.goto(f"{base_url}/reset-password")
        expect(page.locator('h3')).to_contain_text('Reset Password')


# ============ DASHBOARD TESTS (require auth) ============

class TestDashboardFlow:
    """Test the main dashboard after login.
    These tests require a running server with an admin user created.
    """

    @pytest.fixture(autouse=True)
    def login_admin(self, page: Page, base_url: str):
        """Login as admin before each test"""
        page.goto(base_url)
        page.fill('#loginUsername', 'admin')
        page.fill('#loginPassword', 'testadmin123')
        page.click('button[type="submit"]')
        # Wait for redirect to dashboard
        page.wait_for_selector('#userName', timeout=10000)
        yield

    def test_dashboard_shows_branding(self, page: Page):
        """Dashboard navbar shows the current portal branding."""
        expect(page.locator('.navbar-brand')).to_contain_text('Lab Playground')

    def test_dashboard_shows_admin_badge(self, page: Page):
        """Admin user sees Admin badge"""
        expect(page.locator('#userRole')).to_contain_text('Admin')

    def test_admin_sees_all_tabs(self, page: Page):
        """Admin sees Assignments, Students, All VMs, Test Alerts tabs"""
        expect(page.locator('#assignments-tab')).to_be_visible()
        expect(page.locator('#admin-students-tab')).to_be_visible()
        expect(page.locator('#admin-vms-tab')).to_be_visible()
        expect(page.locator('#test-alerts-tab')).to_be_visible()

    def test_launch_lab_tab_loads_groups(self, page: Page):
        """Launch Lab tab shows deployment group dropdown"""
        expect(page.locator('#deployment_group')).to_be_visible()
        # Should have loaded at least the placeholder option
        options = page.locator('#deployment_group option').count()
        assert options >= 1

    def test_my_labs_tab(self, page: Page):
        """My Labs tab loads without error (admin can also view their own labs)"""
        page.click('[data-tab="my-labs"]')
        page.wait_for_timeout(2000)
        # Should show either VMs or "No active labs" message
        expect(page.locator('#my-labs-content')).to_be_visible()

    def test_assignments_tab(self, page: Page):
        """Assignments tab loads with form elements"""
        page.click('[data-tab="assignments"]')
        page.wait_for_timeout(2000)
        expect(page.locator('#assign_group')).to_be_visible()
        expect(page.locator('#assign_students')).to_be_visible()
        expect(page.locator('#assign-btn')).to_be_visible()

    def test_students_tab(self, page: Page):
        """Students tab loads student table"""
        page.click('[data-tab="admin-students"]')
        page.wait_for_timeout(2000)
        expect(page.locator('#student-search')).to_be_visible()
        # Should show at least the admin user in the table
        expect(page.locator('#students-table-container table')).to_be_visible()

    def test_all_vms_tab(self, page: Page):
        """All VMs tab loads with filters"""
        page.click('[data-tab="admin-vms"]')
        page.wait_for_timeout(2000)
        expect(page.locator('#vm-filter-student')).to_be_visible()
        expect(page.locator('#vm-filter-node')).to_be_visible()
        expect(page.locator('#vm-filter-status')).to_be_visible()

    def test_logout(self, page: Page):
        """Logout clears the session and returns to the login view."""
        page.click('#logoutBtn')
        # The dashboard and login page share '/', so URL waiting can finish
        # before the asynchronous logout request and navigation complete.
        expect(page.locator('input#loginUsername')).to_be_visible(timeout=10000)


# ============ ASSIGNMENT RESTRICTION TESTS ============

class TestAssignmentRestriction:
    """Test that lab assignment restrictions work end-to-end.
    These require specific setup (whitelist + student accounts).
    Marked as skip if setup not available.
    """

    def test_admin_can_create_assignment_via_api(self, page: Page, base_url: str):
        """Admin can create an assignment via the API"""
        # Login as admin
        page.goto(base_url)
        page.fill('#loginUsername', 'admin')
        page.fill('#loginPassword', 'testadmin123')
        page.click('button[type="submit"]')
        page.wait_for_selector('#userName', timeout=10000)

        # Use the API directly to test
        response = page.request.get(f"{base_url}/admin/assignments")
        data = response.json()
        assert data['success'] is True


# ============ STUDENT PERSPECTIVE TESTS ============

class TestStudentPerspective:
    """Test student-specific behavior"""

    @pytest.fixture(autouse=True)
    def login_student(self, page: Page, base_url: str):
        """Login as student before each test"""
        page.goto(base_url)
        page.fill('#loginUsername', 'teststudent')
        page.fill('#loginPassword', TEST_STUDENT_PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_selector('#userName', timeout=10000)
        yield

    def test_student_sees_student_badge(self, page: Page):
        """Student should see Student role badge, not Admin"""
        expect(page.locator('#userRole')).to_contain_text('Student')

    def test_student_does_not_see_admin_tabs(self, page: Page):
        """Student should NOT see Assignments, Students, All VMs, Test Alerts tabs"""
        expect(page.locator('#assignments-tab')).to_be_hidden()
        expect(page.locator('#admin-students-tab')).to_be_hidden()
        expect(page.locator('#admin-vms-tab')).to_be_hidden()
        expect(page.locator('#test-alerts-tab')).to_be_hidden()

    def test_student_cannot_access_admin_api(self, page: Page, base_url: str):
        """Student should get 403 on admin endpoints"""
        response = page.request.get(f"{base_url}/admin/students")
        assert response.status == 403

    def test_student_my_labs_tab(self, page: Page):
        """Student can navigate to My Labs tab"""
        page.click('[data-tab="my-labs"]')
        page.wait_for_timeout(2000)
        # Should show either VMs or "No active labs" message
        content = page.locator('#my-labs-content')
        expect(content).to_be_visible()


# ============ LOGIN VALIDATION TESTS ============

class TestLoginValidation:
    """Test login edge cases"""

    def test_login_with_valid_credentials_redirects(self, page: Page, base_url: str):
        """Successful login should redirect to dashboard"""
        page.goto(base_url)
        page.fill('#loginUsername', 'admin')
        page.fill('#loginPassword', 'testadmin123')
        page.click('button[type="submit"]')
        page.wait_for_selector('#userName', timeout=10000)
        # Should be on dashboard now
        expect(page.locator('.navbar-brand')).to_be_visible()

    def test_login_with_empty_fields(self, page: Page, base_url: str):
        """Login with empty fields should not submit (HTML5 required)"""
        page.goto(base_url)
        page.click('button[type="submit"]')
        # Should still be on login page (form validation prevents submit)
        expect(page.locator('input#loginUsername')).to_be_visible()


# ============ ADMIN WORKFLOW TESTS ============

class TestAdminWorkflows:
    """Test admin workflow operations"""

    @pytest.fixture(autouse=True)
    def login_admin(self, page: Page, base_url: str):
        """Login as admin before each test"""
        page.goto(base_url)
        page.fill('#loginUsername', 'admin')
        page.fill('#loginPassword', 'testadmin123')
        page.click('button[type="submit"]')
        page.wait_for_selector('#userName', timeout=10000)
        yield

    def test_student_management_search(self, page: Page):
        """Search box in Students tab filters the table"""
        page.click('[data-tab="admin-students"]')
        page.wait_for_selector('#students-table-container table', timeout=5000)
        # Type in search
        page.fill('#student-search', 'admin')
        page.wait_for_timeout(500)
        # Table should still be visible (search is client-side)
        expect(page.locator('#students-table-container table')).to_be_visible()

    def test_assignments_tab_assign_button_exists(self, page: Page):
        """Assignments tab has the full-width assign button"""
        page.click('[data-tab="assignments"]')
        page.wait_for_timeout(2000)
        expect(page.locator('#assign-btn')).to_be_visible()
        expect(page.locator('#assign_group')).to_be_visible()

    def test_all_vms_filter_controls(self, page: Page):
        """All VMs tab has filter dropdowns and buttons"""
        page.click('[data-tab="admin-vms"]')
        page.wait_for_timeout(2000)
        expect(page.locator('#vm-filter-student')).to_be_visible()
        expect(page.locator('#vm-filter-node')).to_be_visible()
        expect(page.locator('#vm-filter-status')).to_be_visible()
        expect(page.locator('#vm-filter-apply')).to_be_visible()


# ============ XSS RENDERING TESTS ============

IMG_PAYLOAD = '<img src=x onerror=window.__xss=1>'
SVG_PAYLOAD = '"><svg onload=window.__xss=1>'


class TestHostileDataRendering:
    """Names and labels reach the admin UI from places the admin does not
    control: Canvas imports, student-created labels, YAML uploads. Every view
    must show them as text. A payload that runs in an admin session can act as
    the admin, so each test fails if ``window.__xss`` is ever set.
    """

    @pytest.fixture(autouse=True)
    def login_admin(self, page: Page, base_url: str):
        page.goto(base_url)
        page.fill('#loginUsername', 'admin')
        page.fill('#loginPassword', 'testadmin123')
        page.click('button[type="submit"]')
        page.wait_for_selector('#userName', timeout=10000)
        yield

    @staticmethod
    def _serve(page: Page, glob: str, body: dict):
        page.route(glob, lambda route: route.fulfill(json=body))

    @staticmethod
    def _assert_inert(page: Page):
        # Give a pending image error or SVG load event time to fire.
        page.wait_for_timeout(500)
        assert page.evaluate('window.__xss') is None, 'payload executed'

    def test_student_table_shows_names_as_text(self, page: Page):
        self._serve(page, '**/admin/students/full', {'success': True, 'students': [{
            'id': 7, 'login_id': IMG_PAYLOAD, 'full_name': SVG_PAYLOAD,
            'is_admin': False, 'disabled': False, 'last_login': None,
            'entry_vlan_tag': 1, 'internal_vlan_tag': 2, 'active_vm_count': 0,
        }]})
        page.click('[data-tab="admin-students"]')
        table = page.locator('#students-table-container')
        expect(table).to_contain_text(IMG_PAYLOAD)
        expect(table).to_contain_text(SVG_PAYLOAD)
        self._assert_inert(page)

    def test_all_vms_table_shows_names_as_text(self, page: Page):
        self._serve(page, '**/admin/vms*', {'success': True, 'vms': [{
            'id': 3, 'vmid': 9001, 'vm_name': IMG_PAYLOAD,
            'student_name': SVG_PAYLOAD, 'template_name': IMG_PAYLOAD,
            'node': SVG_PAYLOAD, 'destroyed': False,
            'expires_at': '2030-01-01T00:00:00',
        }]})
        page.click('[data-tab="admin-vms"]')
        table = page.locator('#admin-vms-table-container')
        expect(table).to_contain_text(IMG_PAYLOAD)
        expect(table).to_contain_text(SVG_PAYLOAD)
        self._assert_inert(page)

    def test_assignments_show_names_as_text(self, page: Page):
        self._serve(page, '**/admin/assignments', {'success': True, 'assignments': [{
            'id': 5, 'user_login_id': IMG_PAYLOAD, 'user_full_name': SVG_PAYLOAD,
            'deployment_group_name': IMG_PAYLOAD,
            'assigned_at': '2030-01-01T00:00:00',
        }]})
        page.click('[data-tab="assignments"]')
        table = page.locator('#assignments-table-container')
        expect(table).to_contain_text(IMG_PAYLOAD)
        expect(table).to_contain_text(SVG_PAYLOAD)
        self._assert_inert(page)

    def test_assignment_form_options_show_names_as_text(self, page: Page):
        self._serve(page, '**/admin/students', {'success': True, 'students': [{
            'id': 7, 'login_id': IMG_PAYLOAD, 'full_name': SVG_PAYLOAD,
            'is_admin': False,
        }]})
        self._serve(page, '**/templates', {'success': True, 'deployment_groups': [{
            'display_name': SVG_PAYLOAD, 'description': IMG_PAYLOAD,
            'template_count': 1, 'templates': [],
        }]})
        page.click('[data-tab="assignments"]')
        expect(page.locator('#assign_students option')).to_have_text(
            f'{IMG_PAYLOAD} ({SVG_PAYLOAD})')
        expect(page.locator('#assign_group option').nth(1)).to_have_text(SVG_PAYLOAD)
        self._assert_inert(page)

    def test_student_group_views_show_names_as_text(self, page: Page, base_url: str):
        """Create the group through the real admin API, then open every view
        that renders it."""
        slug = f'xss-{int(page.evaluate("Date.now()"))}'
        created = page.request.post(f'{base_url}/admin/groups', data={
            'name': slug, 'display_name': IMG_PAYLOAD,
            'description': SVG_PAYLOAD,
        })
        assert created.status == 201, created.text()
        group_id = created.json()['group']['id']
        self._serve(page, f'**/admin/groups/{group_id}/members', {'members': [{
            'user_id': 4, 'user_login_id': IMG_PAYLOAD, 'user_full_name': SVG_PAYLOAD,
        }]})
        try:
            page.click('[data-tab="admin-groups"]')
            row = page.locator('#groups-table tbody tr', has_text=slug)
            expect(row).to_contain_text(IMG_PAYLOAD)
            row.locator('.js-view').click()
            detail = page.locator('#group-detail-body')
            expect(detail.locator('#sg-display-name')).to_have_value(IMG_PAYLOAD)
            expect(detail.locator('#sg-description')).to_have_value(SVG_PAYLOAD)
            expect(detail.locator('#group-members')).to_contain_text(IMG_PAYLOAD)
            expect(detail.locator('#group-members')).to_contain_text(SVG_PAYLOAD)
            self._assert_inert(page)
        finally:
            page.request.delete(f'{base_url}/admin/groups/{group_id}')

    def test_launch_panel_shows_lab_details_as_text(self, page: Page):
        self._serve(page, '**/templates', {'success': True, 'deployment_groups': [{
            'display_name': 'Hostile Lab', 'description': IMG_PAYLOAD,
            'template_count': 1,
            'templates': [{'name': SVG_PAYLOAD, 'type': 'vm', 'memory': 512, 'cores': 1}],
        }]})
        page.reload()
        page.wait_for_selector('#userName', timeout=10000)
        page.select_option('#deployment_group', label='Hostile Lab (1 VM)')
        expect(page.locator('#deployment-description')).to_contain_text(IMG_PAYLOAD)
        expect(page.locator('#deployment-templates')).to_contain_text(SVG_PAYLOAD)
        self._assert_inert(page)

    def test_my_labs_shows_names_as_text(self, page: Page):
        self._serve(page, '**/list', {'success': True, 'students': [{
            'student_name': SVG_PAYLOAD,
            'deployment_groups': [{
                'group_name': IMG_PAYLOAD, 'vm_count': 1,
                'time_remaining_seconds': 7200,
                'vms': [{
                    'name': IMG_PAYLOAD, 'vmid': 9001, 'type': 'vm',
                    'node': SVG_PAYLOAD, 'ip_address': SVG_PAYLOAD,
                    'is_entry_point': False, 'memory': 512, 'cores': 1,
                    'status': 'running',
                }],
            }],
        }]})
        page.click('[data-tab="my-labs"]')
        content = page.locator('#my-labs-content')
        expect(content).to_contain_text(IMG_PAYLOAD)
        expect(content).to_contain_text(SVG_PAYLOAD)
        self._assert_inert(page)


# ============ ADMIN-ISSUED RESET LINK ============

class TestAdminResetLink:
    """An admin creates a reset link in the Students tab and hands it over;
    the student opens it in their own browser and signs in with the new
    password. Other tests sign in as teststudent with its seeded password,
    so this one restores that password through a second link."""

    STUDENT = 'teststudent'
    NEW_PASSWORD = 'link-reset-pass-9'

    @pytest.fixture(autouse=True)
    def login_admin(self, page: Page, base_url: str):
        page.goto(base_url)
        page.fill('#loginUsername', 'admin')
        page.fill('#loginPassword', 'testadmin123')
        page.click('button[type="submit"]')
        page.wait_for_selector('#userName', timeout=10000)
        yield

    def _create_link(self, admin: Page) -> str:
        admin.click('[data-tab="admin-students"]')
        admin.wait_for_selector('#students-table-container table', timeout=5000)
        admin.fill('#student-search', self.STUDENT)
        admin.once('dialog', lambda dialog: dialog.accept())
        with admin.expect_response('**/reset-link') as issued:
            admin.click(f'.js-reset-link[data-login="{self.STUDENT}"]')
        reset_url = issued.value.json()['reset_url']
        # The panel shows the absolute form of the one link just issued.
        link = admin.locator('#reset-link-url')
        expect(link).to_have_value(re.compile(re.escape(reset_url) + '$'))
        return link.input_value()

    @staticmethod
    def _submit_new_password(student: Page, url: str, password: str):
        student.goto(url)
        expect(student.locator('#tokenResetForm')).to_be_visible()
        student.fill('#tokenPassword', password)
        student.fill('#tokenPasswordConfirm', password)
        student.click('#tokenResetForm button[type="submit"]')

    def _reset_in_fresh_browser(self, browser, url: str, password: str):
        context = browser.new_context()
        try:
            student = context.new_page()
            self._submit_new_password(student, url, password)
            expect(student.locator('.alert-success')).to_be_visible()
        finally:
            context.close()

    def test_student_uses_link_then_signs_in(self, page: Page, browser, base_url: str):
        url = self._create_link(page)
        assert '#token=' in url and '?' not in url

        context = browser.new_context()
        try:
            student = context.new_page()
            self._submit_new_password(student, url, self.NEW_PASSWORD)
            # Read once, then gone from the address bar and history.
            assert '#token=' not in student.url
            expect(student.locator('.alert-success')).to_be_visible()

            student.wait_for_selector('#loginUsername', timeout=10000)
            student.fill('#loginUsername', self.STUDENT)
            student.fill('#loginPassword', self.NEW_PASSWORD)
            student.click('button[type="submit"]')
            student.wait_for_selector('#userName', timeout=10000)
            expect(student.locator('#userRole')).to_contain_text('Student')
        finally:
            context.close()
            self._reset_in_fresh_browser(browser, self._create_link(page), TEST_STUDENT_PASSWORD)

    def test_used_link_is_refused(self, page: Page, browser, base_url: str):
        url = self._create_link(page)
        self._reset_in_fresh_browser(browser, url, TEST_STUDENT_PASSWORD)

        context = browser.new_context()
        try:
            replay = context.new_page()
            self._submit_new_password(replay, url, 'replayed-pass-7')
            expect(replay.locator('.alert-danger')).to_contain_text('already used')
        finally:
            context.close()
