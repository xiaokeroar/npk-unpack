// npk_zstd.js - batch zstd decompressor used by npk_unpack.py
// Usage: node npk_zstd.js jobs.json
// jobs.json: [{"src": "...packed zstd frame...", "dst": "...decompressed..."}]
const fs = require('fs');
const zlib = require('zlib');

const jobsPath = process.argv[2];
if (!jobsPath) {
  console.error('usage: node npk_zstd.js jobs.json');
  process.exit(2);
}
let jobs;
try {
  jobs = JSON.parse(fs.readFileSync(jobsPath, 'utf8'));
} catch (e) {
  console.error('cannot read jobs: ' + e.message);
  process.exit(2);
}
let ok = 0;
let fail = 0;
for (const j of jobs) {
  try {
    const src = fs.readFileSync(j.src);
    const out = zlib.zstdDecompressSync(src);
    fs.writeFileSync(j.dst, out);
    ok++;
  } catch (e) {
    console.log('FAIL ' + j.src.split(/[\\/]/).pop() + ' : ' + e.message);
    fail++;
  }
}
console.log('ok=' + ok + ' fail=' + fail);
if (fail) process.exitCode = 1;
