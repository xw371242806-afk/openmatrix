import fs from 'node:fs'

function read(p) {
  return JSON.parse(fs.readFileSync(p, 'utf8'))
}

function deepMerge(base, patch, path = '') {
  for (const [k, v] of Object.entries(patch)) {
    const here = path ? `${path}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      if (!base[k] || typeof base[k] !== 'object' || Array.isArray(base[k])) base[k] = {}
      deepMerge(base[k], v, here)
    } else {
      if (k in base) console.warn('OVERWRITE at', here)
      base[k] = v
    }
  }
}

for (const lang of ['en', 'zh']) {
  const target = new URL(`../src/locales/${lang}.json`, import.meta.url)
  const json = read(target)
  for (const p of ['p2', 'p3', 'p4', 'p5']) {
    const f = new URL(`../src/locales/patches/${p}.${lang}.json`, import.meta.url)
    if (fs.existsSync(f)) deepMerge(json, read(f))
  }
  fs.writeFileSync(target, JSON.stringify(json, null, 2) + '\n')
  console.log(`merged patches into ${lang}.json`)
}
