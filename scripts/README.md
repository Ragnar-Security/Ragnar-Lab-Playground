# Scripts

Utility scripts for database management, deployment, and system administration.

## Database Scripts

- **init_db.py** - Initialize the database schema
  ```bash
  python scripts/init_db.py
  ```

- **migrate_db.py** - Run database migrations
  ```bash
  python scripts/migrate_db.py
  ```

- **cleanup_db.py** - Clean up old database entries
  ```bash
  python scripts/cleanup_db.py
  ```

- **import_students.py** - Import students from CSV file
  ```bash
  python scripts/import_students.py data/students.csv
  ```

## Shell Scripts

- **backup-database.sh** - Backup the MySQL database
  ```bash
  bash scripts/backup-database.sh
  ```

- **deploy_auth.sh** - Deploy authentication system updates
  ```bash
  bash scripts/deploy_auth.sh
  ```

- **fix_database.sh** - Fix common database issues
  ```bash
  bash scripts/fix_database.sh
  ```

## Usage

All scripts should be run from the project root directory:

```bash
cd /path/to/automation
python scripts/<script_name>.py
# or
bash scripts/<script_name>.sh
```
