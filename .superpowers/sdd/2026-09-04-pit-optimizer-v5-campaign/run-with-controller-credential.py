"""Load the user-selected controller .env key for a locally approved campaign launch.

Run only after external-call approval. No key is written to disk or printed.
The selected file must be .env beside the main checkout's Git directory.
The existing controller parser accepts OPENROUTER and OPENROUTER_API_KEY.
"""
import argparse
import os
from pathlib import Path
import runpy
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--dotenv', required=True, type=Path)
parser.add_argument('helper', type=Path)
parser.add_argument('arguments', nargs=argparse.REMAINDER)
args = parser.parse_args()
dotenv = args.dotenv.absolute()
helper = args.helper.resolve(strict=True)
if dotenv.name != '.env' or not (dotenv.parent / '.git').is_dir():
    raise ValueError('Select the main checkout .env file beside its Git directory')

from agent_loop import _controller_dotenv_values

values = _controller_dotenv_values(dotenv.parent)
keys = {values.get('OPENROUTER'), values.get('OPENROUTER_API_KEY'),
        os.environ.get('OPENROUTER'), os.environ.get('OPENROUTER_API_KEY')}
keys.discard(None)
keys.discard('')
if len(keys) != 1 or not (values.get('OPENROUTER') or values.get('OPENROUTER_API_KEY')):
    raise ValueError('The selected controller key is missing or conflicts with another configured handle')
previous = os.environ.get('OPENROUTER_API_KEY')
original_argv = sys.argv
try:
    os.environ['OPENROUTER_API_KEY'] = next(iter(keys))
    sys.argv = [str(helper), *args.arguments]
    runpy.run_path(str(helper), run_name='__main__')
finally:
    sys.argv = original_argv
    if previous is None:
        os.environ.pop('OPENROUTER_API_KEY', None)
    else:
        os.environ['OPENROUTER_API_KEY'] = previous
