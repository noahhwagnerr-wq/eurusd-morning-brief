// inject-pat.js
// Dieses Script wird vom update-data.yml Workflow aufgerufen,
// um den GH_PAT sicher in index.html einzusetzen (serverseitig, nie öffentlich).
// Der PAT kommt aus process.env.GH_PAT (GitHub Secret).

const fs   = require('fs');
const path = require('path');

const PAT  = process.env.GH_PAT || '';
if (!PAT) {
  console.log('[inject-pat] GH_PAT nicht gesetzt – überspringe.');
  process.exit(0);
}

const file = path.join(__dirname, 'index.html');
let html   = fs.readFileSync(file, 'utf8');

// Ersetze die Zeile: const GH_PAT = window.GH_PAT || '';
html = html.replace(
  /const GH_PAT = window\.GH_PAT \|\| '';/,
  `const GH_PAT = '${PAT}';`
);

fs.writeFileSync(file, html, 'utf8');
console.log('[inject-pat] GH_PAT erfolgreich in index.html injiziert.');
