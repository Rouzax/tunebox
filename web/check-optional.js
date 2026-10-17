// Parsed separately from check.js: in a browser without optional chaining
// this whole file is a syntax error and the flag stays unset.
window.tuneboxSyntax = window.tuneboxSyntax || {};
window.tuneboxSyntax.optionalChaining = (null)?.missing === undefined;
