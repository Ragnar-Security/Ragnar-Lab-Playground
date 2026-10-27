# Student Guide: Lab Playground

This guide explains how to access and manage virtual machines in a Lab Playground deployment.

## Table of Contents
1. [Getting Started with Tailscale](#getting-started-with-tailscale)
2. [Accessing the Jumpbox via SSH](#accessing-the-jumpbox-via-ssh)
3. [Using the Web Portal](#using-the-web-portal)
4. [Creating and Managing VMs](#creating-and-managing-vms)
5. [Troubleshooting](#troubleshooting)

---

## Getting Started with Tailscale

Tailscale provides secure VPN access to the lab environment.

### Step 1: Install Tailscale

**Windows:**
1. Download from: https://tailscale.com/download/windows
2. Run the installer
3. Follow the installation prompts

**macOS:**
1. Download from: https://tailscale.com/download/mac
2. Open the DMG and drag to Applications
3. Launch Tailscale from Applications

**Linux:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

### Step 2: Connect to the Lab Network

Follow the VPN command shown by your portal or provided by your instructor.
For a private Headscale deployment it may look like:

```bash
tailscale up --login-server=https://vpn.example.edu
```

The hostname is deployment-specific. When the portal does not show a
`--login-server` flag, use Tailscale's normal sign-in flow instead. Open the
registration URL from your instructor and verify that Tailscale reports
`Connected`.

### Step 3: Verify Connection

Once connected, locate the lab jumpbox in your Tailscale network and note
the hostname or VPN address provided by your instructor.

---

## Accessing the Jumpbox via SSH

The jumpbox is your gateway to lab resources.

### Your Credentials

- **Username:** The login ID provided by your instructor
- **Initial password:** `<provided-by-instructor>`
- **You may be forced to change this password on first login**

### Step 1: Connect via SSH

**Windows (using PowerShell or Windows Terminal):**
```powershell
ssh <your-username>@jumpbox-name
# Or use the Tailscale IP directly:
ssh <your-username>@100.x.x.x
```

**macOS/Linux (using Terminal):**
```bash
ssh <your-username>@jumpbox-name
# Or use the Tailscale IP directly:
ssh <your-username>@100.x.x.x
```


### Step 2: Change Your Password

On first login, you may be prompted:
```
You are required to change your password immediately (administrator enforced)
Current password: <provided-by-instructor>
New password: [enter your new password]
Retype new password: [enter again]
```

**Password Requirements:**
- At least 8 characters
- Mix of letters, numbers, and symbols recommended
- **Remember this password!**

### Step 3: Verify Access

After logging in, you should see a command prompt:
```
<your-username>@jumpbox:~$
```

---

## Using the Web Portal

The web portal allows you to create and manage VMs through a browser interface.

### Step 1: Access the Portal

1. Make sure Tailscale is connected
2. Open your web browser
3. Navigate to: `http://<portal-ip-address>` (provided by instructor)
   - Or use the portal's Tailscale hostname if available

### Step 2: Create an Account

**First-time users:**
1. Click "Register" on the login page
2. Fill in the registration form:
   - **Username:** Your course login ID (must match SSH username)
   - **Email:** Your university email
   - **Password:** Choose a secure password (can be different from SSH)
3. Click "Register"
4. Wait for instructor approval (you'll receive confirmation)

### Step 3: Log In

1. Enter your username
2. Enter your password
3. Click "Login"

You should now see the VM management dashboard.

---

## Creating and Managing VMs

All VM operations happen in the **My Labs** tab, which combines creation, monitoring, extending, and destruction into a single view.

### Creating a New Lab

1. **Open the My Labs tab** on the dashboard.
2. **Select a deployment group** from the dropdown (e.g., "Homework 2 - Active Recon"). You will only see labs your instructor has made available to you.
3. **Click "Create"** to deploy all VMs in that group at once. The portal provisions them in parallel, typically taking 2-5 minutes. A progress indicator will appear.
4. Once complete, your new deployment group appears in the My Labs list with its entry-point IP address.

### Viewing Your Labs

The **My Labs** tab shows all your active deployment groups:
- **Group name** and description
- **Entry-point IP** (the address you use to connect)
- **Time remaining** before automatic expiration
- **Extend** and **Destroy** buttons for each group

Only the entry-point VM's IP is shown. Internal VMs within a deployment group communicate with each other automatically.

### Extending Lab Lifetime

If your lab has less than 60 minutes remaining:

1. Find the deployment group in **My Labs**.
2. Click **Extend**.
3. The expiration is pushed forward by the time configured for that lab (shown in the group details).

### Connecting to Your Lab

**Via SSH from the Jumpbox:**
```bash
# SSH to jumpbox first
ssh <your-username>@jumpbox-name

# Then SSH to your entry-point VM using the IP shown in My Labs
ssh <vm-username>@<entry-point-ip>
```

**Default VM Credentials:**
- Check the template/lab description for default usernames and passwords.
- Common defaults:
  - Ubuntu: `ubuntu` / password provided in template details
  - Kali: `kali` / password provided in template details
  - Windows: credentials provided in template details

**Example:**
```bash
# From jumpbox
ssh ubuntu@10.0.X.X
```

### Connecting with Remote Desktop

When a deployment group provides a Windows VM, use the `.rdp` link in the
Connect column and the credentials in that lab's description. Save work
outside the VM before it expires; destroying or expiring the lab deletes its
local data.

### Destroying a Lab

**Warning:** This action is permanent and cannot be undone!

1. Make sure you have saved any important data.
2. Find the deployment group in **My Labs**.
3. Click **Destroy**.
4. Confirm the deletion.
5. All VMs in the group and their data will be permanently removed.

---

## Important Notes

### Lab Expiration
- All labs have an expiration countdown shown in the **My Labs** tab
- Labs are **automatically deleted** after expiration
- Check time remaining regularly and use **Extend** if needed
- **Save your work frequently** and back up important data

### Resource Limits
- You can only deploy labs your instructor has assigned to you
- Delete unused labs to free up resources for yourself and others

### Network Access
- VMs are isolated on your personal VLAN
- Only you can access your VMs (plus instructor for support)
- VMs in the same deployment group can communicate with each other
- Entry point VMs may have external access (depending on template)

### Best Practices
1. **Monitor expiration times:** Check the My Labs tab regularly so you don't lose work to automatic deletion
2. **Extend before it is too late:** You can only extend when less than 60 minutes remain
3. **Keep SSH keys secure:** Never share your private keys
4. **Change default passwords:** Always change VM default passwords after first login
5. **Back up important work:** Use git, scp, or other backup methods before your lab expires

---

## Troubleshooting

### Can't Connect to Tailscale

**Problem:** Tailscale won't connect or shows "No coordinator"

**Solution:**
1. Use the login server shown by the portal or provided by your instructor.
2. If no `--login-server` flag is shown, use Tailscale's normal sign-in flow.
3. Check that your registration URL is valid and contact the instructor for a replacement if it expired.

### Can't SSH to Jumpbox

**Problem:** "Connection refused" or "Permission denied"

**Solution:**
1. Verify Tailscale is connected (green checkmark)
2. Check that you are using the correct username.
3. Use the initial password provided by the instructor on first login.
4. Try using the provided VPN address instead of the hostname.
5. Check for typos in username

### Password Changed But Can't Login

**Problem:** You changed your password but it's not working

**Solution:**
1. Make sure Caps Lock is off
2. Try typing password in a text editor first to verify it
3. Contact instructor to reset password

### Can't Access Web Portal

**Problem:** Browser can't reach the portal

**Solution:**
1. Verify Tailscale is connected
2. Make sure you're using HTTP (not HTTPS) unless instructed otherwise
3. Try using the IP address instead of hostname
4. Clear browser cache and cookies
5. Try a different browser

### Lab Won't Start

**Problem:** Lab creation seems stuck or never completes

**Solution:**
1. Wait 2-5 minutes (multi-VM labs take time to provision)
2. Refresh the My Labs tab
3. If the lab appears but entry-point IP shows "N/A", wait another minute for the VM to boot and acquire an IP
4. Contact instructor if the problem persists

### Can't Connect to Lab VM via SSH

**Problem:** SSH to the entry-point IP address fails

**Solution:**
1. Verify the lab is listed in the My Labs tab with a valid IP
2. Make sure you are SSH'd into the jumpbox first
3. Use the IP address shown in the My Labs tab
4. Some VMs take 1-2 minutes after creation to accept SSH connections
5. Verify you are using the correct default credentials for that lab

### Lab Accidentally Deleted

**Problem:** I deleted my lab by mistake

**Solution:**
- Unfortunately, deletions are permanent
- Recreate the lab from the My Labs tab (select the same deployment group)
- Restore your work from backups (if you have any)
- **This is why backing up your work is critical!**

### Can't See a Lab

**Problem:** A lab that should be available is not in the dropdown

**Solution:**
1. Your instructor may not have assigned that lab to you yet
2. The lab may be disabled
3. Contact your instructor to check your lab assignments

---

## Getting Help

### Self-Help Resources
1. Re-read this guide carefully
2. Check the troubleshooting section above
3. Ask a classmate if they've experienced similar issues

### Contact Instructor
If you still need help:
1. Include your username
2. Describe what you were trying to do
3. Describe what happened (error messages, screenshots)
4. List what you've already tried

### Office Hours
- Check course syllabus for instructor office hours
- Come prepared with specific questions
- Bring screenshots of errors if applicable

---

## Quick Reference

### Tailscale Login Server

Use the portal's VPN instructions. A private deployment may use a URL such
as `https://vpn.example.edu`; omit `--login-server` when no custom server is
shown.

### SSH to Jumpbox
```bash
ssh <your-username>@<jumpbox-host>
```

### Initial Jumpbox Password

Use the password supplied by your instructor and change it on first login
when prompted.

### Web Portal
```
http://<portal-address>
```
(Provided by instructor)

### VM Connection (from jumpbox)
```bash
ssh <vm-user>@<vm-ip>
```

---

## Security Reminders

- **Never share your credentials** with other students
- **Never share your SSH keys**
- **Change default VM passwords** immediately
- **Log out** when you're done working
- **Don't run malicious code** or attempt unauthorized access
- **Report security issues** to instructor immediately

Violations of security policies may result in:
- Loss of lab access
- Academic penalties
- Reporting to university administration

---

**Last Updated:** April 2026

**Questions?** Contact your course instructor.

Good luck with your labs! 🚀
