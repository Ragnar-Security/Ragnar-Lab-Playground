# Testing Alert Notifications

This guide explains how to test your Discord webhook and email notification configurations using the admin interface.

## Overview

The alert testing feature allows administrators to manually send test notifications to verify that the alert system is configured correctly. This is useful for:

- Verifying SMTP email settings before production use
- Testing Discord/Slack webhook URLs
- Confirming that all alert methods are working properly
- Troubleshooting configuration issues

## Prerequisites

### Required for All Alerts
- Admin account with `is_admin=True`
- File logging and database alerts are always enabled (no configuration needed)

### Required for Email Alerts
Set these environment variables in your `.env` file:
```bash
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@example.com
SMTP_PASSWORD=your-app-password
SMTP_FROM_EMAIL=alerts@yourdomain.com
ADMIN_ALERT_EMAILS=admin1@example.com,admin2@example.com
```

### Required for Webhook Alerts (Discord)
Set these environment variables in your `.env` file:
```bash
ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN
ALERT_WEBHOOK_TYPE=discord
```

### Required for Webhook Alerts (Slack)
Set these environment variables in your `.env` file:
```bash
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
ALERT_WEBHOOK_TYPE=slack
```

## How to Test Alerts

### Step 1: Login as Admin
1. Navigate to your VM automation dashboard (for example, `https://lab.example.edu`)
2. Login with an admin account

### Step 2: Access Test Alerts Tab
1. In the left sidebar, you should see a **"Test Alerts"** tab (🔔 icon)
2. This tab is only visible to admin users
3. Click on the tab to open the test alerts interface

### Step 3: Select Alert Types to Test
The interface shows 4 alert types:

1. **Email Alerts (SMTP)** - Checkbox (checked by default)
   - Requires SMTP configuration in `.env`
   - Sends test email to all addresses in `ADMIN_ALERT_EMAILS`

2. **Webhook Alerts (Discord/Slack)** - Checkbox (checked by default)
   - Requires `ALERT_WEBHOOK_URL` in `.env`
   - Posts test message to your Discord/Slack channel

3. **File Logging** - Always enabled (grayed out)
   - Writes to `/app/data/failed_vms.log`
   - No configuration needed

4. **Database Alerts** - Always enabled (grayed out)
   - Creates `AdminAlert` record in database
   - No configuration needed

### Step 4: Send Test Alerts
1. Check/uncheck the alert types you want to test (email and/or webhook)
2. Click the **"Send Test Alerts"** button
3. Wait for the results to appear below the form

### Step 5: Review Results
The results will show the status of each alert type:

- ✅ **Success** - Alert was sent successfully
- ❌ **Failed** - Alert failed to send (error message will be shown)

Example success message:
```
Test Alert Results:

✅ File Logging: Success
✅ Database Alert: Success
✅ Email Alert: Success
✅ Webhook Alert: Success

Check your email inbox and Discord/Slack channel for test notifications.
```

Example with errors:
```
Test Alert Results:

✅ File Logging: Success
✅ Database Alert: Success
❌ Email Alert: Failed
    Error: SMTP authentication failed: username and password not accepted
✅ Webhook Alert: Success
```

## Verifying Test Alerts

### Email
- Check the inbox(es) of all email addresses in `ADMIN_ALERT_EMAILS`
- Subject: `VM Deletion Alert - Max Retries Exceeded`
- Body will contain mock VM details:
  - VM ID: 999999
  - VM Name: `test-alert-vm-<timestamp>`
  - Error: "TEST ALERT"
  - Retry attempts: 5

### Discord
- Check your Discord channel where the webhook is configured
- You should see an embedded message with:
  - Title: "⚠️ VM Deletion Alert"
  - Color: Orange/Yellow (warning color)
  - Fields: VM ID, VM Name, Error, Retry Attempts
  - Footer: "Max retries (5) exceeded"

### Slack
- Check your Slack channel where the webhook is configured
- You should see a message with:
  - Title: "VM Deletion Alert"
  - Color: "warning"
  - Attachment with VM details

### File Log
- SSH into your server or check Docker logs
- View the file: `/app/data/failed_vms.log`
- Look for the most recent entry with timestamp and mock VM details

### Database
- Query the `admin_alert` table:
  ```sql
  SELECT * FROM admin_alert ORDER BY created_at DESC LIMIT 1;
  ```
- The most recent record should have:
  - `alert_type`: "vm_deletion_failed"
  - `severity`: "critical"
  - `vmid`: 999999
  - `vm_name`: `test-alert-vm-<timestamp>`
  - `message`: "TEST ALERT"

## Troubleshooting

### Email Alerts Not Sending

**Error: "SMTP configuration not complete"**
- Ensure all SMTP environment variables are set in `.env`
- Required: `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `ADMIN_ALERT_EMAILS`

**Error: "SMTP authentication failed"**
- Verify your SMTP credentials are correct
- For Gmail, use an App Password instead of your regular password
- Check that your email provider allows SMTP access

**Error: "Connection refused" or "Timeout"**
- Check that `SMTP_PORT` is correct (usually 587 for TLS, 465 for SSL)
- Ensure your firewall allows outbound connections to the SMTP server
- Verify the `SMTP_SERVER` hostname is correct

### Webhook Alerts Not Sending

**Error: "Webhook URL not configured"**
- Set `ALERT_WEBHOOK_URL` in your `.env` file
- For Discord: Get webhook URL from Server Settings → Integrations → Webhooks
- For Slack: Get webhook URL from Slack App settings

**Error: "HTTP 404" or "Invalid webhook"**
- Verify the webhook URL is correct and hasn't been deleted
- Discord webhooks look like: `https://discord.com/api/webhooks/ID/TOKEN`
- Slack webhooks look like: `https://hooks.slack.com/services/...`

**Error: "HTTP 401" or "Unauthorized"**
- The webhook token may have expired or been revoked
- Create a new webhook in Discord/Slack

### File Logging Not Working

**Error: "Permission denied"**
- Ensure `/app/data/` directory exists and is writable
- Check Docker volume permissions
- Verify the app is running as the correct user

### Database Alerts Not Working

**Error: "Table 'admin_alert' doesn't exist"**
- Run database migrations: `python migrate_db.py`
- Or reinitialize database: `python init_db.py`

## API Endpoint

The test alerts feature uses the following API endpoint:

**Endpoint:** `POST /admin/test-alerts`

**Authentication:** Requires admin login

**Parameters:**
- `test_email` (optional, default: true) - Whether to test email alerts
- `test_webhook` (optional, default: true) - Whether to test webhook alerts

**Response:**
```json
{
  "success": true,
  "results": {
    "file": { "logged": true },
    "database": { "created": true },
    "email": { "sent": true },
    "webhook": { "sent": true }
  },
  "message": "Test alerts completed. Check results for details."
}
```

## Security Notes

- Only users with `is_admin=True` can access the test alerts feature
- Non-admin users will receive a 403 Forbidden error if they try to access the endpoint
- Test alerts are clearly marked with mock data (VM ID 999999)
- Test alerts create real database records and log entries (for testing purposes)

## Next Steps

After successfully testing your alerts:

1. **Monitor Real Alerts:** Watch for alerts triggered by actual VM deletion failures
2. **Review Logs:** Regularly check `/app/data/failed_vms.log` for any issues
3. **Database Maintenance:** Periodically clean up old `admin_alert` records
4. **Fine-tune Configuration:** Adjust retry limits or alert thresholds as needed

## Related Documentation

- [AUTHENTICATION.md](AUTHENTICATION.md) - User authentication and admin roles
- [REFACTORING_SUMMARY.md](REFACTORING_SUMMARY.md) - Overview of alert system architecture
- [.env.example](.env.example) - Environment variable configuration template
