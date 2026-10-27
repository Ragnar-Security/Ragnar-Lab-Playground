# Multi-VM Deployment Groups & Session Expiration Update

## Summary

This update implements two major changes to the VM automation system:

1. **Multi-VM Deployment Groups**: Students can now deploy multiple VMs for a single assignment
2. **2-Hour Session Expiration**: Login sessions now expire after 2 hours of inactivity

---

## Change 1: Multi-VM Deployment Groups

### Overview

Previously, students could only deploy one VM at a time. Now, the system supports **deployment groups** that allow students to deploy multiple VMs for a single homework/assignment (e.g., homework2, homework3).

### What Changed

#### 1. Templates Configuration ([templates.yaml](templates.yaml))

- **New Structure**: Introduced `deployment_groups` section
- **Format**:
  ```yaml
  deployment_groups:
    - display_name: "homework2"
      description: "Homework 2: Single Ubuntu WordPress VM"
      enabled: true
      templates:
        - display_name: "Ubuntu24.04WordpressTemplate"
          source_vmid: 100
          type: "qemu"
          memory: 2048
          cores: 1
          ...
  ```

- **Example Multi-VM Group**:
  ```yaml
  - display_name: "homework3"
    description: "Homework 3: Load Balancer + 2 Web Servers"
    enabled: false
    templates:
      - display_name: "LoadBalancer"
        ...
      - display_name: "WebServer1"
        ...
      - display_name: "WebServer2"
        ...
  ```

#### 2. Backend Changes

**[app/template_manager.py](app/template_manager.py)**:
- Added `get_enabled_deployment_groups()` - Returns enabled deployment groups
- Added `get_deployment_group_by_name()` - Finds specific deployment group
- Maintains backward compatibility with legacy single template format

**[app/routes.py](app/routes.py)**:
- `/templates` endpoint now returns deployment groups instead of individual templates
- `/create` endpoint:
  - Now accepts `deployment_group` parameter instead of `template`
  - Creates multiple VMs from a single deployment group
  - VM names follow format: `student-{loginid}-{group}-{template}`
  - Returns array of created VMs with details
  - Checks if any VM in the group already exists before creating

#### 3. Frontend Changes

**[templates/index.html](templates/index.html)**:
- Updated form to select deployment groups instead of templates
- Shows deployment details when a group is selected:
  - Description
  - Number of VMs
  - List of VMs with specs (memory, cores, type)
- Updated button text from "Create VM" to "Create VM(s)"

**[static/js/script.js](static/js/script.js)**:
- Renamed `loadTemplates()` to `loadDeploymentGroups()`
- Displays deployment groups with VM count: "homework2 (1 VM)"
- Shows deployment details when user selects a group
- Updated form submission to send `deployment_group` parameter
- Shows multiple VM details in success message

### How to Use

1. **Edit templates.yaml**:
   ```yaml
   deployment_groups:
     - display_name: "homework4"
       description: "Homework 4: Database + Web + Cache"
       enabled: true
       templates:
         - display_name: "MySQL"
           source_vmid: 100
           type: "qemu"
           memory: 2048
           cores: 2
           ...
         - display_name: "WebServer"
           source_vmid: 100
           ...
         - display_name: "Redis"
           source_vmid: 100
           ...
   ```

2. **Set enabled: true** for the deployment group you want students to access

3. **Students select deployment group** from dropdown (e.g., "homework4")

4. **System creates all VMs** in that group automatically

### VM Naming Convention

VMs are now named: `student-{loginid}-{group}-{template}`

Examples:
- `student-jsmith-homework2-ubuntu2404wordpresstemplate`
- `student-jsmith-homework3-loadbalancer`
- `student-jsmith-homework3-webserver1`
- `student-jsmith-homework3-webserver2`

### Database Storage

Each VM is stored in the database with:
- `template_name`: Set to `{deployment_group}/{template_display_name}`
- Example: "homework3/LoadBalancer", "homework3/WebServer1"

---

## Change 2: 2-Hour Session Expiration

### Overview

Login sessions now automatically expire after 2 hours, forcing students to re-authenticate. This improves security and encourages proper logout behavior.

### What Changed

#### 1. Session Configuration ([app/__init__.py](app/__init__.py))

- **Changed**: `PERMANENT_SESSION_LIFETIME` from 86400 seconds (24 hours) to **7200 seconds (2 hours)**

```python
app.config['PERMANENT_SESSION_LIFETIME'] = 7200  # 2 hours (in seconds)
```

#### 2. Session Activation ([app/auth.py](app/auth.py))

- **Added**: `session.permanent = True` in both `/auth/register` and `/auth/login` routes
- This enables Flask to apply the `PERMANENT_SESSION_LIFETIME` setting

**In register route**:
```python
# Auto-login after registration
session.permanent = True  # Enable session expiration (2 hours)
session['user_id'] = user.id
...
```

**In login route**:
```python
# Set session
session.permanent = True  # Enable session expiration (2 hours)
session['user_id'] = user.id
...
```

### How It Works

1. Student logs in or registers
2. Session is created with 2-hour expiration
3. After 2 hours of inactivity, session expires
4. Next request automatically redirects to login page
5. Student must re-authenticate

### Testing

To test the session expiration without waiting 2 hours, you can temporarily change the value in [app/__init__.py](app/__init__.py):

```python
# For testing: 60 seconds (1 minute)
app.config['PERMANENT_SESSION_LIFETIME'] = 60

# For testing: 300 seconds (5 minutes)
app.config['PERMANENT_SESSION_LIFETIME'] = 300

# Production: 7200 seconds (2 hours)
app.config['PERMANENT_SESSION_LIFETIME'] = 7200
```

---

## Files Modified

1. **templates.yaml** - New deployment groups structure
2. **app/template_manager.py** - Deployment group functions
3. **app/routes.py** - Multi-VM creation logic
4. **app/__init__.py** - 2-hour session lifetime
5. **app/auth.py** - Permanent session flag
6. **templates/index.html** - Deployment group UI
7. **static/js/script.js** - Deployment group selection

---

## Migration Notes

### For Existing Deployments

1. **Backup current templates.yaml** before applying changes
2. **Update templates.yaml** with new deployment group structure
3. **Restart the application** to load new configuration
4. **Test with a single-VM group first** (like homework2)
5. **Then test multi-VM groups** (like homework3)

### Backward Compatibility

The system maintains backward compatibility with the legacy `templates:` format in templates.yaml, but it's recommended to migrate to the new `deployment_groups:` format.

### Database

No database schema changes are required. The existing `VMRecord` table structure supports the new multi-VM deployment model.

---

## Example Deployment Scenarios

### Scenario 1: Single VM Deployment
```yaml
- display_name: "homework1"
  description: "Basic Ubuntu VM"
  enabled: true
  templates:
    - display_name: "UbuntuBasic"
      source_vmid: 100
      type: "qemu"
      memory: 2048
      cores: 1
      storage: 32
      storage_device: "<storage>"
      uptime_minutes: 4320
```

### Scenario 2: Multi-VM Deployment
```yaml
- display_name: "homework5"
  description: "Complete LAMP Stack"
  enabled: true
  templates:
    - display_name: "Apache"
      source_vmid: 100
      type: "qemu"
      memory: 2048
      cores: 2
      storage: 32
      storage_device: "<storage>"
      uptime_minutes: 4320
    - display_name: "MySQL"
      source_vmid: 100
      type: "qemu"
      memory: 4096
      cores: 2
      storage: 64
      storage_device: "<storage>"
      uptime_minutes: 4320
    - display_name: "PHP-App"
      source_vmid: 100
      type: "qemu"
      memory: 2048
      cores: 1
      storage: 32
      storage_device: "<storage>"
      uptime_minutes: 4320
```

---

## Testing Checklist

- [ ] Load application and verify no errors
- [ ] Check deployment groups appear in dropdown
- [ ] Select a single-VM group and verify details display
- [ ] Create single VM and verify success message
- [ ] Check VM appears in "List VMs" tab
- [ ] Select a multi-VM group and verify all templates shown
- [ ] Create multi-VM deployment and verify all VMs created
- [ ] Check all VMs appear in database and Proxmox
- [ ] Test duplicate prevention (try creating same group twice)
- [ ] Verify VM names follow correct format
- [ ] Test session expiration after 2 hours
- [ ] Verify forced re-login after session expires

---

## Support & Troubleshooting

### Issue: Deployment groups not appearing
- Check `enabled: true` in templates.yaml
- Verify YAML syntax is correct (proper indentation)
- Check browser console for errors
- Restart application

### Issue: VM creation fails
- Verify source_vmid exists in Proxmox
- Check Proxmox node has sufficient resources
- Review application logs for detailed error messages
- Ensure storage_device is correct

### Issue: Session expires too quickly
- Verify `PERMANENT_SESSION_LIFETIME = 7200` in app/__init__.py
- Check `session.permanent = True` is set in auth.py
- Restart application after changes

---

## Future Enhancements

Possible future improvements:
1. **Template dependencies** - Define VM startup order
2. **Network configuration** - Auto-configure VMs to communicate
3. **Batch operations** - Extend time for all VMs in a group
4. **Group deletion** - Delete all VMs in a deployment group at once
5. **Progress tracking** - Real-time VM creation status updates
6. **Resource quotas** - Limit VMs per student
