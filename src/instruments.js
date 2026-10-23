import { readFileSync } from 'node:fs';

export const instruments = JSON.parse(readFileSync(new URL('./instruments.json', import.meta.url), 'utf8'));

export const publicInstrument = ({ base, ...instrument }) => instrument;
