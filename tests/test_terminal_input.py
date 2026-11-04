"""Exercise actual terminal key bytes rather than mocking input()."""
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(os.name == 'posix' and importlib.util.find_spec('readline'), 'requires a POSIX terminal and readline')
class TerminalInputTests(unittest.TestCase):
    def read_edited_line(self, keys):
        import pty
        master, slave = pty.openpty()
        code = ('from src.application.cli import _enable_line_editing\n'
                'import json\n'
                '_enable_line_editing()\n'
                'value = input("READY>")\n'
                'print("\\nRESULT:" + json.dumps(value))\n')
        process = subprocess.Popen([sys.executable, '-c', code], stdin=slave, stdout=slave, stderr=slave,
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'TERM': 'xterm-256color', 'LC_CTYPE': 'en_US.UTF-8'})
        os.close(slave)
        output = b''
        deadline = time.monotonic() + 8
        sent = False
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .1)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                if not sent and b'READY>' in output:
                    os.write(master, keys)
                    sent = True
                match = re.search(rb'RESULT:([^\r\n]*)\r?\n', output)
                if match:
                    self.assertEqual(process.wait(timeout=2), 0)
                    return json.loads(match[1])
            self.fail('terminal did not return a result: ' + repr(output))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            os.close(master)

    def test_backspace_deletes_a_whole_chinese_character(self):
        for backspace in (b'\x7f', b'\x08'):
            with self.subTest(backspace=backspace):
                self.assertEqual(self.read_edited_line('中文'.encode() + backspace + '好\n'.encode()), '中好')

    def test_repeated_backspaces_clear_chinese_then_accept_new_text(self):
        self.assertEqual(self.read_edited_line('你好世界'.encode() + b'\x7f' * 4 + '重新输入\n'.encode()), '重新输入')

    def test_mixed_english_and_chinese_delete_by_character(self):
        self.assertEqual(self.read_edited_line('A中B文'.encode() + b'\x7f' * 2 + '好\n'.encode()), 'A中好')

    def test_cursor_left_then_backspace_preserves_neighboring_characters(self):
        self.assertEqual(self.read_edited_line('中文测试'.encode() + b'\x1b[D\x7f' + '好\n'.encode()), '中文好试')


if __name__ == '__main__':
    unittest.main()
