#!/usr/bin/env python3
import os, time, smtplib, logging, subprocess, json, shlex
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- Postgres ---
import psycopg2
import psycopg2.extras

# -------- Settings from env (with sensible defaults) --------
HOST = os.getenv('NTP_HOST', 'myntp')
IP_FALLBACK = os.getenv('NTP_IP', '0.0.0.0')
SSH_USER = os.getenv('SSH_USER', 'ubuntu')
SSH_PORT = os.getenv('SSH_PORT', '22')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL_SEC', '30'))
MAX_ACCEPTABLE_STRATUM = int(os.getenv('MAX_STRATUM', '4'))
MAX_ABS_OFFSET_SEC = float(os.getenv('MAX_ABS_OFFSET_SEC', '0.050'))
CGPS_TIMEOUT_SEC = int(os.getenv('CGPS_TIMEOUT_SEC', '8'))
GPSPIPE_SAMPLES = int(os.getenv('GPSPIPE_SAMPLES', '5'))
LOG_PATH = os.getenv('LOG_PATH', '/var/log/ntp-checker.log')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'DEBUG').upper()
PY_UNBUFFERED = os.getenv('PYTHONUNBUFFERED', '1')

# --- DB config ---
DB_URL = os.getenv('DATABASE_URL')  # postgresql://user:pass@host:5432/ntp-checker
POSTGRES_TABLE = os.getenv('POSTGRES_TABLE', 'metrics.ntp_parent')

# -------- Logging setup --------
numeric_level = getattr(logging, LOG_LEVEL, logging.DEBUG)
logging.basicConfig(filename=LOG_PATH, level=numeric_level,
                    format='%(asctime)s:%(levelname)s:%(message)s')
console = logging.StreamHandler()
console.setLevel(numeric_level)
console.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(message)s'))
logging.getLogger().addHandler(console)

def _head(text: str, n: int = 3) -> str:
    lines = [l for l in (text or '').splitlines() if l.strip()]
    return ' / '.join(lines[:n])

def _now() -> str:
    return datetime.utcnow().isoformat() + 'Z'

# -------- Email via SES --------
def send_email(subject: str, body: str) -> None:
    sender = os.getenv('EMAIL_SENDER')
    receivers = [e for e in [os.getenv('EMAIL_RECEIVER1'), os.getenv('EMAIL_RECEIVER2')] if e]
    userid = os.getenv('SMTP_USERNAME')
    password = os.getenv('SMTP_PASSWORD')

    logging.debug(f'Email prep: sender={sender}, receivers={receivers}, subject={subject}')
    if not (sender and receivers and userid and password):
        logging.error('Email not sent: missing SES env vars')
        return
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = ', '.join(receivers)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        logging.debug('SMTP connect start')
        with smtplib.SMTP_SSL('email-smtp.us-east-1.amazonaws.com', 465) as s:
            s.login(userid, password)
            s.sendmail(sender, receivers, msg.as_string())
        logging.info('Alert email sent')
    except Exception as e:
        logging.error(f'Failed to send email: {e}')

# -------- SSH runner with rich logging --------
def run_ssh(cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
    full_cmd = [
        'ssh',
        '-p', SSH_PORT,
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ConnectTimeout=5',
        f'{SSH_USER}@{HOST}',
        cmd
    ]
    start = time.time()
    logging.debug(f'RUN SSH start={_now()} timeout={timeout}s cmd={shlex.join(full_cmd)}')
    try:
        cp = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.error(f'SSH TIMEOUT after {timeout}s: cmd={cmd}')
        raise
    dur = f'{(time.time()-start):.3f}s'
    logging.debug(f'RUN SSH end={_now()} dur={dur} rc={cp.returncode} '
                  f'stdout={_head(cp.stdout)} stderr={_head(cp.stderr)}')
    return cp

# -------- Parsers with logging --------
def parse_tracking(text: str) -> dict:
    result = {'leap_status': None, 'stratum': None, 'last_offset_sec': None}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('Leap status'):
            result['leap_status'] = line.split(':', 1)[1].strip()
        elif line.startswith('Stratum'):
            try: result['stratum'] = int(line.split(':', 1)[1].strip())
            except ValueError: pass
        elif line.startswith('Last offset'):
            try:
                val = line.split(':', 1)[1].strip().split()[0]
                result['last_offset_sec'] = float(val)
            except Exception: pass
    logging.debug(f'parse_tracking => {result}')
    return result

def parse_sources(text: str) -> dict:
    has_selected, selected_line, total = False, None, 0
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith('MS') or s.startswith('Name/IP'):
            continue
        total += 1
        if s.startswith('^*') or '* ' in s or ' ^* ' in s:
            has_selected, selected_line = True, s
    out = {'has_selected': has_selected, 'selected_line': selected_line, 'total_sources': total}
    logging.debug(f'parse_sources => {out}')
    return out

# -------- GPS via gpspipe (JSON) with logging --------
def run_gps_status() -> tuple[bool, str]:
    remote_cmd = (
        f"bash -lc '"
        f"if ! command -v gpspipe >/dev/null 2>&1; then echo gpspipe-not-found >&2; exit 127; fi; "
        f"if command -v timeout >/dev/null 2>&1; then "
        f"  timeout {CGPS_TIMEOUT_SEC} gpspipe -w -n {GPSPIPE_SAMPLES}; "
        f"else "
        f"  gpspipe -w -n {GPSPIPE_SAMPLES}; "
        f"fi'"
    )
    logging.debug(f'run_gps_status: invoking gpspipe with samples={GPSPIPE_SAMPLES} timeout={CGPS_TIMEOUT_SEC}')
    try:
        cp = run_ssh(remote_cmd, timeout=CGPS_TIMEOUT_SEC + 3)
    except subprocess.TimeoutExpired:
        return (False, 'gpspipe timed out')

    if cp.returncode == 127 or 'gpspipe-not-found' in (cp.stderr or ''):
        logging.warning('gpspipe binary not found on remote')
        return (False, 'gpspipe not found on remote host')

    out = (cp.stdout or '') + (cp.stderr or '')
    has_fix, summary, tpv_count, last_mode = parse_gpspipe_output(out)
    logging.debug(f'run_gps_status: tpv_count={tpv_count} last_mode={last_mode} has_fix={has_fix} summary={summary}')
    if summary:
        return (has_fix, summary)
    return (False, _head(out) or 'no TPV data from gpspipe')

def parse_gpspipe_output(text: str) -> tuple[bool, str, int, int | None]:
    has_fix = False
    tpv_count = 0
    last_mode = None
    last_time = None
    last_lat = None
    last_lon = None

    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith('{'):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get('class') == 'TPV':
            tpv_count += 1
            mode = obj.get('mode')
            if mode is not None:
                last_mode = mode
                has_fix = mode >= 2
            last_time = obj.get('time') or last_time
            last_lat = obj.get('lat') or last_lat
            last_lon = obj.get('lon') or last_lon

    if last_mode is not None:
        mode_txt = {0:'Unknown',1:'No fix',2:'2D fix',3:'3D fix'}.get(last_mode, str(last_mode))
        parts = [f'mode={mode_txt}']
        if last_time: parts.append(f'time={last_time}')
        if last_lat is not None and last_lon is not None:
            parts.append(f'pos=({last_lat},{last_lon})')
        return has_fix, 'GPS(TPV): ' + ' | '.join(parts), tpv_count, last_mode
    return False, '', tpv_count, last_mode

# -------- Health check wrapper --------
def check_ntp_health() -> tuple[bool, str]:
    logging.debug('check_ntp_health: chronyc tracking')
    tr = run_ssh('chronyc tracking', timeout=10)
    if tr.returncode != 0:
        return False, f'SSH/chronyc tracking failed on {HOST} ({IP_FALLBACK}): {_head(tr.stderr)}'
    tracking = parse_tracking(tr.stdout)
    leap, stratum, offset = tracking['leap_status'], tracking['stratum'], tracking['last_offset_sec']

    logging.debug('check_ntp_health: chronyc sources -n')
    sr = run_ssh('chronyc sources -n', timeout=10)
    if sr.returncode != 0:
        return False, f'SSH/chronyc sources failed on {HOST} ({IP_FALLBACK}): {_head(sr.stderr)}'
    sources = parse_sources(sr.stdout)

    logging.debug('check_ntp_health: gps status via gpspipe')
    gps_ok, gps_summary = run_gps_status()

    problems = []
    if leap is None or 'Normal' not in leap:
        problems.append(f'Leap status not Normal (got {leap})')
    if stratum is None or stratum > MAX_ACCEPTABLE_STRATUM:
        problems.append(f'Stratum too high (got {stratum}, max {MAX_ACCEPTABLE_STRATUM})')
    if offset is None or abs(offset) > MAX_ABS_OFFSET_SEC:
        problems.append(f'Time offset too large (abs {offset}s > {MAX_ABS_OFFSET_SEC}s)')
    if not sources['has_selected']:
        problems.append('No selected NTP source in chronyc sources')
    if sources['total_sources'] == 0:
        problems.append('No NTP sources visible in chronyc sources')
    if not gps_ok:
        problems.append('GPS has no fix via gpspipe')

    detail = [
        f'Host: {HOST} ({IP_FALLBACK})',
        f'Leap: {leap}',
        f'Stratum: {stratum}',
        f'Last offset: {offset} sec',
        f'Selected source: {sources["selected_line"]}',
        f'Total sources: {sources["total_sources"]}',
        f'GPS: {gps_summary}'
    ]

    if problems:
        return False, ' | '.join(problems) + ' || ' + ' | '.join(detail)
    return True, 'OK | ' + ' | '.join(detail)

# -------- DB insert helper --------
def db_insert_sample(tracking: dict, sources: dict, gps_ok: bool, gps_summary: str) -> None:
    """
    Insert a single row into the configured table (default metrics.ntp_parent).
    Uses a short-lived connection for robustness in K8s.
    """
    if not DB_URL:
        logging.debug('DB write skipped: no DATABASE_URL set')
        return
    try:
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {POSTGRES_TABLE}
                      (ts, last_offset_sec, stratum, total_sources, leap_status, gps_mode, selected_source, gps_summary)
                    VALUES (now(), %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ts) DO NOTHING;
                    """,
                    (
                        tracking.get('last_offset_sec'),
                        tracking.get('stratum'),
                        sources.get('total_sources'),
                        tracking.get('leap_status'),
                        '3D fix' if gps_ok else 'No fix',
                        sources.get('selected_line'),
                        gps_summary
                    )
                )
        logging.debug('DB write OK')
    except Exception as e:
        logging.error(f'DB write failed: {e}')

# -------- Main loop --------
def main():
    logging.info(f'Starting NTP health monitor for {HOST} ({IP_FALLBACK})...')
    logging.info(f'Env summary: SSH_USER={SSH_USER} SSH_PORT={SSH_PORT} CHECK_INTERVAL_SEC={CHECK_INTERVAL} '
                 f'MAX_STRATUM={MAX_ACCEPTABLE_STRATUM} MAX_ABS_OFFSET_SEC={MAX_ABS_OFFSET_SEC} '
                 f'CGPS_TIMEOUT_SEC={CGPS_TIMEOUT_SEC} GPSPIPE_SAMPLES={GPSPIPE_SAMPLES} LOG_LEVEL={LOG_LEVEL}')
    # Confirm binaries
    try:
        which = subprocess.run(['which','ssh'], capture_output=True, text=True)
        logging.info(f'which ssh => {which.stdout.strip() or which.stderr.strip()}')
    except Exception as e:
        logging.warning(f'which ssh failed: {e}')

    while True:
        try:
            ok, detail = check_ntp_health()
            if ok:
                logging.info(detail)
                # Collect values once more for DB (fast SSH calls, same as in check_ntp_health)
                try:
                    tr = run_ssh('chronyc tracking', timeout=10)
                    sr = run_ssh('chronyc sources -n', timeout=10)
                    tracking = parse_tracking(tr.stdout)
                    sources  = parse_sources(sr.stdout)
                    gps_ok, gps_summary = run_gps_status()
                    db_insert_sample(tracking, sources, gps_ok, gps_summary)
                except Exception as db_wrap_e:
                    logging.error(f'DB collection/write error: {db_wrap_e}')
            else:
                logging.error(detail)
                send_email('NTP health alert on observium.andrea-house.com', f'{detail}\n')
        except subprocess.TimeoutExpired:
            msg = f'SSH command timed out contacting {HOST} ({IP_FALLBACK})'
            logging.error(msg)
            send_email('NTP health alert timeout', msg)
        except Exception as e:
            msg = f'Unexpected error during NTP check: {e}'
            logging.exception(msg)  # includes traceback
            send_email('NTP health alert exception', msg)
        logging.debug(f'sleeping {CHECK_INTERVAL}s before next check')
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()



