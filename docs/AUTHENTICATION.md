# Authentication System

This application includes a secure user authentication system that allows students to register and login using their Canvas login IDs.

## Overview

- **Student Registration**: Students must be whitelisted (from Canvas CSV) before they can register
- **Password Security**: Passwords are hashed using werkzeug.security (PBKDF2)
- **Session Management**: Secure session cookies signed with SECRET_KEY
- **Admin Accounts**: Support for multiple administrator accounts (instructor and TAs)
- **Session Duration**: 24 hours

## Environment Variables

Add these to your `.env` file:

```bash
# Session security key (required)
SECRET_KEY=your-random-secret-key-here

# Bootstrap admin account
ADMIN_LOGIN_ID=admin
ADMIN_PASSWORD=<choose-a-password>

# Additional admin users (comma-separated Canvas login IDs)
ADMIN_USERS=ta1loginid,ta2loginid
```

### Generating a Secure SECRET_KEY

For production, generate a secure random key:

```python
import secrets
print(secrets.token_hex(32))
```

## Setup Instructions

### 1. Import Student Whitelist

First, export your Canvas gradebook as CSV and import the student login IDs:

```bash
# Copy your Canvas CSV to the automation directory
# Then run the import script
docker exec -it proxmox-vm-automation python import_students.py /app/gradebook.csv
```

The script will:
- Parse the Canvas CSV file
- Extract student login IDs (SIS Login ID column)
- Add them to the `student_whitelist` table
- Skip duplicate entries

Example output:
```
Importing students from ./gradebook.csv...
  Added jdoe1: Doe, Jane
  Added asmith2: Smith, Alex
  ...
✓ Import complete!
  Imported: 89
  Skipped: 0
  Total in whitelist: 89
```

### 2. Configure Admin Accounts

#### Bootstrap Admin Account

The first run creates an admin from `ADMIN_LOGIN_ID` and `ADMIN_PASSWORD`.
Development and test environments retain convenient defaults. Production
refuses to seed an admin when the password is missing or is a known weak
default, so set a unique value before startup.

#### Additional Admin Accounts (TAs)

To grant admin privileges to your TAs, add their Canvas login IDs to the `ADMIN_USERS` variable in `.env`:

```bash
ADMIN_USERS=jsmith,mjones,taname
```

**How it works:**
1. TAs register normally with their Canvas login ID
2. System automatically grants admin privileges if their login ID is in `ADMIN_USERS`
3. Existing users can be promoted to admin by adding them to `ADMIN_USERS` and restarting the container

### 3. Database Initialization

The authentication tables are created automatically when you start the application:

```bash
docker compose up -d
```

Tables created:
- `users` - User accounts with passwords and admin flags
- `student_whitelist` - Whitelisted Canvas login IDs

## API Endpoints

### Register New Account

**POST** `/auth/register`

Request body:
```json
{
  "login_id": "jdoe1",
  "password": "mypassword123",
  "email": "jdoe1@example.edu"
}
```

Response:
```json
{
  "success": true,
  "message": "Account created successfully",
  "user": {
    "id": 1,
    "login_id": "jdoe1",
    "full_name": "Jane Doe",
    "is_admin": false,
    "created_at": "2026-01-27T10:00:00"
  }
}
```

**Validation:**
- Login ID must exist in student whitelist
- Password must be at least 8 characters
- Login ID must be unique (no duplicate accounts)

### Login

**POST** `/auth/login`

Request body:
```json
{
  "login_id": "jdoe1",
  "password": "mypassword123"
}
```

Response:
```json
{
  "success": true,
  "message": "Login successful",
  "user": {
    "id": 1,
    "login_id": "jdoe1",
    "full_name": "Jane Doe",
    "is_admin": false,
    "last_login": "2026-01-27T21:30:00"
  }
}
```

### Logout

**POST** `/auth/logout`

Response:
```json
{
  "success": true,
  "message": "Logged out successfully"
}
```

### Check Authentication Status

**GET** `/auth/check`

Response (authenticated):
```json
{
  "authenticated": true,
  "user": {
    "id": 1,
    "login_id": "jdoe1",
    "full_name": "Jane Doe",
    "is_admin": false
  }
}
```

Response (not authenticated):
```json
{
  "authenticated": false
}
```

### Get Current User

**GET** `/auth/me`

Requires authentication.

Response:
```json
{
  "success": true,
  "user": {
    "id": 1,
    "login_id": "jdoe1",
    "full_name": "Jane Doe",
    "email": "jdoe1@example.edu",
    "is_admin": false,
    "created_at": "2026-01-27T10:00:00",
    "last_login": "2026-01-27T21:30:00"
  }
}
```

## Protected Routes

### Using the @login_required Decorator

Protect routes that require authentication:

```python
from app.auth import login_required

@api.route('/create', methods=['POST'])
@login_required
def create_vm():
    # User must be logged in to access this
    return jsonify({'success': True})
```

### Using the @admin_required Decorator

Protect routes that require admin privileges:

```python
from app.auth import admin_required

@api.route('/admin/users', methods=['GET'])
@admin_required
def list_users():
    # Only admins can access this
    return jsonify({'users': [...]})
```

**Response Codes:**
- `401 Unauthorized` - User not logged in
- `403 Forbidden` - User logged in but not an admin

## Session Management

Sessions are stored server-side and linked via signed cookies:
- **Duration**: 24 hours (configurable via `PERMANENT_SESSION_LIFETIME`)
- **Security**: Cookies are signed with `SECRET_KEY`
- **Storage**: Server-side in filesystem (`SESSION_TYPE='filesystem'`)

Session data includes:
```python
session['user_id'] = user.id
session['login_id'] = user.login_id
session['full_name'] = user.full_name
session['is_admin'] = user.is_admin
```

## Database Schema

### users Table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| login_id | VARCHAR(100) | Canvas login ID (unique) |
| password_hash | VARCHAR(255) | Hashed password |
| email | VARCHAR(255) | Optional email |
| full_name | VARCHAR(255) | Full name from Canvas |
| is_admin | BOOLEAN | Admin privileges flag |
| created_at | DATETIME | Account creation timestamp |
| last_login | DATETIME | Last successful login |

### student_whitelist Table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| login_id | VARCHAR(100) | Canvas login ID (unique) |
| full_name | VARCHAR(255) | Student's full name |
| student_id | VARCHAR(100) | Canvas student ID |
| section | VARCHAR(100) | Course section |
| added_at | DATETIME | Import timestamp |

## Security Best Practices

### Production Deployment

1. **Change Default Credentials**
   ```bash
   ADMIN_PASSWORD=use-a-strong-random-password
   ```

2. **Generate Secure SECRET_KEY**
   ```bash
   SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
   ```

3. **Use HTTPS**
   - Deploy behind a reverse proxy (nginx/traefik)
   - Enable SSL/TLS certificates

4. **Password Requirements**
   - Minimum 8 characters (enforced)
   - Consider adding complexity requirements

5. **Regular Updates**
   - Keep dependencies up to date
   - Monitor security advisories

### Docker Secrets

For Docker Swarm deployments, use secrets instead of environment variables:

```yaml
services:
  vm-automation:
    secrets:
      - secret_key
      - admin_password
```

## Troubleshooting

### "Login ID not found" Error

**Cause**: Student's Canvas login ID hasn't been imported to the whitelist.

**Solution**: 
1. Export latest Canvas gradebook as CSV
2. Run import script: `docker exec -it proxmox-vm-automation python import_students.py /path/to/csv`

### Admin Account Not Working

**Cause**: Admin account not created or `ADMIN_USERS` not configured.

**Solution**:
1. Check `.env` has `ADMIN_LOGIN_ID` and `ADMIN_PASSWORD`
2. Verify `ADMIN_USERS` contains the correct login IDs
3. Restart containers: `docker compose restart`
4. Check logs: `docker logs proxmox-vm-automation`

### Session Expires Too Quickly

**Cause**: Default 24-hour session timeout.

**Solution**: Increase `PERMANENT_SESSION_LIFETIME` in `app/__init__.py`:
```python
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # seconds (24 hours)
```

### "Authentication required" on Valid Session

**Cause**: Session cookies not persisted or SECRET_KEY changed.

**Solution**:
1. Ensure browser accepts cookies
2. Don't change `SECRET_KEY` after users have logged in
3. Check session storage directory has write permissions

## Manual User Management

### Promote User to Admin

```python
docker exec -it proxmox-vm-automation python
>>> from app import create_app, db
>>> from app.models import User
>>> app = create_app()
>>> with app.app_context():
...     user = User.query.filter_by(login_id='studentid').first()
...     user.is_admin = True
...     db.session.commit()
```

### Reset User Password

```python
docker exec -it proxmox-vm-automation python
>>> from app import create_app, db
>>> from app.models import User
>>> app = create_app()
>>> with app.app_context():
...     user = User.query.filter_by(login_id='studentid').first()
...     user.set_password('newpassword')
...     db.session.commit()
```

### List All Users

```python
docker exec -it proxmox-vm-automation python
>>> from app import create_app, db
>>> from app.models import User
>>> app = create_app()
>>> with app.app_context():
...     users = User.query.all()
...     for u in users:
...         print(f"{u.login_id}: {u.full_name} (Admin: {u.is_admin})")
```

## Next Steps

After setting up authentication:

1. **Update Frontend**: Add login/register pages
2. **Protect Routes**: Add `@login_required` to VM creation endpoints
3. **User Dashboard**: Show user's own VMs
4. **Admin Panel**: List users, manage whitelist
5. **Audit Logging**: Track VM creation by user

## Support

For issues or questions about authentication:
1. Check application logs: `docker logs proxmox-vm-automation`
2. Verify environment variables in `.env`
3. Test API endpoints with curl or Postman
4. Review `AUTHENTICATION.md` (this file)
