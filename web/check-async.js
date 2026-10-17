// Parsed separately from check.js: in a browser without async functions this
// whole file is a syntax error and the flag stays unset.
window.tuneboxSyntax = window.tuneboxSyntax || {};
window.tuneboxSyntax.asyncAwait = typeof async function () {
    await 0;
} === 'function';
