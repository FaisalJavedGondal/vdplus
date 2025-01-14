import os
import subprocess

from visidata import VisiData, vd, asyncthread


@VisiData.api
def launchBrowser(vd, *args):
    'Launch $BROWSER with *args* as arguments.'
    browser = os.environ.get('BROWSER') or 'firefox' or vd.fail('$BROWSER not set')
    args = [browser] + list(args)
    subprocess.call(args)

