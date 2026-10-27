# User Authentication Implementation - Complete Guide

## Overview
This guide covers the complete user authentication system that has been implemented. The system enforces login requirements on all routes and provides role-based access control for students and administrators.

## Key Features

### 1. **User Registration & Login**
- Users register with their Canvas login ID
- Login IDs are validated against a student whitelist
- Passwords are securely hashed using Werkzeug
- Session-based authentication with Flask-Login

### 2. **Role-Based Access Control**
- **Students**: Can only see and manage VMs they created
- **Admins**: Can see and manage all VMs

### 3. **Protected Routes**
All API endpoints are now protected with `@login_required` decorator:
- `/create` - Create new VM (uses logged-in user's login_id)
- `/list` - List VMs (filtered by user role)
- `/extend` - Extend VM time (ownership verified)
- `/destroy` - Destroy VM (ownership verified)
- `/api/vms` - Get VM list from database (filtered by user role)
- `/api/vms/<vmid>` - Get VM details (ownership verified)

## Database Schema Changes

### Updated `vm_records` Table
```sql
ALTER TABLE vm_records
ADD COLUMN created_by_user_id INT DEFAULT NULL,
ADD INDEX idx_created_by_user_id (created_by_user_id),
ADD FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL;
```

This associates each VM with the user who created it.

## Setup Instructions

### 1. Run Database Migration
```bash
# Inside the Docker container
docker exec -it proxmox-vm-automation python migrate_db.py
```

This script:
- Checks if the `created_by_user_id` column exists
- Adds the column if missing
- Creates index and foreign key constraint
- Existing VMs will have NULL for created_by_user_id (only new VMs will be tracked)

### 2. Create Admin Accounts
Admin accounts are automatically created during database initialization.

**Single Admin** (already configured):
```env
ADMIN_LOGIN_ID=admin
ADMIN_PASSWORD=secure_password_here
```

**Multiple Admins** (for TAs):
```env
ADMIN_USERS=admin,ta1,ta2,ta3
```

When the application starts, these users are automatically promoted to admin status.

### 3. Import Student Whitelist
```bash
# Copy Canvas CSV to container
docker cp students.csv proxmox-vm-automation:/app/

# Import students
docker exec -it proxmox-vm-automation python import_students.py students.csv
```

**CSV Format** (Canvas export):
```csv
Student,ID,SIS User ID,SIS Login ID,Section
John Doe,123456,1234567890,jdoe,Section 001
Jane Smith,123457,1234567891,jsmith,Section 001
```

Only the "SIS Login ID" column is used for validation.

## API Changes

### Create VM
**Before**: Required `student_name` in form data
**After**: Uses logged-in user's `login_id` automatically

```javascript
// OLD
fetch('/create', {
    method: 'POST',
    body: formData // { student_name: 'john', template: 'Ubuntu' }
})

// NEW
fetch('/create', {
    method: 'POST',
    body: formData // { template: 'Ubuntu' }
    // student_name is taken from session['login_id']
})
```

### List VMs
**Before**: Returned all VMs from Proxmox
**After**: Returns filtered VMs based on user role

```javascript
// Non-admin user
fetch('/list') // Returns only VMs created by this user

// Admin user
fetch('/list') // Returns all VMs
```

### Extend VM
**Before**: Required `student_name` in form data
**After**: Requires `vmid` and verifies ownership

```javascript
// OLD
fetch('/extend', {
    method: 'POST',
    body: { student_name: 'john', hours: 2 }
})

// NEW
fetch('/extend', {
    method: 'POST',
    body: { vmid: 123 }
    // Extension only allowed if <60 minutes remaining
    // Non-admins can only extend their own VMs
})
```

### Destroy VM
**Before**: Only checked VM ID existence
**After**: Verifies ownership before destruction

```javascript
fetch('/destroy', {
    method: 'POST',
    body: { vm_id: 123 }
    // Non-admins can only destroy their own VMs
})
```

## Frontend Changes

### 1. Login Page (`/`)
- Shows login/register tabs if user is not authenticated
- Login form with login_id and password
- Register form with login_id validation against whitelist
- Bootstrap 5 styling with gradient background

### 2. Main Dashboard
- **Navbar Updates**:
  - Shows logged-in user's login_id
  - Shows user role badge (Admin/Student)
  - Logout button
  
- **Form Updates**:
  - Removed `student_name` field from Create VM form
  - Updated Extend VM form to use VMID with ownership checks
  - All forms now respect user permissions

### 3. User Info JavaScript
```javascript
// Automatically loads on page load
loadUserInfo(); // Fetches /auth/me

// Shows user info in navbar
document.getElementById('userName').textContent = data.user.login_id;

// Logout handler
document.getElementById('logoutBtn').addEventListener('click', ...)
```

## Security Features

### 1. **Password Security**
- Passwords hashed with `werkzeug.security.generate_password_hash`
- Uses pbkdf2:sha256 algorithm
- Verification with `check_password_hash`

### 2. **Session Management**
- SECRET_KEY required in environment variables
- Flask sessions with secure cookies
- Session timeout on logout

### 3. **Access Control**
```python
# Example from routes.py
@api.route('/destroy', methods=['POST'])
@login_required  # Must be authenticated
def destroy_vm():
    # Get current user
    user = User.query.get(session['user_id'])
    
    # Get VM record
    vm_record = VMRecord.query.filter_by(vmid=vmid).first()
    
    # Check ownership
    if not user.is_admin and vm_record.created_by_user_id != user.id:
        return jsonify({'error': 'You can only destroy your own VMs'}), 403
```

### 4. **Student Whitelist Validation**
```python
# During registration
student = StudentWhitelist.query.filter_by(login_id=login_id).first()
if not student:
    return jsonify({'error': 'Login ID not found in student whitelist'})
```

## Testing the System

### 1. **Test Student Registration**
```bash
# Register a new student
curl -X POST http://localhost:8080/auth/register \
  -F "login_id=jdoe" \
  -F "password=test123"
```

### 2. **Test Student Login**
```bash
# Login
curl -X POST http://localhost:8080/auth/login \
  -F "login_id=jdoe" \
  -F "password=test123" \
  -c cookies.txt

# Check session
curl -X GET http://localhost:8080/auth/me \
  -b cookies.txt
```

### 3. **Test Admin Access**
```bash
# Login as admin
curl -X POST http://localhost:8080/auth/login \
  -F "login_id=admin" \
  -F "password=your_admin_password" \
  -c admin_cookies.txt

# List all VMs (admin sees all)
curl -X GET http://localhost:8080/list \
  -b admin_cookies.txt
```

### 4. **Test Student Restrictions**
```bash
# Student tries to destroy another student's VM
curl -X POST http://localhost:8080/destroy \
  -F "vm_id=999" \
  -b cookies.txt
  
# Should return: 403 Forbidden
# "You can only destroy your own VMs"
```

## Troubleshooting

### Issue: "User not found" on every request
**Solution**: Check that SECRET_KEY is set in .env and container is restarted
```bash
docker compose down
docker compose up -d
```

### Issue: "Login ID not found in student whitelist"
**Solution**: Import students from Canvas CSV
```bash
docker exec -it proxmox-vm-automation python import_students.py students.csv
```

### Issue: Existing VMs show as belonging to no user
**Expected Behavior**: VMs created before authentication have `created_by_user_id=NULL`
- Admins can still manage these VMs
- Students cannot see these VMs in their list
- Consider destroying old VMs or manually assigning them

### Issue: Admin can't see all VMs
**Solution**: Verify admin status in database
```bash
docker exec -it proxmox-vm-automation-mysql-1 mysql -u proxmox_user -p
USE proxmox_automation;
SELECT id, login_id, is_admin FROM users WHERE login_id='admin';
# is_admin should be 1

# If not, update manually
UPDATE users SET is_admin=1 WHERE login_id='admin';
```

## File Changes Summary

### New Files
- `app/auth.py` - Authentication routes and decorators
- `templates/login.html` - Login/register page
- `migrate_db.py` - Database migration script
- `import_students.py` - Canvas CSV import script
- `AUTHENTICATION.md` - Authentication documentation
- `USER_AUTH_GUIDE.md` - This file

### Modified Files
- `app/models.py` - Added User, StudentWhitelist, created_by_user_id field
- `app/__init__.py` - Registered auth blueprint
- `app/routes.py` - Added @login_required to all routes, user-based filtering
- `app/config.py` - Added SECRET_KEY, ADMIN_USERS
- `init_db.py` - Added admin account creation
- `.env` - Added SECRET_KEY, ADMIN_LOGIN_ID, ADMIN_PASSWORD, ADMIN_USERS
- `templates/index.html` - Removed student_name field, added user info navbar
- `static/js/script.js` - Added loadUserInfo(), logout handler

## Migration Checklist

✅ 1. Update .env with SECRET_KEY and admin credentials
✅ 2. Rebuild containers: `docker compose down && docker compose build && docker compose up -d`
✅ 3. Run database migration: `docker exec -it proxmox-vm-automation python migrate_db.py`
✅ 4. Verify admin account: `docker exec -it proxmox-vm-automation python init_db.py`
✅ 5. Import student whitelist: `docker exec -it proxmox-vm-automation python import_students.py students.csv`
✅ 6. Test login at http://localhost:8080/
✅ 7. Test student registration
✅ 8. Test VM creation (should use login_id automatically)
✅ 9. Test that students only see their own VMs
✅ 10. Test that admins see all VMs

## Environment Variables Reference

```env
# Database
MYSQL_ROOT_PASSWORD=root_password
MYSQL_DATABASE=proxmox_automation
MYSQL_USER=proxmox_user
MYSQL_PASSWORD=proxmox_password
MYSQL_HOST=mysql

# Proxmox
PROXMOX_HOST=<proxmox-host>
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=<choose-a-password>

# Application
SECRET_KEY=your-secret-key-here-use-secrets.token-hex
DEFAULT_VM_UPTIME_MINUTES=60
EXTEND_TIME_MINUTES=60

# Authentication
ADMIN_LOGIN_ID=admin
ADMIN_PASSWORD=secure_admin_password
ADMIN_USERS=admin,ta1,ta2,ta3  # Comma-separated list of admin login IDs
```

## Next Steps

1. **Customize Login Page**: Update branding in `templates/login.html`
2. **Add Password Reset**: Implement password reset functionality
3. **Email Notifications**: Add email alerts for VM expiration
4. **Usage Analytics**: Track VM creation per student
5. **Quota System**: Limit number of VMs per student
6. **VM Templates by Course**: Different templates for different sections

## Support

For issues or questions, see:
- `AUTHENTICATION.md` - Authentication system details
- `DEVELOPER_GUIDE.md` - Development information
- `README.md` - General application documentation
