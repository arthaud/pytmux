#!/usr/bin/env python3
'''
A simple tmux clone in python using curses
'''

from datetime import datetime
import copy
import curses
import fcntl
import json
import locale
import logging
import math
import os
import platform
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import unicodedata

logging.basicConfig(filename='tmux.log',
                    filemode='w',
                    level=logging.DEBUG)
log = logging.getLogger('tmux')
replay = logging.getLogger('replay')


def get_hw(fd):
    '''Return the size of the tty asociated to the given file descriptor'''
    assert platform.system() != 'Windows'

    buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\x00' * 8)
    return struct.unpack('hhhh', buf)[0:2]


def set_hw(fd, height, width):
    '''Set the size of the tty asociated to the given file descriptor'''
    assert platform.system() != 'Windows'

    buf = struct.pack('hhhh', height, width, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, buf)


def addstr(win, y, x, s, *args, **kwargs):
    try:
        win.addstr(y, x, s, *args, **kwargs)
    except curses.error:
        pass # writing on the last col/row raises an exception


def can_read(fd):
    '''Returns True if the file descriptor has available data'''
    return select.select([fd], [], [], 0) == ([fd], [], [])


class Cursor:
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


class Colors:
    def __init__(self):
        self.attr_map = {}
        self.next = 1

    def attr(self, fg, bg=-1):
        if (fg, bg) in self.attr_map:
            return self.attr_map[fg, bg]

        pair_num = self.next
        self.next += 1

        curses.init_pair(pair_num, fg, bg)
        self.attr_map[fg, bg] = curses.color_pair(pair_num)
        return self.attr_map[fg, bg]

colors = Colors()


class BannerWindow(Window):
    def refresh(self):
        self.win.leaveok(1) # avoid cursor blinking
        left = '[0] tmux.py'
        right = '"%s" %s' % (platform.node(),
                             datetime.now().strftime('%H:%M %d-%m-%Y'))
        banner = left + ' ' * (self.width - len(left) - len(right)) + right

        addstr(self.win, 0, 0, banner,
               colors.attr(curses.COLOR_BLACK, curses.COLOR_BLUE))

        self.win.refresh()
        self.win.leaveok(0)


class FormattedString:
    def __init__(self, text=None, attr=0, fg=-1, bg=-1):
        if text:
            self._elements = [(text, attr, fg, bg)]
        else:
            self._elements = []

    def __len__(self):
        return sum(len(text) for text, _, _, _ in self._elements)

    def __bool__(self):
        return bool(self._elements)

    def _clone(self):
        o = FormattedString()
        o._elements = copy.copy(self._elements)
        return o

    def _add(self, o):
        assert isinstance(o, FormattedString)

        for text, attr, fg, bg in o._elements:
            if self._elements and self._elements[-1][1:] == (attr, fg, bg):
                self._elements[-1] = (self._elements[-1][0] + text, attr, fg, bg)
            else:
                self._elements.append((text, attr, fg, bg))

    def __add__(self, s):
        if not self:
            return s
        if not s:
            return self

        o = self._clone()
        o._add(s)
        return o

    def __getitem__(self, index):
        if isinstance(index, int):
            index = index if index >= 0 else len(self) + index
            if not (0 <= index < len(self)):
                raise IndexError

            for text, attr, fg, bg in self._elements:
                n = len(text)

                if index < n:
                    return FormattedString(text[index], attr, fg, bg)
                else:
                    index -= n
        elif isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            assert step == 1

            o = FormattedString()
            for text, attr, fg, bg in self._elements:
                n = len(text)

                if start >= n:
                    start -= n
                    stop -= n
                else:
                    text = text[start:]
                    n = len(text)
                    stop -= start
                    start = 0

                    if stop <= n:
                        text = text[:stop]
                        o._add(FormattedString(text, attr, fg, bg))
                        return o
                    else:
                        o._add(FormattedString(text, attr, fg, bg))
                        stop -= n

            return o
        else:
            raise TypeError('index must be int or slice')

    def ljust(self, n, fillchar=' ', attr=0, fg=-1, bg=-1):
        m = len(self)

        if n > m:
            return self + FormattedString(fillchar * (n - m), attr, fg, bg)
        else:
            return self

    def rstrip(self, chars=None):
        o = self._clone()

        while o._elements:
            text, attr, fg, bg = o._elements[-1]

            if bg != -1:
                return o

            text = text.rstrip(chars)

            if not text:
                o._elements.pop()
            else:
                o._elements[-1] = (text, attr, fg, bg)
                return o

        return o

    def __repr__(self):
        return 'FormattedString(%r)' % self._elements


def add_formatted_str(win, y, x, s):
    for text, attr, fg, bg in s._elements:
        addstr(win, y, x, text, attr | colors.attr(fg, bg))
        x += len(text)


class ConsoleWindow(Window):
    def __init__(self, height, width, begin_y, begin_x, history_size, reply_query=None):
        super(ConsoleWindow, self).__init__(height, width, begin_y, begin_x)
        self.history_size = history_size
        self.reply_query = reply_query

        replay.info('%d:SIZE %d %d', time.time(), self.height, self.width)

        # the buffer
        self.lines = []
        self.lines.append([FormattedString(), # line content
                           0]) # real line number

        # There are two windows:
        # - the real window, where the cursor is and where writes are performed
        # - the display window. This is usually the real window except when you
        #     look at the history

        self.offset = 0 # first line of the real window
        self.cursor = Cursor(0, 0, # position in the real window
                             visibility=1)
        self.scroll_area = 0, height - 1
        self.attr = 0
        self.fg = -1
        self.bg = -1

        self.display_offset = 0 # first line of the display window
        self.auto_scroll = True

        self.redraw = True

    def _log_state(self):
        log.debug('offset: %d', self.offset)
        log.debug('display_offset: %d', self.display_offset)
        log.debug('cursor: (%d, %d)', self.cursor.x, self.cursor.x)
        log.debug('scroll_area: (%d, %d)', self.scroll_area[0], self.scroll_area[1])
        log.debug('lines = ')
        for line in self.lines:
            log.debug('  %r', line)

    def resize(self, height, width, begin_y, begin_x):
        prev_height, prev_width = self.size
        real_y, real_x = self._cursor_real_pos()
        super(ConsoleWindow, self).resize(height, width, begin_y, begin_x)
        self.scroll_area = 0, height - 1

        if prev_height != height:
            diff = prev_height - height
            if prev_height > height and self.cursor.y < height:
                diff = 0

            diff = max(diff, -self.offset)
            self.offset = max(min(self.offset + diff, len(self.lines) - 1), 0)
            self.display_offset = max(min(self.display_offset + diff, len(self.lines) - 1), 0)
            self.cursor.y = max(min(self.cursor.y - diff, height - 1), 0)

        if prev_width != width:
            # index of the current real line
            prev_y = self._index_real_line(real_y)

            # rebuild the buffer
            self._rebuild_lines(prev_width, width)

            # index of the current real line, after the rebuild
            new_y = self._index_real_line(real_y)

            self.offset = max(0, self.offset + new_y - prev_y)
            self.display_offset = max(0, self.display_offset + new_y - prev_y)

        # clean-up that could be needed (if too many/not enough lines)

        while len(self.lines) > self.offset + height:
            self._remove_lastline()

        while self.offset + self.cursor.y >= len(self.lines):
            self._insert_newline(real=False)

        self.redraw = True
        self._log_state()

    def _rebuild_lines(self, prev_width, new_width):
        lines = []
        current_line, current_num = self.lines[0]

        for line, num in self.lines[1:]:
            if current_num == num: # same line
                padding = (prev_width - len(current_line) % prev_width) % prev_width
                current_line += FormattedString(' ' * padding)
                current_line += line
            else:
                if not current_line:
                    lines.append([FormattedString(), current_num])
                else:
                    while current_line:
                        lines.append([current_line[:new_width], current_num])
                        current_line = current_line[new_width:]

                current_line, current_num = line, num

        if not current_line:
            lines.append([FormattedString(), current_num])
        else:
            while current_line:
                lines.append([current_line[:new_width], current_num])
                current_line = current_line[new_width:]

        self.lines = lines

    def refresh(self):
        if self.redraw:
            self.win.leaveok(1) # avoid cursor blinking

            for i in range(self.display_offset, self.display_offset + self.height):
                line = self.lines[i][0] if i < len(self.lines) else FormattedString()
                line = line.ljust(self.width, ' ')
                add_formatted_str(self.win, i - self.display_offset, 0, line)

            self.redraw = False
            self.win.leaveok(0)

        if 0 <= self.offset + self.cursor.y - self.display_offset < self.height:
            self.win.move(self.offset + self.cursor.y - self.display_offset,
                          min(self.cursor.x, self.width - 1))
            visibility = 1
        else:
            visibility = 0

        if self.cursor.visibility != visibility:
            curses.curs_set(visibility)
            self.cursor.visibility = visibility

        self.win.refresh()

    def write(self, data):
        '''Write data at the current cursor position'''
        assert self.offset + self.cursor.y < len(self.lines)

        if isinstance(data, bytes):
            data = data.decode('utf8', 'replace')

        log.debug('write: %r', data)
        replay.info('%d:WRITE %s', time.time(), json.dumps(data))

        current = ''
        while data:
            c = data[0]
            remove = 1

            if c == '\x1b':
                self._write_line(current)
                remove = self._control_seq(data)
                current = ''
            elif c == '\a':
                curses.beep()
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
        self._log_state()

    def _cursor_newline(self, real):
        '''Add a new line at the cursor position (if needed)

        Arguments:
            real(bool): Is it a real line or just a line wrapped?
        '''
        self.cursor.x = 0

        if self.cursor.y == self.scroll_area[1]:
            # scroll down
            self._scroll_down(real=real)
        else:
            # move the cursor down
            self.cursor.y = min(self.cursor.y + 1, self.height - 1)

            if self.offset + self.cursor.y > len(self.lines) - 1:
                self._insert_newline(real)

    def _insert_newline(self, real):
        '''Insert a new line at the end of the buffer

        Arguments:
            real(bool): Is it a real line or just a line wrapped?
        '''
        assert len(self.lines) + 1 <= self.offset + self.height

        line_num = self.lines[-1][1]
        if real:
            line_num += 1
        self.lines.append([FormattedString(), line_num])

        self._check_history_size()

    def _remove_lastline(self):
        '''Remove the last line in the buffer'''
        assert len(self.lines) > 0

        self.lines.pop()

    def _check_history_size(self):
        '''Shrink the history if needed

        Note: that method can update self.lines, self.display_offset and self.offset
        '''
        if len(self.lines) > self.history_size:
            nb = len(self.lines) - self.history_size
            self.lines = self.lines[nb:]
            self.display_offset = max(0, self.display_offset - nb)
            self.offset -= nb

    def _update_line(self, y, x, data):
        assert isinstance(data, (str, FormattedString))
        assert 0 <= y < len(self.lines)
        assert 0 <= x < self.width
        assert 0 <= x + len(data) <= self.width

        if isinstance(data, str):
            data = FormattedString(data)

        # update buffer
        self.lines[y][0] = (self.lines[y][0][:x].ljust(x, ' ') +
                            data +
                            self.lines[y][0][x + len(data):]).rstrip()

        # update screen directly (only if the window won't be redraw completely)
        if not self.redraw and self.display_offset <= y < self.display_offset + self.height:
            add_formatted_str(self.win, y - self.display_offset, x, data)

    def _write_line(self, data):
        assert isinstance(data, str)
        assert self.offset + self.cursor.y < len(self.lines)
        assert all(c not in data for c in ('\x1b', '\a', '\b', '\t', '\n', '\r'))

        while data:
            if self.cursor.x == self.width:
                self._cursor_newline(real=False)

            y, x = self.offset + self.cursor.y, self.cursor.x

            line = data[:self.width - x]
            data = data[self.width - x:]

            self._update_line(y, x, FormattedString(line, self.attr, self.fg, self.bg))

            self.cursor.x += len(line)

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
        _, x = self._cursor_real_pos() # num of chars before the cursor
        x += len(current) # num of chars after the cursor
        return current + ' ' * (8 - x % 8)

    def _control_seq(self, data):
        for regex, fun in ((r'^\x1b\[(\d+;\d+)?H', self._ctl_cursor_home),
                           (r'^\x1b\[(\d+;\d+)?f', self._ctl_cursor_home),
                           (r'^\x1b\[(\d+)?A', self._ctl_cursor_up),
                           (r'^\x1b\[(\d+)?B', self._ctl_cursor_down),
                           (r'^\x1b\[(\d+)?C', self._ctl_cursor_forward),
                           (r'^\x1b\[(\d+)?D', self._ctl_cursor_backward),
                           (r'^\x1b\[(\d+)?d', self._ctl_cursor_vertical_pos),
                           (r'^\x1b\[(\d+)?G', self._ctl_cursor_horizontal_pos),
                           (r'^\x1b\[0?K', self._ctl_erase_end_line),
                           (r'^\x1b\[1K', self._ctl_erase_start_line),
                           (r'^\x1b\[2K', self._ctl_erase_entire_line),
                           (r'^\x1b\[0?J', self._ctl_erase_down),
                           (r'^\x1b\[1J', self._ctl_erase_up),
                           (r'^\x1b\[2J', self._ctl_erase_screen),
                           (r'^\x1b\[(\d+)?X', self._ctl_erase_char),
                           (r'^\x1b\[M', self._ctl_erase_entire_line),
                           (r'^\x1b\[(\d+)?L', self._ctl_insert_line),
                           (r'^\x1b\[(\d+)?P', self._ctl_delete_char),
                           (r'^\x1b\[(\d+(;\d+)*)?r', self._ctl_scroll_area),
                           (r'^\x1bD', self._ctl_scroll_down),
                           (r'^\x1bM', self._ctl_scroll_up),
                           (r'^\x1b=', self._ctl_application_keypad),
                           (r'^\x1b>', self._ctl_normal_keypad),
                           (r'^\x1b\[(\d+(;\d+)*)?m', self._ctl_attr),
                           (r'^\x1b(\)|\(|\*|\+)[a-zA-Z]', lambda s: None),
                           (r'^\x1b\]\d+(;[^\a]+)*\a', lambda s: None),
                           (r'^\x1b\[(\d+(;\d+)*)(h|l)', self._ctl_set_mode),
                           (r'^\x1b\[\?(\d+(;\d+)*)(h|l)', self._ctl_private_set_mode),
                           (r'^\x1b\[c', self._ctl_query_code),
                           (r'^\x1b\[5n', self._ctl_query_status),
                           (r'^\x1b\[6n', self._ctl_query_cursor_pos),
                           (r'^\x1b\[>c', self._ctl_query_term_id)):
            match = re.search(regex, data)
            if match:
                log.debug('control sequence %r -> %s', match.group(0), fun.__name__)
                fun(match)
                return len(match.group(0))

        log.error('Unable to parse control sequence %r', data[:16])
        return 1

    def _ctl_set_mode(self, match):
        val = match.groups()[-1] == 'h'

        for num in map(int, match.group(1).split(';')):
            if num == 4:
                assert not val, 'insert mode not supported'
                continue # ignored
            else:
                log.error('Unknow control sequence %r', match.group(0))

    def _ctl_private_set_mode(self, match):
        val = match.groups()[-1] == 'h'

        for num in map(int, match.group(1).split(';')):
            if num in (1, 12, 25, 1049, 2004):
                continue # ignored
            elif num in (1000, 1001, 1002, 1005, 1006):
                continue # ignore all mouse modes
            else:
                log.error('Unknow control sequence %r', match.group(0))

    def _ctl_attr(self, match):
        s = match.group(1) or '0'
        it = map(int, s.split(';'))

        try:
            while True:
                attr = next(it)

                if attr == 0:
                    self.attr = 0
                    self.fg = self.bg = -1
                elif attr < 10:
                    self.attr |= {
                        1: curses.A_BOLD,
                        2: curses.A_DIM,
                        4: curses.A_UNDERLINE,
                        5: curses.A_BLINK,
                        7: curses.A_REVERSE,
                        8: curses.A_INVIS
                    }.get(attr, 0)
                elif 20 <= attr < 29:
                    self.attr &= ~({
                        2: curses.A_BOLD | curses.A_DIM,
                        4: curses.A_UNDERLINE,
                        5: curses.A_BLINK,
                        7: curses.A_REVERSE,
                        8: curses.A_INVIS
                    }.get(attr, 0))
                elif 30 <= attr <= 37:
                    self.fg = attr - 30
                elif attr == 39:
                    self.fg = -1
                elif 40 <= attr <= 47:
                    self.bg = attr - 40
                elif attr == 49:
                    self.bg = -1
                elif attr in (38, 48):
                    kind = next(it)

                    if kind == 2:
                        r = next(it)
                        g = next(it)
                        b = next(it)
                    elif kind == 5:
                        rgb = next(it)

                        if rgb < 16:
                            continue
                        elif 16 <= rgb < 232:
                            rgb -= 16
                            r = (rgb // 36) * 256 // 6
                            g = ((rgb // 6) % 6) * 256 // 6
                            b = (rgb % 6) * 256 // 6
                        else:
                            r = g = b = (rgb - 232) * 256 // 23
                    else:
                        log.error('Unknow control sequence %r', match.group(0))
                        continue

                    if attr == 38:
                        self.fg = self._approximate_color(r, g, b)
                    else:
                        self.bg = self._approximate_color(r, g, b)

        except StopIteration:
            pass

    def _approximate_color(self, r, g, b):
        colors = [
            (curses.COLOR_BLACK, (0, 0, 0)),
            (curses.COLOR_RED, (174, 0, 0)),
            (curses.COLOR_GREEN, (0, 174, 0)),
            (curses.COLOR_YELLOW, (174, 174, 0)),
            (curses.COLOR_BLUE, (0, 0, 174)),
            (curses.COLOR_MAGENTA, (174, 0, 174)),
            (curses.COLOR_CYAN, (0, 174, 174)),
            (curses.COLOR_WHITE, (174, 174, 174)),
        ]

        color = min(colors,
                    key=lambda c: math.sqrt((r - c[1][0])**2 + (g - c[1][1])**2 + (b - c[1][2])**2))
        return color[0]

    def _ctl_cursor_home(self, match):
        y, x = 1, 1
        if match.group(1):
            y, x = map(int, match.group(1).split(';'))

        self._move_cursor_win(y - 1, x - 1)

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

    def _ctl_cursor_vertical_pos(self, match):
        y = 1
        if match.group(1):
            y = int(match.group(1))

        self._move_cursor_win(y - 1, self.cursor.x)

    def _ctl_cursor_horizontal_pos(self, match):
        x = 1
        if match.group(1):
            x = int(match.group(1))

        self._move_cursor_win(self.cursor.y, x - 1)

    def _ctl_erase_end_line(self, match):
        y, x = self.offset + self.cursor.y, min(self.width - 1, self.cursor.x)
        self._update_line(y, x, ' ' * (self.width - x))

    def _ctl_erase_start_line(self, match):
        y, x = self.offset + self.cursor.y, min(self.width - 1, self.cursor.x)
        self._update_line(y, 0, ' ' * (x + 1))

    def _ctl_erase_entire_line(self, match):
        y = self.offset + self.cursor.y

        # update buffer
        self.lines[y][0] = FormattedString()

        # update screen directly (only if the window won't be redraw completely)
        if not self.redraw and self.display_offset <= y < self.display_offset + self.height:
            add_formatted_str(self.win, y - self.display_offset, 0, FormattedString(' ' * self.width))

    def _ctl_erase_down(self, match):
        y, x = self.offset + self.cursor.y, min(self.width - 1, self.cursor.x)

        # update buffer
        self.lines[y][0] = self.lines[y][0][:x]
        self.lines = self.lines[:y + 1]
        self.redraw = True

    def _ctl_erase_up(self, match):
        y, x = self.offset + self.cursor.y, min(self.width - 1, self.cursor.x)

        # update buffer
        self.lines[y][0] = FormattedString(' ' * (x + 1)) + self.lines[y][0][x + 1:]
        for i in range(self.offset, y):
            self.lines[i][0] = FormattedString()
        self.redraw = True

    def _ctl_erase_screen(self, match):
        self._ctl_erase_up(match)
        self._ctl_erase_down(match)

    def _ctl_erase_char(self, match):
        num = 1
        if match.group(1):
            num = int(match.group(1))

        y, x = self.offset + self.cursor.y, self.cursor.x

        if x == self.width:
            return

        self._update_line(y, x, ' ' * num)

    def _ctl_insert_line(self, match):
        num = 1
        if match.group(1):
            num = int(match.group(1))

        if self.cursor.y > self.scroll_area[1]:
            return

        saved_scroll_area = self.scroll_area
        self.scroll_area = (self.cursor.y, self.scroll_area[1])

        for _ in range(num):
            self._scroll_up()

        self.scroll_area = saved_scroll_area

    def _ctl_delete_char(self, match):
        num = 1
        if match.group(1):
            num = int(match.group(1))

        y, x = self.offset + self.cursor.y, self.cursor.x

        if x == self.width:
            return

        self._update_line(y, x, self.lines[y][0][x + num:].ljust(self.width - x, ' '))

    def _ctl_scroll_area(self, match):
        top, down = 1, self.height
        if match.group(1):
            top, down = map(int, match.group(1).split(';'))

        down = max(min(down, self.height), 1)
        top = max(min(top, down), 1)

        self.scroll_area = top - 1, down - 1
        self._move_cursor_win(0, 0)

    def _scroll_down(self, real):
        area_top, area_down = self.scroll_area

        if area_top == 0 and area_down == self.height - 1: # usual scroll
            self.offset += 1

            if self.auto_scroll:
                self.display_offset = self.offset

            if self.offset + self.cursor.y > len(self.lines) - 1:
                self._insert_newline(real)
        else:
            for i in range(self.offset + area_top,
                           min(self.offset + area_down, len(self.lines) - 1)):
                self.lines[i] = copy.copy(self.lines[i + 1])

        self.lines[self.offset + area_down][0] = FormattedString()
        num = self.lines[self.offset + area_down - 1][1] if self.offset + area_down > 0 else 0

        if real:
            num += 1
        self.lines[self.offset + area_down][1] = num
        last = None

        for i in range(self.offset + area_down + 1, len(self.lines)):
            if last != self.lines[i][1]:
                num += 1

            last = self.lines[i][1]
            self.lines[i][1] = num

        self.redraw = True

    def _ctl_scroll_down(self, match):
        if self.cursor.y != self.scroll_area[1]:
            self._move_cursor_win(self.cursor.y + 1, self.cursor.x)
            return

        self._scroll_down(real=True)

    def _scroll_up(self):
        area_top, area_down = self.scroll_area

        for i in range(min(self.offset + area_down - 1, len(self.lines) - 1),
                       self.offset + area_top - 1,
                       -1):
            if i == len(self.lines) - 1: # last line
                self.lines.append(copy.copy(self.lines[i]))
            else:
                self.lines[i + 1] = copy.copy(self.lines[i])

        self.lines[self.offset + area_top][0] = FormattedString()
        num = self.lines[self.offset + area_top - 1][1] + 1 if self.offset + area_top > 0 else 0

        self.lines[self.offset + area_top][1] = num
        last_num = None

        for i in range(self.offset + area_top + 1, len(self.lines)):
            if last_num != self.lines[i][1]:
                num += 1

            last_num = self.lines[i][1]
            self.lines[i][1] = num

        self.redraw = True

    def _ctl_scroll_up(self, match):
        if self.cursor.y != self.scroll_area[0]:
            self._move_cursor_win(self.cursor.y - 1, self.cursor.x)
            return

        self._scroll_up()

    def _ctl_application_keypad(self, match):
        self.win.keypad(1)

    def _ctl_normal_keypad(self, match):
        self.win.keypad(0)

    def _ctl_query_code(self, match):
        if not self.reply_query:
            return

        self.reply_query('\x1b[?1;2c')

    def _ctl_query_status(self, match):
        if not self.reply_query:
            return

        self.reply_query('\x1b[0n')

    def _ctl_query_cursor_pos(self, match):
        if not self.reply_query:
            return

        self.reply_query('\x1b[%d;%dR' % (self.cursor.y + 1, self.cursor.x + 1))

    def _ctl_query_term_id(self, match):
        if not self.reply_query:
            return

        self.reply_query('\x1b[>84;0;0c')

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


class Process:
    def __init__(self, args, env=None):
        # validate parameters
        if not isinstance(args, str):
            args = [args]

        env = env or os.environ

        # open a new pty
        master, slave = pty.openpty()
        tty.setraw(master)
        tty.setraw(slave)

        # launch subprocess
        self.proc = subprocess.Popen(args=args,
                                     env=env,
                                     stdin=slave,
                                     stdout=slave,
                                     stderr=slave,
                                     close_fds=True,
                                     preexec_fn=self._preexec_fn)

        self.proc.stdin = os.fdopen(os.dup(master), 'r+b', 0)
        self.proc.stdout = os.fdopen(os.dup(master), 'r+b', 0)
        self.proc.stderr = os.fdopen(os.dup(master), 'r+b', 0)

        os.close(master)
        os.close(slave)

    @property
    def pid(self):
        return self.proc.pid

    @property
    def stdin(self):
        return self.proc.stdin

    @property
    def stdout(self):
        return self.proc.stdout

    @property
    def stderr(self):
        return self.proc.stderr

    def poll(self):
        return self.proc.poll()

    def kill(self):
        return self.proc.kill()

    def send_signal(self, sig):
        self.proc.send_signal(sig)

    def _preexec_fn(self):
        '''
        Routine executed in the child process before invoking execve().

        This makes the pseudo-terminal the controlling tty. This should be
        more portable than the pty.fork() function.
        '''
        child_name = os.ttyname(0)

        # Disconnect from controlling tty. Harmless if not already connected.
        try:
            fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
            if fd >= 0:
                os.close(fd)
        except OSError:
            pass  # Already disconnected

        os.setsid()

        # Verify we are disconnected from controlling tty
        # by attempting to open it again.
        try:
            fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
            if fd >= 0:
                os.close(fd)
                raise Exception('Failed to disconnect from controlling tty. '
                                'It is still possible to open /dev/tty.')
        except OSError:
            pass  # Good! We are disconnected from a controlling tty.

        # Verify we can open the child pty.
        fd = os.open(child_name, os.O_RDWR)
        if fd < 0:
            raise Exception('Could not open child pty, %s' % child_name)
        else:
            os.close(fd)

        # Verify we now have a controlling tty.
        fd = os.open('/dev/tty', os.O_WRONLY)
        if fd < 0:
            raise Exception('Could not open controlling tty, /dev/tty')
        else:
            os.close(fd)


class ScreenManager:
    def __init__(self, screen):
        height, width = get_hw(sys.stdout)
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
        height, width = get_hw(sys.stdout)
        curses.resizeterm(height, width)
        curses.update_lines_cols()

        self.screen.resize(height, width)
        self.banner.resize(1, width, height - 1, 0)
        self.console.resize(height - 1, width, 0, 0)

        set_hw(self.proc.stdout, self.console.height, self.console.width)
        self.proc.send_signal(signal.SIGWINCH)

        self.resize_event = False

        self.screen.clear()
        self.refresh()

    def get_key(self):
        fd = sys.stdin.fileno()

        if can_read(fd):
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

        self.proc = Process(os.environ.get('SHELL', '/bin/sh'))
        self.console.reply_query = lambda s: self.proc.stdin.write(s.encode('utf8'))
        set_hw(self.proc.stdout, self.console.height, self.console.width)
        self.proc.send_signal(signal.SIGWINCH)

        try:
            self.refresh()

            while True:
                key = self.get_key()
                if key:
                    self.proc.stdin.write(key)
                    self.refresh()

                if self.resize_event:
                    self.resize()

                if self.proc.poll() is not None:
                    break

                if can_read(self.proc.stdout):
                    self.console.write(self.proc.stdout.read(4096))
                    self.refresh()

                if can_read(self.proc.stderr):
                    self.console.write(self.proc.stderr.read(4096))
                    self.refresh()

                curses.napms(5)
        finally:
            signal.signal(signal.SIGWINCH, old_sigwinch)
            signal.signal(signal.SIGCONT, old_sigcont)

            if self.proc.poll() is None:
                self.proc.kill()


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
