const addon = require(process.argv[2]);
const ab = addon.soundnessHole();
const view = new Uint8Array(ab);
console.log(`byteLength=${ab.byteLength} bytes=${Array.from(view).join(',')}`);
for (let i = 0; i < 10000; i++) Buffer.alloc(4096, i & 255);
console.log(`afterChurn=${Array.from(view).join(',')}`);
