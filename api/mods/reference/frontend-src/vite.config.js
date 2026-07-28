import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Builds the reference mod's ONE status view as a Svelte 5 custom element and
// emits it as `entry.<contenthash>.js` at the ROOT of api/mods/reference/ ---
// the exact filename/location contract T-A05's bundle-discovery glob
// (`entry.*.js`, non-recursive at the mod root) resolves and T-A03 serves.
//
// cavekit-mods-frontend-client.md R7 (AC1, AC7).
export default defineConfig({
	plugins: [
		// compilerOptions.customElement: true compiles every .svelte file in this
		// package as a Web Component. The one component carries
		// `<svelte:options customElement="mod-reference" />`, so importing the
		// entry self-registers the tag via customElements.define() (R3).
		svelte({ compilerOptions: { customElement: true } })
	],
	build: {
		// One level up from frontend-src/ === api/mods/reference/ (the mod root).
		outDir: '..',
		// CRITICAL: do NOT wipe the mod root --- mod.yaml, reference_mod.py, and
		// state.py live there too. A build only adds/replaces its own entry file.
		emptyOutDir: false,
		// Content-hashed, immutable filename: a content change yields a NEW name
		// (a new URL), never an in-place overwrite --- the property T-A05's
		// always-fresh manifest + T-A03's immutable asset headers rely on.
		lib: {
			entry: 'src/main.js',
			formats: ['es']
		},
		rollupOptions: {
			output: {
				entryFileNames: 'entry.[hash].js',
				// One self-contained file: no split chunks, no separate asset dir
				// cluttering the mod root that the static server exposes.
				inlineDynamicImports: true
			}
		}
	}
});
