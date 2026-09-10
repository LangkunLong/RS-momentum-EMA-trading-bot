"""Hold an idle-sleep request while a local operator helper runs on this thread.

Does not override lid closure, explicit sleep, shutdown or battery protection.
Does not change persistent power settings, the display, PATH or campaign clocks.
Reference: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import runpy
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('helper', type=Path)
parser.add_argument('arguments', nargs=argparse.REMAINDER)
args = parser.parse_args()
helper = args.helper.resolve(strict=True)
if os.name != 'nt':
    raise RuntimeError('This local helper requires Windows')
set_state = ctypes.WinDLL('kernel32', use_last_error=True).SetThreadExecutionState
set_state.argtypes = [ctypes.c_uint32]
set_state.restype = ctypes.c_uint32
continuous = 0x80000000
previous = set_state(continuous | 0x00000001)
if previous == 0:
    raise RuntimeError('Windows rejected the idle-sleep request; helper was not launched')
original_argv = sys.argv
try:
    print(json.dumps({'host_idle_sleep_request': 'active', 'lid_sleep_prevented': False}), flush=True)
    sys.argv = [str(helper), *args.arguments]
    runpy.run_path(str(helper), run_name='__main__')
finally:
    sys.argv = original_argv
    if set_state(previous | continuous) == 0:
        print('Could not restore previous thread power request; it ends when this process exits.', file=sys.stderr)
    else:
        print(json.dumps({'host_idle_sleep_request': 'restored'}), flush=True)
