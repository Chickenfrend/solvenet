"""Write one external worker key file using hidden terminal input (no API calls)."""

import getpass
import os
from pathlib import Path
import signal
import sys
import warnings


def write_key(path):
    # Refuse getpass's echoing fallback when no hidden terminal is available.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        key = getpass.getpass('OpenAI API key (hidden): ')
    if not key or key.strip() != key or '\n' in key or '\r' in key:
        raise ValueError('Invalid credential format')
    fd = None
    created = False
    complete = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, 'w') as output:
            fd = None
            output.write(key + '\n')
        complete = True
    finally:
        if fd is not None:
            os.close(fd)
        # Only remove a file created by us; an existing file is never replaced.
        if created and not complete:
            Path(path).unlink(missing_ok=True)


def main():
    if len(sys.argv) != 2:
        print('Usage: python3 worker/setup_openai_key.py EXTERNAL_KEY_FILE', file=sys.stderr)
        return 1
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        write_key(sys.argv[1])
    except (OSError, ValueError, getpass.GetPassWarning, KeyboardInterrupt):
        print('Key setup failed or interrupted; use an absent file in a private external directory and a hidden terminal.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
