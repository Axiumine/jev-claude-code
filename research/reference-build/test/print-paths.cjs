'use strict';
// helper for e2e: prints where the lib would read config/state (used to prove that the environment cannot redirect them)
const L = require('../jev/lib/jevlib.cjs');
console.log(JSON.stringify({ CFG: L.CFG, STATE: L.STATE, HOME: L.HOME, test: L.TEST_MODE }));
