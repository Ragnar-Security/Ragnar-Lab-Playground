"""
Rotate students' reusable Headscale pre-auth keys. Run it through
scripts/rotate_headscale_keys.sh, which loads scripts/site.env first.

The default is a dry run: it reads the portal roster and Headscale and prints
the plan. --apply then works in an order chosen so that a failure at any step
leaves every student with a key that still works:

  1. create a new reusable key per student on Headscale;
  2. write the new data/students_merged.csv locally (mode 0600);
  3. back up the portal's roster file and replace it atomically (mode 0640,
     group APP_GID: the app container's group, see Dockerfile.prod);
  4. store the new keys in the portal database
     (scripts/update_headscale_keys.py inside the app container);
  5. only then expire the keys that were active before step 1.

--apply holds an exclusive lock on both hosts from before it reads the roster
until after step 5, so two rotations never interleave. Each lock is a
directory made with mkdir (atomic) holding an owner file with a random token;
every mutating command first checks that the token is still this run's, so a
run whose lock was broken stops instead of racing the run that broke it. A
login the portal database does not know keeps its old key and fails the run
unless --allow-partial is given.

Expiring a pre-auth key does not disconnect a node that already registered
with it; it only stops new registrations. Students who reinstall or add a
device after the rotation need the new key, which the portal now shows them.

Key values travel over SSH stdin/stdout only, never in arguments or output.

The same file also runs on the Headscale host: the orchestrator sends its own
source as `python3 -c <source> --remote <step>`, so the Headscale host needs
no copy of this repository. That side uses only the standard library and the
`docker compose exec -T headscale headscale` CLI (v0.27 flags).
"""

import argparse
import csv
import getpass
import hashlib
import io
import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

FIELDNAMES = ['SIS User ID', 'SIS Login ID', 'email', 'headscale_key']
APP_SERVICE = 'vm-automation'
UPDATE_SCRIPT = 'scripts/update_headscale_keys.py'
SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
# The app container runs as uid/gid 1000 (Dockerfile.prod pins both). The live
# roster carries reusable VPN keys, so it is delivered as mode 0640 with this
# group: the container can read it, other accounts on the portal host cannot.
APP_GID = 1000
LOCK_NAME = '.rotate.lock'
# A lock this old belongs to a run that died; --break-stale-lock may take it.
# No real rotation comes close: it takes minutes, plus the confirmation prompt.
STALE_LOCK_HOURS = 6
REQUIRED = (
    'PORTAL_HOST', 'PORTAL_DIR', 'PORTAL_COMPOSE',
    'HEADSCALE_HOST', 'HEADSCALE_DIR', 'HEADSCALE_KEEP_USER', 'HEADSCALE_STAFF',
    'KEY_DAYS', 'REMOTE_BACKUP_DIR',
)


class Abort(Exception):
    """Stop with a message; the message must already say what state remains."""


def mask(key):
    return '...' + key[-4:] if len(key) > 8 else '...'


def scrub(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, mask(secret))
    return text


# --- Headscale host side ------------------------------------------------------

def _headscale(*args):
    return subprocess.run(
        ['docker', 'compose', 'exec', '-T', 'headscale', 'headscale', *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )


def _epoch(value):
    """Expiry as Unix seconds. The CLI marshals protobuf timestamps with
    encoding/json ({"seconds": N}); accept RFC 3339 too in case a later
    release switches to protojson."""
    if value is None:
        return None
    if isinstance(value, dict):
        return int(value.get('seconds', 0))
    text = str(value).replace('Z', '+00:00')
    if '.' in text:
        head, _, tail = text.partition('.')
        digits = ''.join(c for c in tail if c.isdigit())
        text = head + '.' + digits[:6] + tail[len(digits):]
    return int(datetime.fromisoformat(text).timestamp())


def _list_keys(user_id):
    proc = _headscale('preauthkeys', 'list', '-u', str(user_id), '-o', 'json')
    if proc.returncode != 0:
        raise RuntimeError(f"listing keys for user {user_id} failed")
    return json.loads(proc.stdout or 'null') or []


def _active(key, now):
    expiry = _epoch(key.get('expiration'))
    return expiry is None or expiry > now


def remote_inventory(request):
    proc = _headscale('users', 'list', '-o', 'json')
    if proc.returncode != 0:
        raise RuntimeError('headscale users list failed: ' + proc.stderr.strip())
    ids = {user['name']: user['id'] for user in json.loads(proc.stdout or '[]')}
    now = time.time()
    users, missing = {}, []
    for login in request['logins']:
        if login not in ids:
            missing.append(login)
            continue
        active = [str(k['id']) for k in _list_keys(ids[login]) if _active(k, now)]
        users[login] = {'id': ids[login], 'active_key_ids': active}
    return {'users': users, 'missing': missing}


def remote_create(request):
    keys = {}
    for login, user_id in request['users'].items():
        proc = _headscale('preauthkeys', 'create', '--user', str(user_id), '--reusable',
                          '--expiration', request['expiration'], '-o', 'json')
        try:
            key = json.loads(proc.stdout)['key'] if proc.returncode == 0 else ''
        except (ValueError, KeyError, TypeError):
            key = ''
        if not key:
            # Stop at the first failure: every extra key would be a live
            # credential that nobody receives.
            print(f"creating a key for {login} failed: "
                  + scrub(proc.stderr.strip() or proc.stdout.strip(), keys.values()),
                  file=sys.stderr)
            return {'keys': keys, 'failed': login}
        keys[login] = key
    return {'keys': keys, 'failed': None}


def remote_expire(request):
    expired, failed = {}, {}
    now = time.time()
    for login, item in request['users'].items():
        wanted = set(item['key_ids'])
        try:
            current = _list_keys(item['id'])
        except RuntimeError as error:
            failed[login] = str(error)
            continue
        count = 0
        for key in current:
            if str(key['id']) not in wanted or not _active(key, now):
                continue
            proc = _headscale('preauthkeys', 'expire', '-u', str(item['id']), key['key'])
            if proc.returncode != 0:
                failed[login] = scrub(proc.stderr.strip() or 'expire failed', [key['key']])
                break
            count += 1
        expired[login] = count
    return {'expired': expired, 'failed': failed}


REMOTE_STEPS = {'inventory': remote_inventory, 'create': remote_create, 'expire': remote_expire}


def remote_main(step):
    try:
        result = REMOTE_STEPS[step](json.load(sys.stdin))
    except Exception as error:  # report, never a traceback that may quote a key
        print(f"{step} failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


# --- workstation side ---------------------------------------------------------

def _ssh(host, command, stdin_text=''):
    return subprocess.run(['ssh', *SSH_OPTIONS, host, command],
                          input=stdin_text, capture_output=True, text=True)


def _relay(stderr, secrets=()):
    stderr = scrub(stderr.strip(), secrets)
    if stderr:
        print('  ' + stderr.replace('\n', '\n  '), file=sys.stderr)


def _operator():
    try:
        user = getpass.getuser()
    except Exception:  # no login name in this environment
        user = 'unknown'
    return (f"{user}@{socket.gethostname()} pid={os.getpid()} "
            f"started={time.strftime('%Y-%m-%d %H:%M:%S %z')}")


class HostLock:
    """An exclusive rotation lock on one host, held across many SSH calls.

    The lock is a directory: mkdir either creates it or fails, atomically. Its
    `owner` file names the run (operator, workstation, pid, start time) and a
    random token. Staleness is the directory's age on the host itself, so a
    workstation's clock cannot make a live lock look stale.
    """

    def __init__(self, host, directory, token, label):
        self.host, self.label, self.token = host, label, token
        self.path = directory.rstrip('/') + '/' + LOCK_NAME
        self.q_path = shlex.quote(self.path)
        self.q_owner = shlex.quote(self.path + '/owner')
        self.held = False

    def where(self):
        return f"{self.host}:{self.path}"

    def fence(self, cleanup=''):
        """Shell line that stops a command unless this run still owns the lock."""
        lost = f"the rotation lock {self.path} is no longer held by this run; stopping"
        cleanup = f"{cleanup}; " if cleanup else ''
        return (f"[ \"$(sed -n 's/^token=//p' {self.q_owner} 2>/dev/null)\" = "
                f"{shlex.quote(self.token)} ] || "
                f"{{ {cleanup}printf '%s\\n' {shlex.quote(lost)} >&2; exit 75; }}")

    def acquire(self, break_stale):
        minutes = STALE_LOCK_HOURS * 60
        proc = _ssh(self.host, f"""lock={self.q_path}
            owner={self.q_owner}
            stale() {{ [ -n "$(find "$lock" -maxdepth 0 -mmin +{minutes} 2>/dev/null)" ]; }}
            take() {{
                mkdir -m 700 "$lock" 2>/dev/null || return 1
                printf 'token=%s\\nby=%s\\n' {shlex.quote(self.token)} {shlex.quote(_operator())} > "$owner"
            }}
            if take; then echo ACQUIRED; exit 0; fi
            if [ {int(bool(break_stale))} = 1 ] && stale; then
                echo BROKE
                cat "$owner" 2>/dev/null || true
                rm -rf "$lock"
                if take; then exit 0; fi
            fi
            if stale; then echo STALE; else echo HELD; fi
            cat "$owner" 2>/dev/null || true
            exit 3""")
        lines = proc.stdout.splitlines()
        if proc.returncode == 0 and lines and lines[0] in ('ACQUIRED', 'BROKE'):
            self.held = True
            if lines[0] == 'BROKE':
                print(f"  WARNING: broke the stale rotation lock {self.where()} "
                      f"({self._owner(lines[1:])})", file=sys.stderr)
            return
        markers = [i for i, line in enumerate(lines) if line in ('HELD', 'STALE')]
        if proc.returncode != 3 or not markers:
            _relay(proc.stderr)
            raise Abort(f"could not take the rotation lock {self.where()}. Nothing was changed.")
        state, owner = lines[markers[-1]], self._owner(lines[markers[-1] + 1:])
        message = (f"another key rotation holds the {self.label} lock {self.where()} "
                   f"({owner}). Nothing was changed. ")
        if state == 'STALE':
            message += (f"The lock is older than {STALE_LOCK_HOURS} hours. If that run is no "
                        "longer running, re-run with --break-stale-lock.")
        else:
            message += (f"Wait for it to finish. A lock left by a run that died can be broken "
                        f"with --break-stale-lock once it is {STALE_LOCK_HOURS} hours old, or "
                        f"removed by hand (rm -r {self.path}) after confirming no rotation runs.")
        raise Abort(message)

    @staticmethod
    def _owner(lines):
        by = [line[3:] for line in lines if line.startswith('by=')]
        return 'held by ' + by[0] if by else 'owner unknown'

    def release(self):
        """Remove the lock only if it is still ours; never raise."""
        if not self.held:
            return
        self.held = False
        proc = _ssh(self.host, f"""if [ "$(sed -n 's/^token=//p' {self.q_owner} 2>/dev/null)" = {shlex.quote(self.token)} ]; then
                rm -rf {self.q_path}
            else
                echo NOT-OURS
            fi""")
        if proc.returncode != 0:
            print(f"  WARNING: could not release the rotation lock {self.where()}; remove it "
                  "by hand after confirming no rotation runs.", file=sys.stderr)
        elif 'NOT-OURS' in proc.stdout:
            print(f"  WARNING: the rotation lock {self.where()} now belongs to another run; "
                  "left it in place.", file=sys.stderr)


class Rotation:
    def __init__(self, env, only, apply, allow_partial=False, break_stale_lock=False):
        self.env = env
        self.apply = apply
        self.only = only
        self.allow_partial = allow_partial
        self.break_stale_lock = break_stale_lock
        self.q = {name: shlex.quote(env[name]) for name in REQUIRED}
        self.repo_root = Path(__file__).resolve().parent.parent
        self.local_csv = self.repo_root / 'data' / 'students_merged.csv'
        self.portal_csv = env['PORTAL_DIR'].rstrip('/') + '/data/students_merged.csv'
        self.new_keys = {}
        # One token for both hosts identifies this run in each lock's owner file.
        token = secrets.token_hex(16)
        self.headscale_lock = HostLock(env['HEADSCALE_HOST'], env['HEADSCALE_DIR'], token, 'Headscale')
        self.portal_lock = HostLock(env['PORTAL_HOST'], env['PORTAL_DIR'].rstrip('/') + '/data',
                                    token, 'portal')

    @staticmethod
    def fenced(lock, command, cleanup=''):
        """Prefix a remote command with the lock check while the lock is held."""
        return f"{lock.fence(cleanup)}\n{command}" if lock.held else command

    # Headscale host --------------------------------------------------------
    # What a failure at each Headscale step leaves behind, for the error message.
    AFTERMATH = {
        'inventory': "Nothing was changed.",
        'create': ("Nothing was distributed and no key was expired; the next successful "
                   "run expires any unused new keys."),
        'expire': ("Students already have their new keys; old keys not yet expired still "
                   "work. Re-run --apply to finish."),
    }

    def headscale(self, step, payload):
        source = Path(__file__).read_text(encoding='utf-8')
        command = (f"cd {self.q['HEADSCALE_DIR']} && "
                   f"python3 -c {shlex.quote(source)} --remote {step}")
        command = self.fenced(self.headscale_lock, command)
        proc = _ssh(self.env['HEADSCALE_HOST'], command, json.dumps(payload))
        _relay(proc.stderr, self.new_keys.values())
        try:
            if proc.returncode != 0:
                raise ValueError
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise Abort(f"the Headscale '{step}' step failed on {self.env['HEADSCALE_HOST']}. "
                        + self.AFTERMATH[step])

    # Portal host -----------------------------------------------------------
    def compose_exec(self, *command):
        return (f"cd {self.q['PORTAL_DIR']} && docker compose -f {self.q['PORTAL_COMPOSE']} "
                f"exec -T {APP_SERVICE} " + ' '.join(shlex.quote(c) for c in command))

    def read_roster(self):
        proc = _ssh(self.env['PORTAL_HOST'], f"cat {shlex.quote(self.portal_csv)}")
        if proc.returncode != 0:
            raise Abort(f"cannot read {self.portal_csv} on {self.env['PORTAL_HOST']}.")
        reader = csv.DictReader(io.StringIO(proc.stdout, newline=''))
        fields = reader.fieldnames or []
        if not set(FIELDNAMES) <= set(fields):
            raise Abort(f"{self.portal_csv} lacks the columns {FIELDNAMES}.")
        rows = list(reader)
        logins = [row['SIS Login ID'].strip().lower() for row in rows]
        if not rows:
            raise Abort(f"{self.portal_csv} has no students.")
        if len(set(logins)) != len(logins):
            raise Abort(f"{self.portal_csv} repeats a login; fix the roster first.")
        return fields, rows

    def update_script_present(self):
        proc = _ssh(self.env['PORTAL_HOST'], self.compose_exec('test', '-f', UPDATE_SCRIPT))
        return proc.returncode == 0

    def deliver(self, data, digest, stamp):
        backup_dir = self.env['REMOTE_BACKUP_DIR']
        backup = f"{backup_dir.rstrip('/')}/students_merged-pre-rotation-{stamp}.csv"
        remote_tmp = f"{self.env['PORTAL_DIR'].rstrip('/')}/data/.students_merged-rotation-{stamp}.tmp"
        live, q_backup = shlex.quote(self.portal_csv), shlex.quote(backup)
        proc = _ssh(self.env['PORTAL_HOST'], self.fenced(self.portal_lock, f"""set -e
            umask 077
            mkdir -p {self.q['REMOTE_BACKUP_DIR']}
            chmod 700 {self.q['REMOTE_BACKUP_DIR']}
            cp {live} {q_backup}
            chmod 600 {q_backup}"""))
        if proc.returncode != 0:
            _relay(proc.stderr)
            raise Abort("backing up the portal roster failed. Headscale has unused new keys; "
                        "nothing else changed and no key was expired.")
        print(f"  portal roster backup: {self.env['PORTAL_HOST']}:{backup}")
        scp = subprocess.run(['scp', *SSH_OPTIONS, str(data), f"{self.env['PORTAL_HOST']}:{remote_tmp}"],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
        tmp = shlex.quote(remote_tmp)
        # The same temp-then-mv as term_changeover.sh: the container never sees
        # a half-written roster, and the hash proves the bytes arrived intact.
        # chgrp needs the SSH account to own the file and belong to APP_GID
        # (or be root); if it cannot, set -e stops before the live file changes.
        replace = f"""set -e
            trap {shlex.quote('rm -f ' + tmp)} EXIT
            chgrp {APP_GID} {tmp}
            chmod 640 {tmp}
            mv {tmp} {live}
            test "$(sha256sum {live} | cut -d' ' -f1)" = {digest}"""
        proc = scp if scp.returncode != 0 else _ssh(
            self.env['PORTAL_HOST'], self.fenced(self.portal_lock, replace, cleanup=f"rm -f {tmp}"))
        if proc.returncode != 0:
            _relay(proc.stderr, self.new_keys.values())
            raise Abort("replacing the portal roster failed. The database still holds the old "
                        "keys and no key was expired, so every student's key still works.")

    def update_database(self, targets):
        rows = io.StringIO()
        writer = csv.writer(rows)
        for login in targets:
            writer.writerow([login, self.new_keys[login]])
        proc = _ssh(self.env['PORTAL_HOST'],
                    self.fenced(self.portal_lock, self.compose_exec('python', UPDATE_SCRIPT)),
                    rows.getvalue())
        _relay(proc.stderr, self.new_keys.values())
        try:
            if proc.returncode != 0:
                raise ValueError
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise Abort("the portal database update failed; it rolls back as one transaction. "
                        "Old keys were NOT expired, so every student's current key keeps working. "
                        "The portal roster file already carries the new keys. Fix the database, "
                        "then re-run --apply: it issues fresh keys and expires all older ones.")

    # Steps -------------------------------------------------------------------
    def write_local(self, fields, rows):
        self.local_csv.parent.mkdir(mode=0o700, exist_ok=True)
        previous = os.umask(0o077)
        try:
            handle = tempfile.NamedTemporaryFile(
                'w', dir=self.local_csv.parent, prefix='.students_merged-', suffix='.tmp',
                delete=False, encoding='utf-8', newline='')
            with handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, self.local_csv)
        finally:
            os.umask(previous)
        return hashlib.sha256(self.local_csv.read_bytes()).hexdigest()

    def confirm(self, count):
        if not sys.stdin.isatty():
            raise Abort("--apply needs --yes when no one is at the terminal to confirm.")
        answer = input(f"Rotate {count} students' keys now? Type 'rotate' to continue: ")
        if answer.strip() != 'rotate':
            raise Abort("not confirmed; nothing was changed.")

    def run(self, yes):
        print(f"Mode: {'APPLY' if self.apply else 'DRY RUN (read-only)'}")
        # A dry run writes nothing, so it takes no lock. --apply takes both
        # locks before it reads the roster and keeps them through the expiry:
        # a run that read the roster before another run replaced it would
        # write stale keys back over the newer ones.
        try:
            if self.apply:
                self.headscale_lock.acquire(self.break_stale_lock)
                self.portal_lock.acquire(self.break_stale_lock)
            self._rotate(yes)
        finally:
            self.portal_lock.release()
            self.headscale_lock.release()

    def _rotate(self, yes):
        fields, rows = self.read_roster()
        protected = {self.env['HEADSCALE_KEEP_USER'].strip().lower()}
        protected |= {s.strip().lower() for s in self.env['HEADSCALE_STAFF'].split(',') if s.strip()}
        roster = [row['SIS Login ID'].strip().lower() for row in rows]
        targets = [login for login in roster if login not in protected]
        if self.only is not None:
            bad = [f"{login} is not on the portal roster" for login in self.only if login not in roster]
            bad += [f"{login} is protected by HEADSCALE_KEEP_USER/HEADSCALE_STAFF"
                    for login in self.only if login in protected]
            if bad:
                raise Abort('--only: ' + '; '.join(bad) + '. Nothing was changed.')
            targets = [login for login in targets if login in self.only]
        if not targets:
            raise Abort("no student to rotate.")

        inventory = self.headscale('inventory', {'logins': targets})
        users = inventory['users']
        old_count = sum(len(users[login]['active_key_ids']) for login in users)

        print(f"Portal roster: {len(rows)} rows; protected and skipped: "
              f"{', '.join(sorted(protected & set(roster))) or 'none'}")
        print(f"Students to rotate: {len(targets)}")
        for login in targets:
            if login in users:
                print(f"  {login}: {len(users[login]['active_key_ids'])} active key(s) to expire")
            else:
                print(f"  {login}: NO HEADSCALE USER")
        print(f"New keys: reusable, expiring in {self.env['KEY_DAYS']}. "
              f"Old keys to expire after the portal is updated: {old_count}")

        problems = []
        if inventory['missing']:
            problems.append("no Headscale user for: " + ', '.join(inventory['missing'])
                            + " (create them, or leave them out with --only)")
        if not self.update_script_present():
            problems.append(f"cannot find {UPDATE_SCRIPT} in the running portal container "
                            f"('{APP_SERVICE}'); deploy an image that includes it first")
        if problems:
            raise Abort('; '.join(problems) + '. Nothing was changed.')

        if not self.apply:
            print("\nDry run: nothing was changed. Re-run with --apply to rotate.")
            return
        if not yes:
            self.confirm(len(targets))

        # pid + randomness: two runs in the same second never share a temp
        # file or overwrite each other's backup.
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{secrets.token_hex(4)}"
        print("\n[1/5] Creating new keys on Headscale")
        created = self.headscale('create', {
            'users': {login: users[login]['id'] for login in targets},
            'expiration': self.env['KEY_DAYS'],
        })
        self.new_keys = created['keys']
        if created['failed'] or set(self.new_keys) != set(targets):
            raise Abort(f"key creation stopped at {created['failed']}. Nothing was distributed and "
                        "no key was expired; the next successful run expires the unused new keys.")
        print(f"  created {len(self.new_keys)} reusable keys")

        print("[2/5] Writing the new roster locally")
        for row in rows:
            login = row['SIS Login ID'].strip().lower()
            if login in self.new_keys:
                row['headscale_key'] = self.new_keys[login]
        digest = self.write_local(fields, rows)
        print(f"  wrote {self.local_csv} (mode 0600)")

        print("[3/5] Replacing the portal roster file")
        self.deliver(self.local_csv, digest, stamp)
        print("  verified: the portal roster matches the local file")

        print("[4/5] Updating the portal database")
        result = self.update_database(targets)
        print(f"  updated {len(result['updated'])}, already current {len(result['unchanged'])}, "
              f"not in the database {len(result['unknown'])}")

        print("[5/5] Expiring the old keys")
        stored = set(result['updated']) | set(result['unchanged'])
        expire = {login: {'id': users[login]['id'], 'key_ids': users[login]['active_key_ids']}
                  for login in targets if login in stored and users[login]['active_key_ids']}
        outcome = self.headscale('expire', {'users': expire}) if expire else {'expired': {}, 'failed': {}}
        expired = sum(outcome['expired'].values())
        print(f"  expired {expired} old key(s) for {len(outcome['expired'])} student(s)")

        print("\nSummary")
        print(f"  rotated: {len(self.new_keys)} students; new keys in {self.local_csv}")
        failures = []
        if result['unknown']:
            # Logins only, never keys. Their old keys stay live: expiring a key
            # the portal cannot replace would strand the student.
            unknown = ', '.join(result['unknown'])
            if self.allow_partial:
                print("  WARNING (--allow-partial): not in the portal database, old keys left "
                      "active: " + unknown)
            else:
                failures.append(
                    "not in the portal database, so their old keys were left active: " + unknown
                    + ". Add them to the portal and re-run --apply --only <logins>, or accept "
                    "this with --allow-partial")
        if outcome['failed']:
            failures.append(
                "expiring old keys failed for: " + ', '.join(sorted(outcome['failed']))
                + ". Those students already have new keys; their old keys still work. "
                "Re-run --apply --only <logins> to finish")
        if failures:
            raise Abort('the rotation is incomplete; ' + '. '.join(failures) + '.')


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='rotate_headscale_keys.sh',
        description='Rotate students\' reusable Headscale pre-auth keys. Dry run by default.')
    parser.add_argument('--apply', action='store_true', help='make the changes')
    parser.add_argument('--yes', action='store_true', help='skip the interactive confirmation')
    parser.add_argument('--only', metavar='LOGIN,...',
                        help='rotate only these roster logins (e.g. one test account)')
    parser.add_argument('--allow-partial', action='store_true',
                        help='exit 0 even when a roster login is missing from the portal '
                             'database (its old key stays active)')
    parser.add_argument('--break-stale-lock', action='store_true',
                        help=f'take over a rotation lock older than {STALE_LOCK_HOURS} hours '
                             'left by a run that died')
    args = parser.parse_args(argv)
    if args.only is not None:
        args.only = [s.strip().lower() for s in args.only.split(',') if s.strip()]
        if not args.only:
            parser.error('--only needs at least one login')
    return args


def main(argv):
    if argv[:1] == ['--remote']:
        return remote_main(argv[1])
    # Progress lines and relayed remote errors must interleave in order.
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args(argv)
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print(f"ERROR: required configuration variable {missing[0]} is unset; "
              "set it in the environment or scripts/site.env", file=sys.stderr)
        return 1
    # Refuse before contacting any host: an unattended --apply must be explicit.
    if args.apply and not args.yes and not sys.stdin.isatty():
        print("ERROR: --apply needs --yes when no one is at the terminal to confirm.",
              file=sys.stderr)
        return 1
    rotation = Rotation(os.environ, args.only, args.apply,
                        allow_partial=args.allow_partial, break_stale_lock=args.break_stale_lock)
    try:
        rotation.run(args.yes)
    except Abort as error:
        print(f"ERROR: {scrub(str(error), rotation.new_keys.values())}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
