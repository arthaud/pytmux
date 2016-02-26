#!/usr/bin/env python3
import curses
import locale
import os


def main(screen):
    curses.use_default_colors()
    data = []

    while True:
        ch = os.read(0, 1024)
        if ch == b'\x04':
            return

        data.append(ch)
        screen.addstr(0, 0, repr(data))
        screen.refresh()


if __name__ == '__main__':
    locale.setlocale(locale.LC_ALL, '')
    curses.wrapper(main)
