// Runs the repo's own patch scripts (web/build.py and the mods) in Pyodide, off the page's thread.
// A module worker: Pyodide doesn't support classic ones.
// Messages in:  {cmd: 'init'} | {cmd: 'build', buf: ArrayBuffer (the stock image)}
// Messages out: {type: 'progress', msg} | {type: 'ready'} | {type: 'done', mod, recovery} | {type: 'error', msg}
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v314.0.8/full/';
const FILES = [
  'rkfw.py', 'rkcrc.py', 'fwtool.py',
  'mods/navilib.py', 'mods/nanitec.py', 'mods/turbo.py', 'mods/opus.py', 'mods/opus/build/opus.bin',
  'web/build.py',
];

let ready = null;

// rkcrc.py's RKCRC32 (poly 0x04C10DB7, MSB first, init 0): the image checksum, ~50x faster in JS
const CRC_TABLE = new Uint32Array(256);
for (let i = 0; i < 256; i++) {
  let c = (i << 24) >>> 0;
  for (let k = 0; k < 8; k++) c = c & 0x80000000 ? ((c << 1) ^ 0x04c10db7) >>> 0 : (c << 1) >>> 0;
  CRC_TABLE[i] = c;
}
function rkcrc32(data, crc) {
  crc >>>= 0;
  for (let i = 0; i < data.length; i++) crc = ((crc << 8) ^ CRC_TABLE[((crc >>> 24) ^ data[i]) & 0xff]) >>> 0;
  return crc;
}

function progress(msg) {
  postMessage({ type: 'progress', msg });
}

async function init() {
  progress('Downloading Python (about 13 MB, the first time only)…');
  const { loadPyodide } = await import(PYODIDE + 'pyodide.mjs');
  const py = await loadPyodide({ indexURL: PYODIDE });
  progress('Downloading the patch scripts…');
  const root = new URL('../', self.location.href);
  await Promise.all(FILES.map(async (f) => {
    const r = await fetch(new URL(f, root), { cache: 'no-cache' });
    if (!r.ok) throw new Error(`could not load ${f} (${r.status})`);
    const dir = '/fw/' + f.split('/').slice(0, -1).join('/');
    py.FS.mkdirTree(dir);
    py.FS.writeFile('/fw/' + f, new Uint8Array(await r.arrayBuffer()));
  }));
  py.runPython(`
import sys
sys.path.insert(0, '/fw/web')
import build, fwtool
from pyodide.ffi import to_js

def run(progress, crc):
    fwtool.rkcrc32 = lambda data, c=0: crc(to_js(data), c)
    try:
        raw = open('/in.img', 'rb').read()
        build.check_stock(raw)
        open('/mod.img', 'wb').write(build.build(raw, progress=lambda s: progress('Applying ' + s + '…')))
        progress('Making the recovery image…')
        open('/recovery.img', 'wb').write(build.recovery(raw))
        return ''
    except SystemExit as e:
        return str(e) or 'the patch scripts refused this file'
`);
  return py;
}

function lastLine(err) {
  // a Python traceback ends with the line that matters
  const lines = String(err && err.message || err).trim().split('\n');
  return lines[lines.length - 1];
}

self.onmessage = async (e) => {
  let py;
  try {
    if (!ready) ready = init().catch((err) => { ready = null; throw err; });
    py = await ready;
  } catch (err) {
    postMessage({ type: 'error', msg: 'Could not start Python: ' + lastLine(err) });
    return;
  }
  if (e.data.cmd !== 'build') {
    postMessage({ type: 'ready' });
    return;
  }
  try {
    py.FS.writeFile('/in.img', new Uint8Array(e.data.buf));
    const err = py.globals.get('run')(progress, rkcrc32);
    py.FS.unlink('/in.img');
    if (err) throw new Error(err);
    const mod = py.FS.readFile('/mod.img');
    const recovery = py.FS.readFile('/recovery.img');
    py.FS.unlink('/mod.img');
    py.FS.unlink('/recovery.img');
    postMessage({ type: 'done', mod: mod.buffer, recovery: recovery.buffer }, [mod.buffer, recovery.buffer]);
  } catch (err) {
    postMessage({ type: 'error', msg: lastLine(err) });
  }
};
