# Proxmox VM Automation - Docker Deployment

## Quick Start with Docker

### Prerequisites
- Docker and Docker Compose installed
- `.env` file configured with Proxmox credentials
- `templates.yaml` configured with your templates

### Running with Docker

```bash
# Start the application
docker-compose up -d

# View logs
docker-compose logs -f vm-automation

# Stop the application
docker-compose down
```

The application will be available at `http://localhost:5000`

### Database

The compose stack runs MySQL 8. Its data persists in the `db_data` Docker
volume; `docker compose down -v` deletes it.

### File Structure

```
├── app.py                 # Main Flask application
├── Dockerfile            # Docker image definition
├── docker-compose.yml    # Docker Compose configuration
├── requirements.txt      # Python dependencies
├── templates.yaml        # VM template configuration
├── .env                  # Environment variables (not in repo)
├── data/                 # Bind-mounted into the app; holds the untracked roster CSV
├── templates/            # HTML templates
│   └── index.html
├── static/               # Static files
│   ├── css/style.css
│   └── js/script.js
└── README.md
```

## Database API

### List all VMs
```
GET /api/vms
GET /api/vms?active=true  (only active/not destroyed)
GET /api/vms?active=false (all VMs including destroyed)
```

Response:
```json
{
  "success": true,
  "count": 5,
  "vms": [
    {
      "id": 1,
      "vmid": 100,
      "vm_name": "student-alice",
      "vm_type": "qemu",
      "node": "node1",
      "template_name": "Ubuntu24.04WordpressTemplate",
      "student_name": "alice",
      "memory": 2048,
      "cores": 1,
      "storage": 32,
      "ip_address": "192.0.2.100",
      "created_at": "2026-01-27T10:30:45.123456",
      "expires_at": "2026-01-27T22:30:45.123456",
      "destroyed": false,
      "destroyed_at": null
    }
  ]
}
```

### Get specific VM details
```
GET /api/vms/<vmid>
```

## Database Schema

The `vm_records` table tracks all created VMs:

| Column | Type | Description |
|--------|------|-------------|
| id | Integer | Primary key |
| vmid | Integer | Proxmox VM ID (unique) |
| vm_name | String | VM hostname |
| vm_type | String | 'lxc' or 'qemu' |
| node | String | Proxmox node name |
| template_name | String | Template display name |
| student_name | String | Student identifier |
| memory | Integer | RAM in MB |
| cores | Integer | CPU cores |
| storage | Integer | Storage in GB |
| ip_address | String | Assigned IP address |
| created_at | DateTime | VM creation time |
| expires_at | DateTime | Scheduled expiration |
| destroyed | Boolean | Whether VM has been destroyed |
| destroyed_at | DateTime | Destruction timestamp |

## Environment Variables

Create `.env` file with:
```
PROXMOX_HOST=<proxmox-host>
PROXMOX_USER=root@pam
PROXMOX_TOKEN_ID=your-token-id
PROXMOX_TOKEN_SECRET=your-token-secret
VM_UPTIME_MINUTES=4320
```

## Volume Mounts

- `/app/data` - Database persistence
- `/app/.env` - Environment configuration (read-only)
- `/app/templates.yaml` - Template configuration (read-only)
