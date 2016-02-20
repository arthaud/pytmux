#!/usr/bin/env python3

import argparse
import fcntl
import json
import platform
import re
import struct
import sys
import termios
import time


def get_hw(fd):
    '''Return the size of the tty asociated to the given file descriptor'''
    assert platform.system() != 'Windows'

    buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\x00' * 8)
    return struct.unpack('hhhh', buf)[0:2]


def replay(f, check_height=True, check_width=True):
    # find the size of the screen
    while True:
        line = f.readline()

        if not line:
            print('error: could not find the terminal size in the log file', file=sys.stderr)
            exit(1)

        match = re.match(r'INFO:replay:(\d+):SIZE (\d+) (\d+)\n', line)

        if match:
            last_timestamp = int(match.group(1))
            height = int(match.group(2))
            width = int(match.group(3))
            break

    # check the terminal size
    real_height, real_width = get_hw(sys.stdout)

    if check_height and real_height != height:
        print('error: wrong terminal height (expected %d, got %d)' % (height, real_height), file=sys.stderr)
        exit(2)

    if check_width and real_width != width:
        print('error: wrong terminal width (expected %d, got %d)' % (width, real_width), file=sys.stderr)
        exit(2)

    # play
    while True:
        line = f.readline()

        if not line:
            break

        match = re.match(r'INFO:replay:(\d+):WRITE (.*)\n', line)
        if match:
            timestamp = int(match.group(1))
            data = json.loads(match.group(2))

            diff = timestamp - last_timestamp
            if diff > 0.005:
                time.sleep(diff)

            sys.stdout.write(data)
            sys.stdout.flush()
            last_timestamp = timestamp


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Replay a tmux session')
    parser.add_argument('file',
                        help='The log file (example: tmux.log)',
                        type=argparse.FileType())
    parser.add_argument('--no-check-height',
                        help='Do not Check the height of the current window',
                        action='store_true')
    parser.add_argument('--no-check-width',
                        help='Do not check the width of the current window',
                        action='store_true')

    args = parser.parse_args()
    replay(args.file, not args.no_check_height, not args.no_check_width)
