import { spawnSync } from 'node:child_process';
import { copyFileSync, readFileSync } from 'node:fs';
const result = spawnSync('allure', ['generate', '/results/allure-results', '--config', '/report/allurerc.json', '--output', '/results/html'], {stdio: 'inherit'});
if (result.error || result.status !== 0) process.exit(2);
copyFileSync('/results/html/index.html', '/results/allure-report.html');
const smoke = JSON.parse(readFileSync('/results/results.json', 'utf8'));
process.exit(smoke.failures === 0 ? 0 : 1);
