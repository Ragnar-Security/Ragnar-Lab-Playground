# Authentication System Implementation Summary

## Changes Completed

### 1. Database Schema Updates

#### New Models Added (`app/models.py`)
- **User Model**: For user authentication
  - `id` (Primary Key)
  - `login_id` (Unique, indexed)
  - `password_hash`
  - `is_admin` (Boolean)
  - `created_at` (Timestamp)

- **StudentWhitelist Model**: For Canvas login ID validation
  - `id` (Primary Key)
  - `login_id` (Unique, indexed)
  - `added_at` (Timestamp)

#### Updated Models
- **VMRecord Model**: Added user ownership tracking
  - `created_by_user_id` (Foreign Key to User.id)
  - Indexed for performance
  - Cascade: SET NULL on user deletion

### 2. New Backend Files

#### `app/auth.py` - Authentication Module
- **Routes**:
  - `POST /auth/register` - User registration with whitelist validation
  - `POST /auth/login` - User login with session creation
  - `POST /auth/logout` - User logout
  - `GET /auth/me` - Get current user info
  - `GET /auth/check` - Check authentication status

- **Decorators**:
  - `@login_required` - Protect routes requiring authentication
  - `@admin_required` - Protect routes requiring admin access

- **Security**:
  - Password hashing with Werkzeug (pbkdf2:sha256)
  - Session-based authentication
  - CSRF protection ready

#### `migrate_db.py` - Database Migration Script
- Adds `created_by_user_id` column to existing `vm_records` table
- Creates index on the new column
- Adds foreign key constraint
- Idempotent (safe to run multiple times)

#### `import_students.py` - Student Whitelist Import
- Imports Canvas CSV exports
- Extracts "SIS Login ID" column
- Bulk inserts into `student_whitelist` table
- Skips duplicates

### 3. Updated Backend Files

#### `app/__init__.py`
- Registered `auth` blueprint at `/auth` prefix
- Added `SECRET_KEY` configuration from environment

#### `app/routes.py`
**Protected Routes** (added `@login_required`):
- `/create` - Create VM
- `/list` - List VMs
- `/extend` - Extend VM time
- `/destroy` - Destroy VM
- `/api/vms` - Get VM list
- `/api/vms/<vmid>` - Get VM details

**Route Modifications**:

1. **Create VM** (`POST /create`):
   - Removed `student_name` parameter from form
   - Uses `session['login_id']` automatically
   - Associates VM with `session['user_id']` via `created_by_user_id`
   - Updated `_log_vm_to_database()` signature to accept `created_by_user_id`

2. **List VMs** (`GET /list`):
   - Added user role-based filtering
   - Admin users: See all VMs from database
   - Non-admin users: See only their own VMs
   - Returns enhanced VM data (student_name, template, IP, expiration)

3. **Extend VM** (`POST /extend`):
   - Changed from `student_name` to `vmid` parameter
   - Added ownership verification
   - Admin users: Can extend any VM
   - Non-admin users: Can only extend their own VMs
   - Maintains 60-minute restriction

4. **Destroy VM** (`POST /destroy`):
   - Added ownership verification
   - Admin users: Can destroy any VM
   - Non-admin users: Can only destroy their own VMs

5. **VM List API** (`GET /api/vms`):
   - Added user role-based filtering
   - Same ownership rules as `/list`

6. **VM Detail API** (`GET /api/vms/<vmid>`):
   - Added ownership verification
   - Returns 403 Forbidden if non-admin tries to view others' VMs

7. **Index Route** (`GET /`):
   - Redirects to login page if not authenticated
   - Serves main dashboard if authenticated

#### `app/config.py`
- Added `SECRET_KEY` from environment (required for sessions)
- Added `ADMIN_USERS` configuration

#### `init_db.py`
- Added `create_admin_account()` function
- Creates admin account from `ADMIN_LOGIN_ID` and `ADMIN_PASSWORD`
- Promotes users in `ADMIN_USERS` list to admin status
- Called automatically during database initialization

### 4. Frontend Changes

#### New File: `templates/login.html`
- Bootstrap 5 styled login/register page
- Gradient background design
- Tabbed interface for login/register
- Form validation and error handling
- AJAX form submissions with feedback
- Password confirmation on registration
- Auto-redirect to dashboard on successful login

#### Updated: `templates/index.html`

**Navbar Changes**:
```html
<!-- Added user info display -->
<span id="userName">Loading...</span>
<span id="userRole" class="badge"></span>
<button id="logoutBtn">Logout</button>
```

**Form Changes**:
1. **Create VM Form**:
   - Removed `student_name` input field
   - Student name now derived from session automatically

2. **Extend VM Form**:
   - Changed input from `student_name` to `vmid`
   - Added helper text about 60-minute restriction
   - Removed `hours` input (extension time is now fixed)

#### Updated: `static/js/script.js`

**New Functions**:
```javascript
// Load current user info
loadUserInfo()

// Logout handler
document.getElementById('logoutBtn').addEventListener('click', ...)
```

**Behavior**:
- Fetches user info on page load
- Displays user's login_id and role badge
- Redirects to login if not authenticated
- Handles logout button click

### 5. Configuration Files

#### `.env` Additions
```env
# Authentication
SECRET_KEY=<generated-hex-token>
ADMIN_LOGIN_ID=admin
ADMIN_PASSWORD=<secure-password>
ADMIN_USERS=admin,ta1,ta2,ta3
```

### 6. Documentation Files

#### New Documentation
1. **AUTHENTICATION.md** - Authentication system overview
2. **USER_AUTH_GUIDE.md** - Complete implementation guide
3. **deploy_auth.sh** - Automated deployment script

### 7. Helper Scripts

#### `deploy_auth.sh`
- Automated deployment script
- Checks and adds missing environment variables
- Generates SECRET_KEY if missing
- Rebuilds and restarts containers
- Runs database migration
- Imports student whitelist (if students.csv exists)
- Provides deployment summary

## Security Enhancements

### 1. Password Security
- Passwords hashed using Werkzeug's `generate_password_hash()`
- Algorithm: pbkdf2:sha256
- Verification: `check_password_hash()`
- Never stored in plaintext

### 2. Session Security
- Flask sessions with SECRET_KEY
- Secure cookie attributes (can be enhanced with HTTPS)
- Session timeout on logout
- User ID stored in session, not password

### 3. Access Control
- Route-level protection with `@login_required`
- Resource-level ownership verification
- Role-based filtering (admin vs student)
- 403 Forbidden on unauthorized access

### 4. Student Validation
- Registration requires valid Canvas login ID
- Whitelist checked during registration
- Prevents unauthorized account creation

## API Contract Changes

### Breaking Changes

#### 1. Create VM Endpoint
```javascript
// OLD
POST /create
Body: { student_name: "john", template: "Ubuntu" }

// NEW
POST /create
Body: { template: "Ubuntu" }
// student_name automatically set from session
```

#### 2. Extend VM Endpoint
```javascript
// OLD
POST /extend
Body: { student_name: "john", hours: 2 }

// NEW
POST /extend
Body: { vmid: 123 }
// Fixed extension time from EXTEND_TIME_MINUTES
// Must be under 60 minutes remaining
```

### New Authentication Endpoints

```javascript
// Register
POST /auth/register
Body: { login_id: "jdoe", password: "password123" }

// Login
POST /auth/login
Body: { login_id: "jdoe", password: "password123" }

// Logout
POST /auth/logout

// Get current user
GET /auth/me
Response: { success: true, user: { login_id, is_admin } }

// Check authentication
GET /auth/check
Response: { authenticated: true }
```

## Migration Steps

### For Fresh Installation
1. Run `./deploy_auth.sh`
2. Import Canvas CSV with student login IDs
3. Access http://localhost:8080
4. Login with admin credentials

### For Existing Installation
1. Backup database
2. Update .env with authentication variables
3. Run `docker compose down`
4. Run `docker compose build`
5. Run `docker compose up -d`
6. Run `docker exec proxmox-vm-automation python migrate_db.py`
7. Run `docker exec proxmox-vm-automation python init_db.py`
8. Import student whitelist

## Testing Checklist

- [ ] Admin can login
- [ ] Student can register with valid login ID
- [ ] Student cannot register with invalid login ID
- [ ] Student can login after registration
- [ ] Student can create VM (using their login_id automatically)
- [ ] Student can see only their own VMs in list
- [ ] Student cannot extend/destroy other students' VMs
- [ ] Admin can see all VMs in list
- [ ] Admin can extend/destroy any VM
- [ ] Logout redirects to login page
- [ ] Unauthenticated requests redirect to login
- [ ] User info shows correctly in navbar
- [ ] Existing VMs (created_by_user_id=NULL) are accessible by admin only

## Future Enhancements

### Suggested Improvements
1. **Password Reset**: Email-based password reset flow
2. **Email Notifications**: VM expiration alerts
3. **Usage Analytics**: Track VMs created per student
4. **Quota System**: Limit VMs per student
5. **Course Sections**: Associate users with specific sections
6. **Template Permissions**: Restrict templates by user/section
7. **Audit Logging**: Track all VM operations
8. **Session Timeout**: Automatic logout after inactivity
9. **Two-Factor Authentication**: Enhanced security for admins
10. **API Rate Limiting**: Prevent abuse

## Known Limitations

1. **Existing VMs**: VMs created before authentication have `created_by_user_id=NULL`
   - Only admins can manage these VMs
   - Consider destroying old VMs or manually assigning ownership

2. **No Password Recovery**: Users cannot reset forgotten passwords
   - Admins must manually reset in database
   - Consider implementing email-based reset

3. **Single Role System**: Users are either admin or student
   - No middle-tier roles (e.g., TA with limited admin access)
   - ADMIN_USERS provides multi-admin support but same privileges

4. **No Email Verification**: Registration doesn't verify email
   - Relies on Canvas whitelist for validation
   - Consider adding email verification

## Rollback Plan

If issues arise, rollback steps:

1. **Revert to previous version**:
   ```bash
   git checkout <previous-commit>
   docker compose down
   docker compose build
   docker compose up -d
   ```

2. **Remove authentication** (emergency):
   - Remove `@login_required` decorators from routes.py
   - Comment out auth blueprint registration in __init__.py
   - Restart containers

3. **Database rollback**:
   ```sql
   ALTER TABLE vm_records DROP FOREIGN KEY <fk_name>;
   ALTER TABLE vm_records DROP COLUMN created_by_user_id;
   DROP TABLE users;
   DROP TABLE student_whitelist;
   ```

## Support Resources

- **AUTHENTICATION.md**: Authentication system details
- **USER_AUTH_GUIDE.md**: Complete setup and testing guide
- **DEVELOPER_GUIDE.md**: Development guidelines
- **README.md**: General application documentation
