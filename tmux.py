#!/usr/bin/env python3
'''
A simple tmux clone in python using curses

Notes:
    To follow curses convention, we use (y, x) and not (x, y)
    We need to call leaveok(1) before each call to refresh() to avoid some blinks
'''

from datetime import datetime
import curses
import fcntl
import locale
import logging
import os
import platform
import re
import select
import signal
import string
import struct
import sys
import termios
import unicodedata

logging.basicConfig(filename='tmux.log',
                    filemode='w',
                    level=logging.DEBUG)
log = logging.getLogger('tmux')


def gethw():
    '''Return the screen real size'''
    assert platform.system() != 'Windows'

    buf = fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, b'\x00' * 8)
    return struct.unpack("hhhh", buf)[0:2]


def addstr(win, y, x, s, *args, **kwargs):
    try:
        win.addstr(y, x, s, *args, **kwargs)
    except curses.error:
        pass # writing on the last col/row raises an exception


class Point:
    def __init__(self, y, x):
        self.y = y
        self.x = x

    def __repr__(self):
        return 'Point(y=%d, x=%d)' % (self.y, self.x)


class Cursor(Point):
    def __init__(self, y, x, visibility):
        self.y = y
        self.x = x
        self.visibility = visibility

    def __repr__(self):
        return 'Cursor(y=%d, x=%d, visibility=%d)' % (self.y, self.x, self.visibility)


class Window:
    def __init__(self, height, width, begin_y, begin_x):
        self.win = curses.newwin(height, width, begin_y, begin_x)
        self.size = height, width

    @property
    def width(self):
        return self.size[1]

    @property
    def height(self):
        return self.size[0]

    def resize(self, height, width, begin_y, begin_x):
        self.win.mvwin(begin_y, begin_x)
        self.win.resize(height, width)
        self.size = height, width

    def refresh(self):
        raise NotImplementedError


# TODO: Should we remove it?
class Colors:
    def __init__(self):
        self.pairs = {}
        self.next = 1

    def get(self, fg, bg):
        if (fg, bg) in self.pairs:
            return self.pairs[fg, bg]

        pair_num = self.next
        self.next += 1

        curses.init_pair(pair_num, fg, bg)
        self.pairs[fg, bg] = curses.color_pair(pair_num)
        return self.pairs[fg, bg]

colors = Colors()


class BannerWindow(Window):
    def refresh(self):
        self.win.leaveok(1)
        left = '[0] tmux.py'
        right = '"%s" %s' % (platform.node(),
                             datetime.now().strftime('%H:%M %d-%m-%Y'))
        banner = left + ' ' * (self.width - len(left) - len(right)) + right

        addstr(self.win, 0, 0, banner,
               colors.get(curses.COLOR_BLACK, curses.COLOR_BLUE))

        self.win.refresh()
        self.win.leaveok(0)


class ConsoleWindow(Window):
    def __init__(self, height, width, begin_y, begin_x, history_size):
        super(ConsoleWindow, self).__init__(height, width, begin_y, begin_x)
        self.history_size = history_size

        # the buffer
        self.lines = []
        self.lines.append(['', # line content
                           0]) # real line number

        # There are two windows:
        # - the real window, where the cursor is and where writes are performed
        # - the display window. This is usually the real window except when you
        #     look at the history
        self.display_offset = 0 # first line of the display window
        self.offset = 0 # first line of the real window
        self.cursor = Cursor(0, 0, # position in the real window
                             visibility=1)
        self.auto_scroll = True
        self.redraw = True

    def resize(self, height, width, begin_y, begin_x):
        log.debug('resize from %r to %r', self.size, (height, width))
        prev_height, prev_width = self.size
        real_y, real_x = self._cursor_real_pos()
        super(ConsoleWindow, self).resize(height, width, begin_y, begin_x)

        if prev_height > height:
            diff = prev_height - height
            self.offset = min(self.offset + diff, len(self.lines) - 1)
            self.display_offset = min(self.display_offset + diff, len(self.lines) - 1)
            self.cursor.y = max(0, self.cursor.y - diff)

        if prev_width != width:
            # index of the current real line
            prev_y = self._index_real_line(real_y)

            # rebuild the buffer
            self._rebuild_lines(prev_width, width)

            # index of the current real line, after the rebuild
            new_y = self._index_real_line(real_y)

            self.offset = max(0, self.offset + new_y - prev_y)
            self.display_offset = max(0, self.display_offset + new_y - prev_y)

            if new_y + (real_x // width) - self.offset > height - 1:
                # cursor outside of the window
                self.offset = new_y + (real_x // width) - (height - 1)

            if self.auto_scroll:
                self.display_offset = self.offset

            self.cursor.y = max(0, new_y + (real_x // width) - self.offset)
            self.cursor.x = max(0, real_x % width)

            # Note that self.offset can be updated in _insert_newline
            # because of _check_history_size
            while self.offset + self.cursor.y >= len(self.lines):
                self._insert_newline(real=False)

        self.redraw = True

        log.debug('real_y = %d, real_x = %d', real_y, real_x)
        log.debug('self.display_offset = %d, self.offset = %d, self.cursor = (%d, %d)',
                  self.display_offset, self.offset, self.cursor.y, self.cursor.x)
        log.debug('self.lines = ')
        for line in self.lines:
            log.debug('  %r', line)

    def _rebuild_lines(self, prev_width, new_width):
        lines = []
        current_line, current_num = self.lines[0]

        for line, num in self.lines[1:]:
            if current_num == num: # same line
                current_line += ' ' * ((prev_width - len(current_line) % prev_width) % prev_width)
                current_line += line
            else:
                if not current_line:
                    lines.append(['', current_num])
                else:
                    while current_line:
                        lines.append([current_line[:new_width], current_num])
                        current_line = current_line[new_width:]

                current_line, current_num = line, num

        if not current_line:
            lines.append(['', current_num])
        else:
            while current_line:
                lines.append([current_line[:new_width], current_num])
                current_line = current_line[new_width:]

        self.lines = lines

    def refresh(self):
        if self.redraw:
            self.win.leaveok(1)
            self.win.clear()

            for i in range(self.display_offset, self.display_offset + self.height):
                if i >= len(self.lines):
                    break

                addstr(self.win, i - self.display_offset, 0, self.lines[i][0])

            self.redraw = False
            self.win.leaveok(0)

        if 0 <= self.offset + self.cursor.y - self.display_offset < self.height:
            self.win.move(self.offset + self.cursor.y - self.display_offset,
                          self.cursor.x)
            visibility = 1
        else:
            visibility = 0

        if self.cursor.visibility != visibility:
            curses.curs_set(visibility)
            self.cursor.visibility = visibility

        self.win.refresh()

    def do_command(self, key):
        log.debug('do_command(%r)', key)

        # FIXME: For debugging purpose only
        if key == b'\x1b[A':
            self.cursor.y = max(0, self.cursor.y - 1)
        elif key == b'\x1b[B':
            self.cursor.y = min(self.height - 1, self.cursor.y + 1)
        elif key == b'\x1b[C':
            self.cursor.x = min(self.width - 1, self.cursor.x + 1)
        elif key == b'\x1b[D':
            self.cursor.x = max(0, self.cursor.x - 1)

    def write(self, data):
        '''Write data at the current cursor position'''
        assert self.offset + self.cursor.y < len(self.lines)

        if isinstance(data, bytes):
            data = data.decode('utf8', 'replace')

        current = ''
        while data:
            c = data[0]
            remove = 1

            if c == '\x1b':
                self._write_line(current)
                remove = self._control_seq(data)
                current = ''
            elif c == '\b':
                self._write_line(current)
                self.cursor.x = max(0, self.cursor.x - 1)
                current = ''
            elif c == '\t':
                current = self._expand_tab(current)
            elif c == '\n':
                self._write_line(current)
                self._cursor_newline(real=True)
                current = ''
            elif c == '\r':
                self._write_line(current)
                self.cursor.x = 0
                current = ''
            else:
                if unicodedata.category(c) in ('Cc', 'Cf', 'Cn', 'Cs'): # control characters
                    c = curses.unctrl(ord(c)).decode('utf8')

                current += c

            data = data[remove:]

        self._write_line(current)

        log.debug('self.display_offset = %d, self.offset = %d, self.cursor = (%d, %d)',
                  self.display_offset, self.offset, self.cursor.y, self.cursor.x)
        log.debug('self.lines = ')
        for line in self.lines:
            log.debug('  %r', line)

    def _cursor_newline(self, real):
        '''Add a new line at the cursor position (if needed)

        Arguments:
            real(bool): Is it a real line or just a line wrapped?
        '''
        self.cursor.x = 0

        if self.offset + self.cursor.y == len(self.lines) - 1: # last line
            line_num = self.lines[-1][1]
            if real:
                line_num += 1
            self.lines.append(['', line_num])

        if self.cursor.y == self.height - 1: # scroll down
            self.offset += 1

            if self.auto_scroll:
                self.display_offset = self.offset
                self.redraw = True
        else:
            self.cursor.y += 1

        self._check_history_size()

    def _insert_newline(self, real):
        '''Insert a new line at the end of the buffer

        Arguments:
            real(bool): Is it a real line or just a line wrapped?
        '''
        assert len(self.lines) + 1 <= self.offset + self.height

        line_num = self.lines[-1][1]
        if real:
            line_num += 1
        self.lines.append(['', line_num])

        self._check_history_size()

    def _check_history_size(self):
        '''Shrink the history if needed

        Note: that method can update self.lines, self.display_offset and self.offset
        '''
        if len(self.lines) > self.history_size:
            nb = len(self.lines) - self.history_size
            self.lines = self.lines[nb:]
            self.display_offset = max(0, self.display_offset - nb)
            self.offset -= nb

    def _write_line(self, data):
        assert isinstance(data, str)
        assert self.offset + self.cursor.y < len(self.lines)
        assert all(c not in data for c in ('\b', '\t', '\n', '\r', '\x1b'))

        if not data:
            return

        while data:
            y, x = self.offset + self.cursor.y, self.cursor.x
            line = data[:self.width - x]
            data = data[self.width - x:]

            # update buffer
            self.lines[y][0] = (self.lines[y][0][:x].ljust(x, ' ') +
                                line +
                                self.lines[y][0][x + len(line):])

            # update screen directly (only if the window won't be redraw completely)
            if not self.redraw and self.display_offset <= y < self.display_offset + self.height:
                addstr(self.win, y - self.display_offset, x, line)

            self.cursor.x += len(line)
            if self.cursor.x >= self.width:
                self._cursor_newline(real=False)

    def _index_real_line(self, line_num):
        '''
        Return the index in self.lines of the first line corresponding to
        the real line `line_num`
        '''
        for y, (_, num) in enumerate(self.lines):
            if num == line_num:
                return y

        assert False, 'line not found'

    def _cursor_real_pos(self):
        '''Return the real position of the cursor in the buffer'''

        real_y = self.lines[self.offset + self.cursor.y][1]
        real_x = self.cursor.x

        i = self.offset + self.cursor.y - 1
        while i >= 0 and self.lines[i][1] == real_y:
            real_x += self.width
            i -= 1

        return real_y, real_x

    def _move_cursor_win(self, y, x):
        '''Move the cursor

        Arguments:
            y, x: coordinates relative to the window
        '''
        y = min(max(0, y), self.height - 1)
        x = min(max(0, x), self.width - 1)

        # Note that self.offset can be updated in _insert_newline because of _check_history_size
        while self.offset + y >= len(self.lines):
            self._insert_newline(real=True)

        self.cursor.y, self.cursor.x = y, x

    def _expand_tab(self, current):
        assert self.offset + self.cursor.y < len(self.lines)

        _, x = self._cursor_real_pos() # num of chars before the cursor
        x += len(current) # num of chars after the cursor
        return current + ' ' * (8 - x % 8)

    def _control_seq(self, data):
        assert self.offset + self.cursor.y < len(self.lines)

        for regex, fun in ((r'^\x1b\[(\d+;\d+)?H', self._ctl_cursor_home),
                           (r'^\x1b\[(\d+;\d+)?f', self._ctl_cursor_home),
                           (r'^\x1b\[(\d+)?A', self._ctl_cursor_up),
                           (r'^\x1b\[(\d+)?B', self._ctl_cursor_down),
                           (r'^\x1b\[(\d+)?C', self._ctl_cursor_forward),
                           (r'^\x1b\[(\d+)?D', self._ctl_cursor_backward),
                           (r'^\x1b\[K', self._ctl_erase_end_line),
                           (r'^\x1b\[1K', self._ctl_erase_start_line),
                           (r'^\x1b\[2K', self._ctl_erase_entire_line),
                           (r'^\x1b\[J', self._ctl_erase_down),
                           (r'^\x1b\[1J', self._ctl_erase_up),
                           (r'^\x1b\[2J', self._ctl_erase_screen),
                           (r'^\x1b\[(\d+(;\d+)*)m', lambda s: None),
                           (r'^\x1b\[\?2004(h|l)', lambda s: None)):
            match = re.search(regex, data)
            if match:
                fun(match)
                return len(match.group(0))

        log.error('Unable to parse control sequence %r', data[:16])
        return 1

    def _ctl_cursor_home(self, match):
        y, x = 0, 0
        if match.group(1):
            y, x = map(int, match.group(1).split(';'))

        self._move_cursor_win(y, x)

    def _ctl_cursor_up(self, match):
        offset = 1
        if match.group(1):
            offset = int(match.group(1))

        self._move_cursor_win(max(0, self.cursor.y - offset), self.cursor.x)

    def _ctl_cursor_down(self, match):
        offset = 1
        if match.group(1):
            offset = int(match.group(1))

        self._move_cursor_win(min(self.height - 1, self.cursor.y + offset), self.cursor.x)

    def _ctl_cursor_forward(self, match):
        offset = 1
        if match.group(1):
            offset = int(match.group(1))

        self._move_cursor_win(self.cursor.y, min(self.width - 1, self.cursor.x + offset))

    def _ctl_cursor_backward(self, match):
        offset = 1
        if match.group(1):
            offset = int(match.group(1))

        self._move_cursor_win(self.cursor.y, max(0, self.cursor.x - offset))

    def _ctl_erase_end_line(self, match):
        y, x = self.offset + self.cursor.y, self.cursor.x

        # update buffer
        self.lines[y][0] = self.lines[y][0][:x]

        # update screen directly (only if the window won't be redraw completely)
        if not self.redraw and self.display_offset <= y < self.display_offset + self.height:
            addstr(self.win, y - self.display_offset, x, ' ' * (self.width - x))

    def _ctl_erase_start_line(self, match):
        y, x = self.offset + self.cursor.y, self.cursor.x

        # update buffer
        self.lines[y][0] = ' ' * (x + 1) + self.lines[y][0][x + 1:]

        # update screen directly (only if the window won't be redraw completely)
        if not self.redraw and self.display_offset <= y < self.display_offset + self.height:
            addstr(self.win, y - self.display_offset, 0, ' ' * (x + 1))

    def _ctl_erase_entire_line(self, match):
        y = self.offset + self.cursor.y

        # update buffer
        self.lines[y][0] = ''

        # update screen directly (only if the window won't be redraw completely)
        if not self.redraw and self.display_offset <= y < self.display_offset + self.height:
            addstr(self.win, y - self.display_offset, 0, ' ' * self.width)

    def _ctl_erase_down(self, match):
        y, x = self.offset + self.cursor.y, self.cursor.x

        # update buffer
        self.lines[y][0] = self.lines[y][0][:x]
        self.lines = self.lines[:y + 1]
        self.redraw = True

    def _ctl_erase_up(self, match):
        y, x = self.offset + self.cursor.y, self.cursor.x

        # update buffer
        self.lines[y][0] = ' ' * (x + 1) + self.lines[y][0][x + 1:]
        for i in range(self.offset, y):
            self.lines[i][0] = ''
        self.redraw = True

    def _ctl_erase_screen(self, match):
        self._ctl_erase_up(match)
        self._ctl_erase_down(match)

    def scroll(self, offset):
        if not self.display_offset + offset >= 0:
            return

        self.display_offset += offset
        self.auto_scroll = False # disable auto scroll
        self.redraw = True

    def deactivate_scroll(self):
        self.display_offset = self.offset
        self.auto_scroll = True
        self.redraw = True


class ScreenManager:
    def __init__(self, screen):
        height, width = gethw()
        self.screen = screen
        self.banner = BannerWindow(1, width, height - 1, 0)
        self.console = ConsoleWindow(height - 1, width, 0, 0, 200)
        self.resize_event = False

    def refresh(self):
        self.screen.leaveok(1)
        self.screen.refresh()
        self.screen.leaveok(0)

        self.banner.refresh()
        self.console.refresh()

    def resize(self):
        height, width = gethw()
        curses.resizeterm(height, width)
        curses.update_lines_cols()

        self.screen.resize(height, width)
        self.banner.resize(1, width, height - 1, 0)
        self.console.resize(height - 1, width, 0, 0)
        self.resize_event = False

        self.screen.clear()
        self.refresh()

    def get_key(self):
        fd = sys.stdin.fileno()

        if select.select([fd], [], [], 0) == ([fd], [], []):
            return os.read(fd, 1024)
        else:
            return None

    def sigwinch(self, *args):
        self.resize_event = True

    def sigcont(self, *args):
        self.resize_event = True

    def main_loop(self):
        old_sigwinch = signal.signal(signal.SIGWINCH, self.sigwinch) # window resized
        old_sigcont = signal.signal(signal.SIGCONT, self.sigcont) # redraw after being suspended

        try:
            self.refresh()

            while True:
                key = self.get_key()

                if key:
                    self.console.do_command(key)

                    if key == b'\x04' or key == b'\x03': # EOF or Ctrl-C
                        break
                    elif key == b'+':
                        self.console.scroll(+1)
                    elif key == b'-':
                        self.console.scroll(-1)
                    elif key == b'*':
                        self.console.deactivate_scroll()
                    # FIXME: For debugging purpose only
                    elif key == b'a':
                        with open('/bin/ls', 'rb') as f:
                            data = f.read(200)

                        self.console.write(data)
                    elif key == b'b':
                        self.console.redraw = True
                    elif key == b'c':
                        self.console.write('a\n')
                    elif key == b'd':
                        self.console.write('123456789')
                    elif key == b'e':
                        self.console.write('\rabcd')
                    elif key == b'f':
                        self.console.write('\n'.join(map(str, range(40))))
                    elif key == b'g':
                        self.console.write('\tb\n \td\n')
                    elif key == b'h':
                        self.console.write('1234\n56789\x1b[20;15HZZZ')
                    elif key == b'i':
                        self.console.write('AAAA\nBBBB\x1b[AZ')
                    elif key == b'j':
                        self.console.write('\x1b[K')
                    elif key == b'k':
                        self.console.write('\x1b[2J')
                    elif key == b'l':
                        self.console.write(string.ascii_uppercase)

                    self.refresh()

                if self.resize_event:
                    self.resize()

                curses.napms(5)
        finally:
            signal.signal(signal.SIGWINCH, old_sigwinch)
            signal.signal(signal.SIGCONT, old_sigcont)


def main(screen):
    curses.use_default_colors()
    screen.keypad(0)
    screen.nodelay(1)

    screen_manager = ScreenManager(screen)
    screen_manager.main_loop()


if __name__ == '__main__':
    if not sys.stdin.isatty():
        print('error: %s needs to run inside a tty' % sys.argv[0], file=sys.stderr)
        exit(1)

    locale.setlocale(locale.LC_ALL, '')
    curses.wrapper(main)
